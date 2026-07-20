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

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, url: str, channel_tabs: Optional[list[str]] = None) -> None:
        """Start a FetchWorker for the given URL."""
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

        self._fetch_worker = FetchWorker(
            url,
            cookies_file=self._cfg.cookies_file,
            proxy_url=self._cfg.proxy_server_url,
            proxy_token=self._cfg.spotify_app_api_key,
            channel_tabs=channel_tabs,
            parent=self,
        )
        self._fetch_worker.track_found.connect(self._on_track_meta)
        self._fetch_worker.finished.connect(self._on_fetch_finished)
        self._fetch_worker.error.connect(self._on_fetch_error)
        self._fetch_worker.start()

    def cancel(self) -> None:
        """Cancel any in-flight fetch or scrape."""
        if self._fetch_worker and self._fetch_worker.isRunning():
            self._fetch_worker.cancel()
            self.fetching_changed.emit(False)
        if self._scraper_worker and self._scraper_worker.isRunning():
            self._scraper_worker.cancel()
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
        """Import URLs from a text file and fetch the first one."""
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

        self.status_update.emit(result.summary())
        self.fetch(result.urls[0])

    # ── Private slots ─────────────────────────────────────────────────────────

    def _on_track_meta(self, meta, index: int, total: int) -> None:
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

    def _on_fetch_finished(self, result) -> None:
        self.fetching_changed.emit(False)
        self.cancel_visible.emit(False)
        self.fetch_finished.emit(result)

    def _on_fetch_error(self, err: object) -> None:
        self.fetching_changed.emit(False)
        self.cancel_visible.emit(False)
        from error_handler import ErrorInfo
        if isinstance(err, ErrorInfo):
            msg = err.raw or err.detail
        else:
            msg = str(err)
        self.fetch_error.emit(msg)

