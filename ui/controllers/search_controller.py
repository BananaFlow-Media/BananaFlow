"""
ui/controllers/search_controller.py
=====================================
Manages the search flow (YouTube / Spotify / generic).
Owns the SearchWorker lifecycle.

Communicates exclusively via Qt signals — zero direct panel references.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal

from config import AppConfig
from core.search_engine import SearchResult

logger = logging.getLogger(__name__)


class SearchController(QObject):
    """
    Owns the search flow.

    Signals
    -------
    result_ready      : SearchResult — AppWindow → search_panel.add_result() + thumbnail
    result_to_queue   : TrackMeta — AppWindow → _add_track_to_queue() + switch to queue tab
    search_error      : str — AppWindow shows error dialog
    searching_changed : bool — → search_panel.set_searching()
    """

    result_ready      = Signal(object)   # SearchResult
    result_to_queue   = Signal(object)   # TrackMeta
    search_error      = Signal(str)
    searching_changed = Signal(bool)

    def __init__(self, config: AppConfig, parent: QObject = None) -> None:
        super().__init__(parent)
        self._cfg = config
        self._search_worker: Optional = None

    # ── Public API ────────────────────────────────────────────────────────────

    def search(self, query: str, platform) -> None:
        """Launch a SearchWorker for the given query and platform."""
        from ui.workers.search_worker import SearchWorker

        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.cancel()

        self._search_worker = SearchWorker(
            query=query,
            platform=platform,
            youtube_max_results=self._cfg.youtube_max_results,
            spotify_max_results=self._cfg.spotify_max_results,
            cookies_file=self._cfg.cookies_file,
            spotify_client_id=self._cfg.spotify_app_api_key,
            spotify_client_secret="",
            proxy_url=self._cfg.proxy_server_url,
            proxy_token=self._cfg.spotify_app_api_key,
            parent=self,
        )
        self._search_worker.result_ready.connect(self.result_ready)
        self._search_worker.finished.connect(lambda: self.searching_changed.emit(False))
        self._search_worker.error.connect(self.search_error)
        self.searching_changed.emit(True)
        self._search_worker.start()

    def cancel(self) -> None:
        """Cancel any in-flight search."""
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.cancel()

    def add_to_queue(self, result: SearchResult) -> None:
        """
        Convert a SearchResult to TrackMeta and signal AppWindow to add it to
        the download queue.  AppWindow also switches to the queue tab.
        """
        from core.playlist_parser import TrackMeta

        meta = TrackMeta(
            title=result.title,
            artist=result.artist,
            url=result.url,
            duration_str=result.duration_str,
            thumbnail_url=result.thumbnail_url,
            platform=result.platform,
            album=result.album,
            release_type=result.release_type,
        )
        self.result_to_queue.emit(meta)
