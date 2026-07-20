"""
ui/workers/scraper_worker.py  –  Deep page scraper worker
==========================================================
Runs PageScraper.scrape() on a background thread, emitting each discovered
media URL as it is found so the queue panel can populate incrementally while
the page is still being scanned.

Signal summary
--------------
url_found(str)    Emitted per validated media URL as soon as it is discovered.
status_msg(str)   Status bar text (scanning, done, or warning).
finished(int)     Total unique media URLs found (emitted once at the end).
error(str)        Human-readable error message on irrecoverable failure.

Threading model
---------------
One ScraperWorker is created per scrape request.  Cancel mid-scan by calling
worker.cancel() which sets a threading.Event inside PageScraper checked between
the tag-scan and regex-scan phases, and between individual URL validations.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.search_engine import PageScraper, ScraperError


class ScraperWorker(QThread):
    """
    Background page-scraping thread.

    Parameters
    ----------
    page_url     : The webpage URL to fetch and scan for embedded media links.
    cookies_file : Optional path to a Netscape cookies.txt for authenticated requests.
    timeout      : HTTP request timeout in seconds (default 20.0).
    parent       : Optional Qt parent object.
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    url_found  = Signal(str)    # One validated media URL per emission
    status_msg = Signal(str)    # Status bar text
    finished   = Signal(int)    # Total unique URLs found
    error      = Signal(str)    # Human-readable failure message

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        page_url:     str,
        cookies_file: Optional[str] = None,
        timeout:      float = 20.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._page_url = page_url
        self._cookies  = cookies_file
        self._timeout  = timeout
        self._scraper  = PageScraper()

    def cancel(self) -> None:
        """
        Thread-safe cancellation.
        Delegates to PageScraper.cancel() which sets a threading.Event
        checked between scraping phases.
        """
        self._scraper.cancel()

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Entry point executed on the worker thread."""
        # Truncate the URL for display without leaking long query strings
        display_url = self._page_url
        if len(display_url) > 60:
            display_url = display_url[:57] + "…"

        self.status_msg.emit(f"🕷  Scanning page for media links…  ({display_url})")

        found_urls: list[str] = []

        def on_url_found(url: str) -> None:
            """Called by PageScraper for each validated media URL."""
            found_urls.append(url)
            self.url_found.emit(url)

        try:
            all_urls = self._scraper.scrape(
                self._page_url,
                on_url_found=on_url_found,
                on_status=lambda msg: self.status_msg.emit(msg),
                cookies_file=self._cookies,
                timeout=self._timeout,
            )

            count = len(all_urls)
            if count == 0:
                self.status_msg.emit(
                    "⚠  No downloadable videos found on this page."
                )
            else:
                self.status_msg.emit(
                    f"✅  Page scan complete — "
                    f"{count} video{'s' if count != 1 else ''} found."
                )

            self.finished.emit(count)

        except ScraperError as exc:
            self.error.emit(str(exc))

        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Unexpected scraper error: {exc}")
