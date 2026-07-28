"""
ui/controllers/fetch_controller.py
====================================
Manages all URL fetch, scrape, and batch-import operations.
Owns the FetchWorker and ScraperWorker lifecycle.

Communicates exclusively via Qt signals — zero direct panel references.
AppWindow wires signals to panels and calls fetch() / scrape() / batch_import().
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from config import AppConfig

logger = logging.getLogger(__name__)


class FetchController(QObject):
    """
    Owns the fetch / scrape / batch-import flows.

    Signals
    -------
    track_fetched    : TrackMeta dict — AppWindow calls _add_track_to_queue()
    fetch_finished   : ParseResult — AppWindow updates _last_url_kind / status bar
    fetch_error      : str — AppWindow shows MessageBox
    scrape_finished  : list[str] — AppWindow updates url_bar + status
    status_update    : str — → status_bar.set_status()
    fetching_changed : bool — → url_bar.set_fetching()
    cancel_visible   : bool — → status_bar.set_cancel_visible()
    """

    track_fetched    = Signal(object)   # TrackMeta / dict
    fetch_finished   = Signal(object)   # ParseResult
    fetch_error      = Signal(str)
    scrape_finished  = Signal(list)     # list[str] of scraped URLs
    status_update    = Signal(str)      # in-flight activity (indeterminate)
    temporary_status = Signal(str)      # short terminal, non-critical note
    fetching_changed = Signal(bool)
    cancel_visible   = Signal(bool)

    def __init__(self, config: AppConfig, parent: QObject = None) -> None:
        super().__init__(parent)
        self._cfg             = config
        self._fetch_worker:   Optional = None
        self._scraper_worker: Optional = None
        # TXT imports deliberately reuse the existing single-worker model.
        # A controller-owned FIFO preserves file order and prevents several
        # Playwright/yt-dlp workers from cancelling or overwriting each other.
        self._batch_urls: list[str] = []
        self._batch_total = 0
        self._batch_succeeded = 0
        self._batch_failed = 0
        self._batch_skipped = 0
        self._batch_active = False
        self._batch_cancel_requested = False

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, url: str, channel_tabs: Optional[list[str]] = None) -> None:
        """Start a FetchWorker for the given URL."""
        self._reset_batch_state()
        self._start_fetch(url, channel_tabs=channel_tabs)

    def _start_fetch(self, url: str, channel_tabs: Optional[list[str]] = None) -> None:
        """Start exactly one worker without changing the enclosing TXT batch."""
        from ui.workers.fetch_worker import FetchWorker
        from ui.i18n import t

        if not url.strip():
            return
        if self._fetch_worker and self._fetch_worker.isRunning():
            self._fetch_worker.cancel()
            self._fetch_worker.wait(500)

        self.fetching_changed.emit(True)
        self.status_update.emit(t("fetching"))
        self.cancel_visible.emit(True)

        worker = FetchWorker(
            url,
            cookies_file=self._cfg.cookies_file,
            proxy_url=self._cfg.proxy_server_url,
            proxy_token=self._cfg.spotify_app_api_key,
            channel_tabs=channel_tabs,
            parent=self,
        )
        self._fetch_worker = worker
        # Capture worker identity in every callback.  A cancelled worker may
        # finish after the bounded wait; its late signals must not be counted
        # as results from a newer direct fetch or TXT entry.
        worker.track_found.connect(
            lambda meta, index, total, owner=worker:
            self._on_track_meta(meta, index, total, owner)
        )
        worker.finished.connect(
            lambda result, owner=worker: self._on_fetch_finished(result, owner)
        )
        worker.error.connect(
            lambda err, owner=worker: self._on_fetch_error(err, owner)
        )
        worker.start()

    def cancel(self) -> None:
        """Cancel any in-flight fetch or scrape."""
        if self._batch_active:
            self._batch_cancel_requested = True
            self._batch_urls.clear()
        if self._fetch_worker and self._fetch_worker.isRunning():
            self._fetch_worker.cancel()
        elif self._batch_active:
            self._finish_batch(cancelled=True)
        if self._scraper_worker and self._scraper_worker.isRunning():
            self._scraper_worker.cancel()
        if not self._batch_active:
            self.fetching_changed.emit(False)
            self.cancel_visible.emit(False)

    def scrape(self, url: str) -> None:
        """Start a ScraperWorker for the given URL."""
        from ui.workers.scraper_worker import ScraperWorker

        if self._scraper_worker and self._scraper_worker.isRunning():
            self._scraper_worker.cancel()

        self._scraper_worker = ScraperWorker(
            url, cookies_file=self._cfg.cookies_file, parent=self
        )
        self._scraper_worker.finished.connect(self.scrape_finished)
        # A scrape failure is a short terminal note, not ongoing activity \u2014
        # route it to the auto-clearing temporary channel (no stuck spinner).
        self._scraper_worker.error.connect(self.temporary_status)
        self._scraper_worker.start()

    def batch_import(self, file_path: str) -> None:
        """Import and fetch every supported URL, sequentially and in order."""
        from core.batch_importer import BatchImporter
        from ui.i18n import t
        from ui.dialogs.styled_dialog import show_warning

        try:
            result = BatchImporter.from_text_file(file_path)
        except Exception as exc:
            show_warning(self.parent(), t("batch_import_failed"), str(exc))
            return

        if not result.urls:
            self.temporary_status.emit(t("no_urls_found", filename=Path(file_path).name))
            return

        if self._fetch_worker and self._fetch_worker.isRunning():
            self._fetch_worker.cancel()
            self._fetch_worker.wait(500)
        self._reset_batch_state()
        self._batch_urls = list(result.urls)
        self._batch_total = len(result.urls)
        self._batch_skipped = result.skipped_count
        self._batch_active = True
        self.fetching_changed.emit(True)
        self.cancel_visible.emit(True)
        self._start_next_batch_url()

    # ── Private slots ─────────────────────────────────────────────────────────

    def _on_track_meta(self, meta, index: int, total: int, worker=None) -> None:
        if worker is not None and worker is not self._fetch_worker:
            return
        self.track_fetched.emit(meta)
        from ui.i18n import t
        title = (
            meta.get("title", "") if isinstance(meta, dict)
            else getattr(meta, "title", "")
        )
        if total > 1:
            self.status_update.emit(t("fetching_progress", n=index, total=total))
        else:
            self.status_update.emit(t("fetching_single", title=title[:50]))

    def _on_fetch_finished(self, result, worker=None) -> None:
        if worker is not None and worker is not self._fetch_worker:
            return
        batch_failure = (
            self._batch_active
            and bool(getattr(result, "error", ""))
            and not getattr(result, "tracks", None)
        )
        # AppWindow presents an empty error result as a modal dialog.  During
        # TXT import that would stop the FIFO until a person dismissed every
        # failed URL, so collect it for the final summary and continue.  Valid
        # and partial results still reach AppWindow normally.
        if not batch_failure:
            self.fetch_finished.emit(result)
        self._fetch_worker = None
        if self._batch_active:
            if self._batch_cancel_requested or getattr(result, "cancelled", False):
                self._finish_batch(cancelled=True)
                return
            if getattr(result, "tracks", None):
                self._batch_succeeded += 1
            else:
                self._batch_failed += 1
            self._start_next_batch_url()
            return
        self.fetching_changed.emit(False)
        self.cancel_visible.emit(False)

    def _on_fetch_error(self, err: object, worker=None) -> None:
        if worker is not None and worker is not self._fetch_worker:
            return
        from error_handler import ErrorInfo
        if isinstance(err, ErrorInfo):
            msg = err.raw or err.detail
        else:
            msg = str(err)
        self._fetch_worker = None
        if self._batch_active:
            logger.warning("[BatchImport] URL failed; continuing: %s", msg)
            self._batch_failed += 1
            self._start_next_batch_url()
            return
        self.fetching_changed.emit(False)
        self.cancel_visible.emit(False)
        self.fetch_error.emit(msg)

    def _start_next_batch_url(self) -> None:
        from ui.i18n import t

        if not self._batch_active:
            return
        if self._batch_cancel_requested:
            self._finish_batch(cancelled=True)
            return
        if not self._batch_urls:
            self._finish_batch(cancelled=False)
            return
        completed = self._batch_succeeded + self._batch_failed
        url = self._batch_urls.pop(0)
        self.status_update.emit(t(
            "batch_import_progress", current=completed + 1, total=self._batch_total,
        ))
        self._start_fetch(url)

    def _finish_batch(self, *, cancelled: bool) -> None:
        from ui.i18n import t

        remaining = max(
            0, self._batch_total - self._batch_succeeded - self._batch_failed,
        ) if cancelled else 0
        key = "batch_import_cancelled" if cancelled else "batch_import_complete"
        self.temporary_status.emit(t(
            key,
            success=self._batch_succeeded,
            failed=self._batch_failed,
            skipped=self._batch_skipped,
            remaining=remaining,
        ))
        self._reset_batch_state()
        self.fetching_changed.emit(False)
        self.cancel_visible.emit(False)

    def _reset_batch_state(self) -> None:
        self._batch_urls = []
        self._batch_total = 0
        self._batch_succeeded = 0
        self._batch_failed = 0
        self._batch_skipped = 0
        self._batch_active = False
        self._batch_cancel_requested = False

