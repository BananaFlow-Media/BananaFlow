"""Orchestrate category selection and duplicate review for artist imports."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QWidget

from config import AppConfig
from core.artist_catalog import apply_catalog_decisions, detect_catalog_duplicates
from core.playlist_parser import SourcePlatform
from ui.i18n import t


class ArtistFlowController(QObject):
    tracks_ready = Signal(list)
    status_update = Signal(str)
    finished = Signal()
    cancelled = Signal()

    def __init__(
        self,
        artist_url: str,
        platform: SourcePlatform,
        config: AppConfig,
        parent_widget: QWidget,
        parent: QObject = None,
    ) -> None:
        super().__init__(parent)
        self._url = artist_url
        self._platform = platform
        self._cfg = config
        self._widget = parent_widget
        self.artist_name = ""

    def run(self) -> None:
        from ui.dialogs.artist_catalog_dialog import ArtistCatalogDialog

        self.status_update.emit(t("artist_flow_discovering"))
        dialog = ArtistCatalogDialog(
            self._url,
            self._platform,
            locale="he-IL" if self._cfg.language == "he" else "en-US",
            cookies_file=self._cfg.cookies_file,
            parent=self._widget,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._cancel()
            return

        self.artist_name = dialog.artist_name
        tracks = dialog.tracks
        groups = detect_catalog_duplicates(tracks)
        if groups:
            from ui.dialogs.catalog_conflict_dialog import CatalogConflictDialog

            self.status_update.emit(t("artist_flow_duplicates", n=len(groups)))
            conflicts = CatalogConflictDialog(groups, tracks, parent=self._widget)
            if conflicts.exec() != QDialog.DialogCode.Accepted:
                self._cancel()
                return
            tracks = apply_catalog_decisions(tracks, groups, conflicts.decisions)

        self.status_update.emit(t("artist_flow_adding", n=len(tracks)))
        self.tracks_ready.emit(tracks)
        self.finished.emit()

    def _cancel(self) -> None:
        self.status_update.emit(t("artist_flow_cancelled"))
        self.cancelled.emit()
        self.finished.emit()
