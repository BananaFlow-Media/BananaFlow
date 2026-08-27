"""Background workers for staged Spotify/YouTube Music artist imports."""

from __future__ import annotations

import threading
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

from core.artist_catalog import (
    ArtistCatalogDiscovery,
    discover_artist_catalog,
    scrape_artist_catalog,
)
from core.playlist_parser import SourcePlatform


class ArtistCatalogDiscoveryWorker(QThread):
    completed = Signal(object)  # ArtistCatalogDiscovery

    def __init__(
        self,
        url: str,
        platform: SourcePlatform,
        *,
        locale: str = "en-US",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._platform = platform
        self._locale = locale

    def run(self) -> None:
        self.completed.emit(discover_artist_catalog(
            self._url, self._platform, locale=self._locale,
        ))


class ArtistCatalogScrapeWorker(QThread):
    section_started = Signal(str)
    completed = Signal(object)  # list[dict]
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        discovery: ArtistCatalogDiscovery,
        selected_keys: Iterable[str],
        *,
        locale: str = "en-US",
        cookies_file: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._discovery = discovery
        self._selected_keys = tuple(selected_keys)
        self._locale = locale
        self._cookies_file = cookies_file
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            tracks = scrape_artist_catalog(
                self._discovery,
                self._selected_keys,
                locale=self._locale,
                cookies_file=self._cookies_file,
                cancel_check=self._cancel_event.is_set,
                on_section=self.section_started.emit,
            )
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.completed.emit(tracks)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal UI
            self.failed.emit(str(exc) or exc.__class__.__name__)
