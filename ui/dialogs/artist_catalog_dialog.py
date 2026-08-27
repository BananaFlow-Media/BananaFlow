"""Category discovery and selection dialog for music-service artist URLs."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, PrimaryPushButton, PushButton, SubtitleLabel

from core.artist_catalog import ArtistCatalogDiscovery, ArtistCatalogSection
from core.playlist_parser import SourcePlatform
from ui.dialogs.styled_dialog import StyledDialog, make_footer, set_button_role
from ui.i18n import t
from ui.workers.artist_catalog_worker import (
    ArtistCatalogDiscoveryWorker,
    ArtistCatalogScrapeWorker,
)


def _section_name(key: str) -> str:
    return t(f"artist_section_{key}")


class _SectionCard(QFrame):
    def __init__(self, section: ArtistCatalogSection, parent=None) -> None:
        super().__init__(parent)
        self.section = section
        self.setObjectName("artistSectionCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        icon = QLabel(section.icon)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setObjectName("tabCardIcon")
        self.checkbox = QCheckBox(_section_name(section.key))
        self.checkbox.setChecked(True)
        self.checkbox.setAccessibleName(_section_name(section.key))
        layout.addWidget(icon)
        layout.addWidget(self.checkbox)
        if section.item_count >= 0:
            count = CaptionLabel(t("import_artist_items_count", n=section.item_count))
            count.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(count)


class ArtistCatalogDialog(StyledDialog):
    """Discover categories, optionally ask for selection, then scrape them."""

    def __init__(
        self,
        artist_url: str,
        platform: SourcePlatform,
        *,
        locale: str = "en-US",
        cookies_file: Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent, minimum_size=(520, 360), resize_to=(620, 500))
        self._url = artist_url
        self._platform = platform
        self._locale = locale
        self._cookies_file = cookies_file
        self._discovery_worker: Optional[ArtistCatalogDiscoveryWorker] = None
        self._scrape_worker: Optional[ArtistCatalogScrapeWorker] = None
        self._cancel_after_discovery = False
        self._cards: list[_SectionCard] = []
        self.discovery: Optional[ArtistCatalogDiscovery] = None
        self.selected_keys: list[str] = []
        self.tracks: list[dict] = []
        self.artist_name = ""
        self._build()
        self._start_discovery()

    def _build(self) -> None:
        self.setWindowTitle(t("import_artist_title"))
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        self._title = SubtitleLabel(t("import_artist_title"))
        self._title.setObjectName("dialogMainTitle")
        root.addWidget(self._title)
        self._status = CaptionLabel(t("import_artist_discovering"))
        self._status.setWordWrap(True)
        self._status.setObjectName("dialogMainDesc")
        root.addWidget(self._status)
        self._spinner = QLabel("⏳")
        self._spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._spinner)

        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(10)
        self._grid_widget.hide()
        root.addWidget(self._grid_widget)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.hide()
        root.addWidget(self._progress)

        self._cancel_btn = PushButton(t("cancel_btn"))
        self._scan_btn = PrimaryPushButton(t("import_artist_scan_selected"))
        set_button_role(self._cancel_btn, "cancel")
        set_button_role(self._scan_btn, "primary", default=True)
        self._scan_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._request_cancel)
        self._scan_btn.clicked.connect(self._start_scan)
        root.addWidget(make_footer(self._cancel_btn, self._scan_btn))

    def _start_discovery(self) -> None:
        self._discovery_worker = ArtistCatalogDiscoveryWorker(
            self._url,
            self._platform,
            locale=self._locale,
            parent=self,
        )
        self._discovery_worker.completed.connect(self._on_discovered)
        self._discovery_worker.start()

    def _on_discovered(self, discovery: ArtistCatalogDiscovery) -> None:
        if self._cancel_after_discovery:
            self.reject()
            return
        self.discovery = discovery
        self.artist_name = discovery.artist_name
        self._spinner.hide()
        if discovery.error:
            self._status.setText(t("import_artist_error_prefix", error=discovery.error))
            return
        if discovery.artist_name:
            self._title.setText(t("import_artist_with_name", name=discovery.artist_name))
        if len(discovery.sections) == 1:
            self.selected_keys = [discovery.sections[0].key]
            self._status.setText(t("import_artist_one_section", section=_section_name(
                discovery.sections[0].key,
            )))
            self._start_scan()
            return

        self._status.setText(t("import_artist_sections_found", n=len(discovery.sections)))
        for index, section in enumerate(discovery.sections):
            card = _SectionCard(section)
            card.checkbox.toggled.connect(self._refresh_scan_enabled)
            self._cards.append(card)
            self._grid.addWidget(card, index // 3, index % 3)
        self._grid_widget.show()
        self._refresh_scan_enabled()

    def _refresh_scan_enabled(self) -> None:
        self._scan_btn.setEnabled(any(card.checkbox.isChecked() for card in self._cards))

    def _start_scan(self) -> None:
        if not self.discovery:
            return
        if self._cards:
            self.selected_keys = [
                card.section.key for card in self._cards if card.checkbox.isChecked()
            ]
        if not self.selected_keys:
            return
        self._scan_btn.setEnabled(False)
        self._grid_widget.hide()
        self._spinner.hide()
        self._progress.show()
        self._status.setText(t("import_artist_scanning"))
        self._scrape_worker = ArtistCatalogScrapeWorker(
            self.discovery,
            self.selected_keys,
            locale=self._locale,
            cookies_file=self._cookies_file,
            parent=self,
        )
        self._scrape_worker.section_started.connect(self._on_section_started)
        self._scrape_worker.completed.connect(self._on_scan_complete)
        self._scrape_worker.failed.connect(self._on_scan_failed)
        self._scrape_worker.cancelled.connect(self.reject)
        self._scrape_worker.start()

    def _on_section_started(self, key: str) -> None:
        self._status.setText(t("import_artist_scanning_section", section=_section_name(key)))

    def _on_scan_complete(self, tracks: list[dict]) -> None:
        self.tracks = tracks
        self._progress.hide()
        self._status.setText(t("import_artist_scan_complete", n=len(tracks)))
        self.accept()

    def _on_scan_failed(self, message: str) -> None:
        self._progress.hide()
        self._status.setText(t("import_artist_scrape_error", msg=message))
        self._cancel_btn.setEnabled(True)
        self._scan_btn.setEnabled(True)

    def _request_cancel(self) -> None:
        if self._discovery_worker and self._discovery_worker.isRunning():
            self._cancel_after_discovery = True
            self._cancel_btn.setEnabled(False)
            self._status.setText(t("import_artist_cancelling"))
            return
        if self._scrape_worker and self._scrape_worker.isRunning():
            self._scrape_worker.cancel()
            self._cancel_btn.setEnabled(False)
            self._status.setText(t("import_artist_cancelling"))
            return
        self.reject()

    def closeEvent(self, event) -> None:
        if self._discovery_worker and self._discovery_worker.isRunning():
            self._cancel_after_discovery = True
            event.ignore()
            self._cancel_btn.setEnabled(False)
            self._status.setText(t("import_artist_cancelling"))
            return
        if self._scrape_worker and self._scrape_worker.isRunning():
            self._scrape_worker.cancel()
            event.ignore()
            self._cancel_btn.setEnabled(False)
            self._status.setText(t("import_artist_cancelling"))
            return
        super().closeEvent(event)
