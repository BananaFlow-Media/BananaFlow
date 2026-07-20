"""
ui/dialogs/tab_select_dialog.py  –  Channel tab selection dialog (Card Grid B)
================================================================================
Three-state UX:
  1. DISCOVERING — spinner + "מגלה טאבים…"
  2. READY       — card grid of discovered tabs, each toggleable
  3. SCANNING    — progress bar while ChannelScrapeWorker runs

The dialog is modal and returns via exec().  After exec() returns Accepted, the
caller can read .selected_tabs (list[TabInfo]) and .scrape_results
(dict[tab_name → list[VideoInfo]]).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QVBoxLayout, QGridLayout,
    QLabel, QFrame, QSizePolicy, QScrollArea, QWidget,
    QProgressBar,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, PrimaryPushButton, PushButton, SubtitleLabel,
)

from core.channel_tab_discoverer import TabInfo, DiscoveryResult
from core.duplicate_detector import VideoInfo
from ui.dialogs.styled_dialog import StyledDialog, make_footer, set_button_role
from ui.i18n import t
# ── Discovery QThread ─────────────────────────────────────────────────────────

class _DiscoveryWorker(QThread):
    finished = Signal(object)   # DiscoveryResult

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        from core.channel_tab_discoverer import discover_tabs
        result = discover_tabs(self._url)
        self.finished.emit(result)


# ── Tab toggle card ───────────────────────────────────────────────────────────

class _TabCard(QFrame):
    toggled = Signal(bool)

    def __init__(self, tab: TabInfo, parent=None) -> None:
        super().__init__(parent)
        self._tab     = tab
        self._checked = True
        self._build()

    @property
    def tab(self) -> TabInfo:
        return self._tab

    @property
    def is_checked(self) -> bool:
        return self._checked

    def _build(self) -> None:
        self.setFixedSize(148, 110)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("checked", self._checked)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._icon_lbl = QLabel(self._tab.icon)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_lbl.setObjectName("tabCardIcon")

        self._name_lbl = BodyLabel(self._tab.name)
        self._name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_lbl.setObjectName("tabCardName")

        self._count_lbl = CaptionLabel("")
        self._count_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count_lbl.setObjectName("tabCardCount")
        self._count_lbl.hide()

        self._check_lbl = QLabel("✓")
        self._check_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._check_lbl.setObjectName("tabCardCheck")

        lay.addWidget(self._icon_lbl)
        lay.addWidget(self._name_lbl)
        lay.addWidget(self._count_lbl)
        lay.addStretch()
        lay.addWidget(self._check_lbl)

    def _refresh_style(self) -> None:
        self.setProperty("checked", self._checked)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_count(self, n: int) -> None:
        if n >= 0:
            self._count_lbl.setText(t("import_channel_items_count", n=n))
            self._count_lbl.show()

    def mousePressEvent(self, event) -> None:
        self._checked = not self._checked
        self._check_lbl.setVisible(self._checked)
        self._refresh_style()
        self.toggled.emit(self._checked)
        super().mousePressEvent(event)


# ── Main dialog ───────────────────────────────────────────────────────────────

class TabSelectDialog(StyledDialog):
    """
    Modal dialog for channel tab selection + scraping.

    After exec() returns QDialog.DialogCode.Accepted:
      .selected_tabs   → list[TabInfo] the user confirmed
      .scrape_results  → dict[str, list[VideoInfo]]  (populated after scraping)
      .channel_name    → str
    """

    def __init__(
        self,
        channel_url:  str,
        cookies_file: Optional[str] = None,
        proxy_url:    Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent, minimum_size=(520, 360))
        self._url         = channel_url
        self._cookies     = cookies_file
        self._proxy       = proxy_url
        self._cards:      list[_TabCard] = []
        self._discovery:  Optional[_DiscoveryWorker] = None
        self._scraper:    Optional[QThread] = None

        # Public output
        self.selected_tabs:  list[TabInfo]               = []
        self.scrape_results: dict[str, list[VideoInfo]]  = {}
        self.channel_name:   str                         = ""

        self._build()
        self._start_discovery()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setWindowTitle(t("import_channel_title"))
        self.setMinimumWidth(520)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        # Title
        self._title = SubtitleLabel(t("import_channel_title"))
        self._title.setObjectName("dialogMainTitle")
        root.addWidget(self._title)

        # Subtitle / status line
        self._subtitle = CaptionLabel(t("import_channel_discovering"))
        self._subtitle.setObjectName("dialogMainDesc")
        root.addWidget(self._subtitle)

        # Spinner label (shown during discovery)
        self._spinner = QLabel("⏳")
        self._spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spinner.setObjectName("dialogSpinner")
        root.addWidget(self._spinner)

        # Card grid (hidden until discovery finishes)
        self._grid_widget = QWidget()
        self._grid_widget.setObjectName("dialogGridWidget")
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setSpacing(10)
        self._grid_widget.hide()
        root.addWidget(self._grid_widget)

        # Progress bar (hidden until scanning)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setObjectName("dialogProgressBar")
        self._progress_bar.hide()
        root.addWidget(self._progress_bar)

        self._progress_label = CaptionLabel("")
        self._progress_label.setObjectName("dialogProgressLabel")
        self._progress_label.hide()
        root.addWidget(self._progress_label)

        # Buttons
        self._cancel_btn = PushButton(t("import_channel_cancel"))
        self._scan_btn   = PrimaryPushButton(t("import_channel_scan_selected"))
        set_button_role(self._cancel_btn, "cancel")
        set_button_role(self._scan_btn, "primary", default=True)
        self._scan_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.reject)
        self._scan_btn.clicked.connect(self._on_scan_clicked)
        root.addWidget(make_footer(self._cancel_btn, self._scan_btn))

        # Animate the spinner
        self._dot_count = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._tick_spinner)
        self._spin_timer.start(600)

    # ── Discovery ──────────────────────────────────────────────────────────────

    def _start_discovery(self) -> None:
        self._discovery = _DiscoveryWorker(self._url, parent=self)
        self._discovery.finished.connect(self._on_discovery_done)
        self._discovery.start()

    def _on_discovery_done(self, result: DiscoveryResult) -> None:
        self._spin_timer.stop()
        self._spinner.hide()
        self.channel_name = result.channel_name

        if result.error and not result.tabs:
            self._subtitle.setText(t("import_channel_error_prefix", error=result.error))
            self._scan_btn.setEnabled(False)
            return

        if result.channel_name:
            self._title.setText(t("import_channel_with_name", name=result.channel_name))

        # A degraded result means the tab list is a hardcoded guess and/or the
        # channel name could not be read (issue #27). It is still usable, so we
        # populate the grid — but the user has to be told the list may not match
        # the channel, instead of it looking like a confident discovery.
        notes = []
        if result.error:
            notes.append(result.error)
        if result.degraded:
            notes.append(t("import_channel_degraded_warning"))
        self._subtitle.setText(
            t("import_channel_tabs_found", n=len(result.tabs))
            + "".join(f"\n⚠ {note}" for note in notes)
        )

        self._populate_grid(result.tabs)
        self._grid_widget.show()
        self._scan_btn.setEnabled(True)
        self.adjustSize()

    def _populate_grid(self, tabs: list[TabInfo]) -> None:
        cols = 3
        for i, tab in enumerate(tabs):
            card = _TabCard(tab)
            card.toggled.connect(self._on_card_toggled)
            self._cards.append(card)
            self._grid_layout.addWidget(card, i // cols, i % cols)

    def _on_card_toggled(self, _: bool) -> None:
        any_on = any(c.is_checked for c in self._cards)
        self._scan_btn.setEnabled(any_on)

    def _tick_spinner(self) -> None:
        frames = ["⏳", "⌛"]
        self._dot_count = (self._dot_count + 1) % len(frames)
        self._spinner.setText(frames[self._dot_count])

    # ── Scanning ───────────────────────────────────────────────────────────────

    def _on_scan_clicked(self) -> None:
        from ui.workers.channel_scrape_worker import ChannelScrapeWorker

        self.selected_tabs = [c.tab for c in self._cards if c.is_checked]
        if not self.selected_tabs:
            return

        self._scan_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._subtitle.setText(t("import_channel_scanning_selected"))
        self._progress_bar.show()
        self._progress_label.show()

        total_tabs = len(self.selected_tabs)
        self._tabs_done = 0
        self._total_tabs = total_tabs

        self._scraper = ChannelScrapeWorker(
            channel_url=self._url,
            selected_tabs=self.selected_tabs,
            cookies_file=self._cookies,
            proxy_url=self._proxy,
            parent=self,
        )
        self._scraper.tab_started.connect(self._on_tab_started)
        self._scraper.tab_progress.connect(self._on_tab_progress)
        self._scraper.tab_done.connect(self._on_tab_done)
        self._scraper.all_done.connect(self._on_all_done)
        self._scraper.error.connect(self._on_scrape_error)
        self._scraper.start()

    def _on_tab_started(self, tab_name: str) -> None:
        self._progress_label.setText(t("import_channel_scanning_tab", tab=tab_name))

    def _on_tab_progress(self, tab_name: str, current: int, total: int) -> None:
        self._progress_label.setText(t("import_channel_expanding_playlists", current=current, total=total))
        if total > 0:
            pct = int(self._tabs_done / self._total_tabs * 100
                      + current / total * (100 / self._total_tabs))
            self._progress_bar.setValue(min(pct, 99))

    def _on_tab_done(self, tab_name: str, count: int) -> None:
        self._tabs_done += 1
        # Update the count label on the matching card
        for card in self._cards:
            if card.tab.name == tab_name:
                card.set_count(count)
                break
        pct = int(self._tabs_done / self._total_tabs * 100)
        self._progress_bar.setValue(pct)

    def _on_all_done(self, results: dict) -> None:
        self.scrape_results = results
        total_items = sum(len(v) for v in results.values())
        self._progress_bar.setValue(100)
        self._progress_label.setText(t("import_channel_scan_complete", n=total_items))
        self.accept()

    def _on_scrape_error(self, msg: str) -> None:
        self._subtitle.setText(t("import_channel_scrape_error", msg=msg))
        self._scan_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        self._progress_bar.hide()
        self._progress_label.hide()

    # ── Close guard ───────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._discovery and self._discovery.isRunning():
            self._discovery.quit()
        if self._scraper and self._scraper.isRunning():
            self._scraper.cancel()
            self._scraper.quit()
        super().closeEvent(event)
