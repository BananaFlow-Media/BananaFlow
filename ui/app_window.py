"""
ui/app_window.py  –  Main application window  (v4 — controller architecture)
==============================================================================
Changelog v4
------------
* Decomposed into three controllers (P3-4):
    - FetchController  : fetch / scrape / batch-import flows
    - SearchController : search flows (YouTube, Spotify)
    - DownloadController: download / pause / resume / job-building
  AppWindow is now the pure mediator: it owns panels, wires signals between
  controllers and panels, and handles strictly UI-level concerns (tray, drag
  & drop, accessibility, clipboard, close event, queue card management).

All v3 functionality preserved unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QSystemTrayIcon, QMenu, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    FluentIcon, FluentWindow,
    NavigationItemPosition,
    InfoBar, InfoBarPosition,
    FluentIconBase, Theme, isDarkTheme,
)

# ── Backend ────────────────────────────────────────────────────────────────────
from config import AppConfig
from core.history_db import DownloadRecord, HistoryDB
from core.services import ServiceContainer
from core.search_engine import SearchResult, ResultKind
from core.update_state import UpdateStateStore, app_update_id, component_update_id
from ui.workers.offline_monitor import OfflineMonitor
from ui.touch import apply_touch_density_sizes, apply_touch_support
from core.downloader import AudioQuality, DownloadEngine, DownloadRequest, MediaType, VideoQuality
from core.playlist_parser import (
    ParseResult, SourcePlatform, UrlKind, classify_url,
    is_malformed_url_attempt, looks_like_url,
)
from error_handler import classify_error, ErrorInfo, ErrorSeverity, probe_connectivity

# ── Controllers ────────────────────────────────────────────────────────────────
from ui.controllers.fetch_controller    import FetchController
from ui.controllers.search_controller  import SearchController
from ui.controllers.download_controller import DownloadController

# ── Workers ────────────────────────────────────────────────────────────────────
from ui.workers.thumbnail_worker import ThumbnailWorker
from ui.workers.clipboard_worker import ClipboardWorker
if TYPE_CHECKING:
    from ui.workers.update_worker import UpdateCheckResults

# ── Panels ─────────────────────────────────────────────────────────────────────
from ui.panels.url_bar              import UrlBar
from ui.panels.search_panel         import SearchPanel
from ui.panels.queue_panel          import QueuePanel
from ui.panels.history_panel        import HistoryPanel
from ui.panels.options_bar          import OptionsBar
from ui.panels.status_bar           import StatusBar, StatusState
if TYPE_CHECKING:
    from ui.panels.settings_panel import SettingsPanel
from ui.panels.converter_panel      import ConverterPanel
if TYPE_CHECKING:
    from ui.panels.metadata_editor_panel import MetadataEditorPanel

# ── Tag-editor controller ──────────────────────────────────────────────────────
from ui.controllers.metadata_controller import MetadataController

# ── Components ─────────────────────────────────────────────────────────────────
from ui.components.track_card     import TrackCard
from ui.components.offline_banner import OfflineBanner
from ui.components.status_icon    import StatusKind
from core.batch_outcome           import BatchOutcome
from core.download_recovery       import (
    RecoveryDecision,
    UserActionRequest,
    shared_download_recovery_coordinator,
)

# ── Theme / i18n ───────────────────────────────────────────────────────────────
from ui.i18n         import current_language, t, request_language_restart
from ui.dialogs.styled_dialog import (
    StyledMessageDialog,
    confirm,
    get_text,
    show_info,
    show_warning,
)
from ui.dialogs.update_prompt_dialog import UpdatePromptDialog
from ui.dialogs.cookie_auth_dialog import ManualCookieImportDialog, ask_cookie_auth_choice
from ui.theme_manager import ThemeManager, get_colors

logger = logging.getLogger(__name__)

_SOURCE_ALTERNATIVE_DIALOG_RESULT = 2


def _dim_hex(hex_color: str, factor: float = 0.85) -> str:
    """Return a darkened/dimmed variant of a hex color for hover states."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r = max(0, int(int(h[0:2], 16) * factor))
    g = max(0, int(int(h[2:4], 16) * factor))
    b = max(0, int(int(h[4:6], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


# ── High-contrast QSS for accessibility mode ──────────────────────────────────
# Rules here must NOT use `!important` — Qt Style Sheets do not support it
# (the declaration is silently dropped), which is exactly how a previous
# version of this stylesheet ended up doing nothing. ThemeManager applies
# this overlay *in place of* the decorative theme QSS (high contrast is
# meant to replace the theme), so plain type selectors win uncontested.
#
# The base background is #0d0d12 (near-black) rather than pure #000000 on
# purpose: ui/dialogs/styled_dialog.py recognises that value as a dark-theme
# marker, so app dialogs pick up the high-contrast style from their parent
# window instead of falling back to the decorative theme.
_A11Y_QSS = """
QWidget { background-color: #0d0d12; color: #ffffff; }
QDialog { background-color: #0d0d12; color: #ffffff; }
QFrame  { background-color: #0d0d12; border: 1px solid #ffff00; }
QLabel  { color: #ffffff; background: transparent; border: none; }
QPushButton, QToolButton {
    background-color: #111111; color: #ffff00;
    border: 2px solid #ffff00; padding: 4px 10px;
}
QPushButton:hover, QToolButton:hover { background-color: #24240a; }
QPushButton:focus, QToolButton:focus { border: 3px solid #00ffff; }
QPushButton:disabled, QToolButton:disabled { color: #808080; border-color: #808080; }
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background-color: #111111; color: #ffffff; border: 2px solid #ffff00;
    selection-background-color: #ffff00; selection-color: #000000;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QTextEdit:focus, QPlainTextEdit:focus { border: 3px solid #00ffff; }
QComboBox QAbstractItemView {
    background-color: #111111; color: #ffffff; border: 2px solid #ffff00;
    selection-background-color: #ffff00; selection-color: #000000;
}
QCheckBox, QRadioButton { color: #ffffff; background: transparent; }
QCheckBox:focus, QRadioButton:focus { background-color: #24240a; }
QTableView, QTreeView, QListView, QTableWidget, QTreeWidget, QListWidget {
    background-color: #0d0d12; color: #ffffff; border: 1px solid #ffff00;
    alternate-background-color: #111111; gridline-color: #808000;
    selection-background-color: #ffff00; selection-color: #000000;
}
QHeaderView::section {
    background-color: #111111; color: #ffff00; border: 1px solid #ffff00;
    padding: 4px;
}
QProgressBar {
    background-color: #111111; color: #ffffff; border: 2px solid #ffff00;
    text-align: center;
}
QProgressBar::chunk { background-color: #ffff00; }
QToolTip { background-color: #0d0d12; color: #ffffff; border: 2px solid #ffff00; }
QScrollBar { background-color: #0d0d12; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background-color: #ffff00; }
QMenu { background-color: #0d0d12; color: #ffffff; border: 2px solid #ffff00; }
QMenu::item:selected { background-color: #ffff00; color: #000000; }
QMenu::item:disabled { color: #808080; }
"""


# ──────────────────────────────────────────────────────────────────────────────
# _DownloadBar  (inline widget — unchanged from v2/v3)
# ──────────────────────────────────────────────────────────────────────────────

class _DownloadBar(QFrame):
    from PySide6.QtCore import Signal as _Signal
    download_clicked = _Signal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QHBoxLayout, QLabel
        from qfluentwidgets import PrimaryPushButton

        self.setFixedHeight(36)
        self.setObjectName("downloadBar")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        row.addStretch()

        self._dl_btn = PrimaryPushButton(t("download_selected"))
        self._dl_btn.setIcon(FluentIcon.DOWNLOAD)
        self._dl_btn.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._dl_btn.setObjectName("downloadBarBtn")
        self._dl_btn.setFixedSize(176, 32)
        self._dl_btn.setEnabled(False)
        self._dl_btn.clicked.connect(self.download_clicked)
        row.addWidget(self._dl_btn)
        self._apply_theme()

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    def set_count(self, selected: int, total: int) -> None:
        self._dl_btn.setEnabled(total > 0 and selected > 0)

    def set_downloading(self, downloading: bool) -> None:
        self._dl_btn.setEnabled(not downloading)
        self._dl_btn.setText(
            t("download_downloading") if downloading else t("download_selected")
        )

    def _apply_theme(self) -> None:
        c = get_colors()
        dim = _dim_hex(c.accent)
        self.setStyleSheet("background: transparent; border: none;")
        self._dl_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background-color: {c.accent};
                color: #ffffff;
                border: none;
                border-radius: 9px;
                font-size: 13px;
                font-weight: 700;
                padding-left: 38px;
                padding-right: 14px;
            }}
            PrimaryPushButton:hover {{
                background-color: {dim};
            }}
            PrimaryPushButton:disabled {{
                background-color: {c.surface2};
                color: {c.text_tertiary};
            }}
        """)


class CustomIcon(FluentIconBase):
    """Custom Fluent Icon that points to light/dark SVG files in ui/assets."""

    def __init__(self, name: str) -> None:
        self.name = name

    def path(self, theme: Theme = Theme.AUTO) -> str:
        is_dark = isDarkTheme()
        if theme == Theme.DARK:
            is_dark = True
        elif theme == Theme.LIGHT:
            is_dark = False

        suffix = "white" if is_dark else "black"
        import os
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, "assets", f"{self.name}_{suffix}.svg")


# ──────────────────────────────────────────────────────────────────────────────
# AppWindow  (mediator — owns panels, wires controllers)
# ──────────────────────────────────────────────────────────────────────────────

class AppWindow(FluentWindow):
    """
    Top-level application window.

    Responsibilities (v4):
      * Build panels and controllers
      * Wire all Qt signals between controllers ↔ panels
      * Manage queue card lifecycle (_add_track_to_queue, thumbnails)
      * System tray, drag & drop, accessibility, hotkeys, close event
      * Cross-controller mediation (e.g. search drill-down → fetch)
    """

    def __init__(
        self,
        config:   AppConfig,
        services: "ServiceContainer",
        db:       Optional[HistoryDB] = None,
    ) -> None:
        super().__init__()

        # ── Core references ───────────────────────────────────────────────────
        self._cfg    = config
        self._svc    = services
        self._db     = services.db if db is None else db
        self._engine = services.engine
        self._theme  = ThemeManager(config)

        # Snapshot the language at startup so _on_settings_saved can detect
        # a change and prompt for a restart.
        self._previous_language: str = config.language

        # ── URL routing state (needed when building download jobs) ─────────────
        self._last_playlist_title: str               = ""
        self._last_url_kind:       Optional[UrlKind] = None

        # ── Queue card routing ────────────────────────────────────────────────
        self._index_to_card: dict[int, TrackCard]      = {}
        self._thumb_workers: set[ThumbnailWorker]      = set()

        # Most recent whole-batch snapshot (drives the finish summary counts).
        self._last_snapshot = None

        # Interactive repair of Spotify rows whose automatic YouTube match
        # produced no usable source. The search panel stays reusable: outside
        # this narrow mode its Add action behaves exactly as before.
        self._source_repair_incident_id = ""
        self._source_repair_key = ""
        self._source_repair_total = 0
        self._source_repair_completed = 0
        self._source_repair_jobs = []

        # ── Misc background workers ───────────────────────────────────────────
        self._clipboard_worker: Optional[ClipboardWorker] = None
        self._net_monitor:      Optional[OfflineMonitor]  = None
        self._tray:             Optional[QSystemTrayIcon] = None
        self._update_worker:    Optional["UpdateWorker"]  = None

        # ── Build ─────────────────────────────────────────────────────────────
        self._build_panels()
        self._url_bar.clear_url()
        self._build_controllers()
        self._configure_window()
        self._register_navigation()
        self._theme.apply(self._cfg.theme)
        if self._cfg.accessibility_mode:
            self._apply_accessibility(True)
        self._connect_signals()
        self._restore_state()
        self._setup_tray()
        self._setup_drag_drop()
        # One sweep over the finished widget tree makes every scroll area and
        # every context menu in the panels finger-usable. Runs after the panels
        # exist and after context-menu policies are set, because it reacts to
        # both. Dialogs sweep themselves — they are built on demand.
        apply_touch_support(self)
        # Toggling touch density emits theme_changed. Re-running the size pass
        # on that signal is what lets the setting apply without a restart —
        # the stylesheet half updates itself, this half cannot.
        self._theme.theme_changed.connect(lambda: apply_touch_density_sizes(self))

        QTimer.singleShot(300,  self._start_background_workers)
        QTimer.singleShot(450,  self._show_browser_cookie_migration_notice)
        # Reclaim abandoned download workspaces and restore paused jobs first,
        # so a paused batch is back in the queue before the queue-state resume
        # prompt and before the user can start anything new.
        QTimer.singleShot(900,  self._restore_paused_batches)
        QTimer.singleShot(1200, self._check_auto_resume)
        # Offer review-first recovery if a previous run crashed mid-Apply.
        QTimer.singleShot(1500, self._check_tag_apply_recovery)

    def _show_browser_cookie_migration_notice(self) -> None:
        """Explain a one-time Windows upgrade after persisting the safe value."""
        if not self._cfg.cookies_browser_migration_notice_pending:
            return
        show_info(
            self,
            t("browser_cookie_migrated_title"),
            t("browser_cookie_migrated_msg"),
        )
        self._cfg.cookies_browser_migration_notice_pending = False
        self._cfg.save()

    # ──────────────────────────────────────────────────────────────────────────
    # Panel construction
    # ──────────────────────────────────────────────────────────────────────────

    def _build_panels(self) -> None:
        self._url_bar         = UrlBar(self._cfg)
        self._options_bar     = OptionsBar(self._cfg)
        self._queue_panel     = QueuePanel()
        self._search_panel    = SearchPanel(self._cfg)
        self._history_panel   = HistoryPanel(self._db, self._cfg)
        self._status_bar      = StatusBar()
        self._offline_banner  = OfflineBanner()
        self._dl_bar          = _DownloadBar()
        self._converter_panel  = ConverterPanel()
        # The Tag Editor owns a very large widget tree. Keep a lightweight
        # navigation page now and construct the editor only when it is opened
        # (or when startup recovery genuinely needs to show it).
        self._metadata_panel: Optional["MetadataEditorPanel"] = None
        self._metadata_signals_connected = False
        self._metadata_page = QWidget()
        self._metadata_page.setObjectName("metadataEditorPage")
        self._metadata_page_layout = QVBoxLayout(self._metadata_page)
        self._metadata_page_layout.setContentsMargins(0, 0, 0, 0)
        self._metadata_loading_label = QLabel(t("tag_editor_loading"))
        self._metadata_loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._metadata_loading_label.setAccessibleName(t("tag_editor_loading"))
        self._metadata_page_layout.addWidget(self._metadata_loading_label)
        self._settings_panel: Optional["SettingsPanel"] = None
        self._settings_page = QWidget()
        self._settings_page.setObjectName("settingsPage")
        self._settings_page_layout = QVBoxLayout(self._settings_page)
        self._settings_page_layout.setContentsMargins(0, 0, 0, 0)
        self._settings_loading_label = QLabel(t("settings_loading"))
        self._settings_loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._settings_loading_label.setAccessibleName(t("settings_loading"))
        self._settings_page_layout.addWidget(self._settings_loading_label)

        # Queue composite wrapper
        queue_wrapper = QWidget()
        queue_wrapper.setObjectName("queuePage")
        queue_wrapper.setAcceptDrops(True)
        vl = QVBoxLayout(queue_wrapper)
        vl.setContentsMargins(20, 16, 20, 14)
        vl.setSpacing(10)
        vl.addWidget(self._offline_banner)

        self._queue_header = QFrame()
        self._queue_header.setObjectName("queueHeroHeader")
        header_layout = QVBoxLayout(self._queue_header)
        header_layout.setContentsMargins(0, 0, 0, 4)
        header_layout.setSpacing(3)
        self._queue_title = QLabel(t("downloads_header_title"))
        self._queue_title.setObjectName("queueHeroTitle")
        self._queue_subtitle = QLabel(t("downloads_header_subtitle"))
        self._queue_subtitle.setObjectName("queueHeroSubtitle")
        self._queue_subtitle.setWordWrap(True)
        header_layout.addWidget(self._queue_title)
        header_layout.addWidget(self._queue_subtitle)
        vl.addWidget(self._queue_header)

        vl.addWidget(self._url_bar)
        vl.addWidget(self._options_bar)

        vl.addWidget(self._queue_panel, stretch=1)

        footer_row = QHBoxLayout()
        footer_row.setSpacing(12)
        footer_row.addWidget(self._status_bar, stretch=1)
        footer_row.addWidget(self._dl_bar)
        vl.addLayout(footer_row)
        self._queue_wrapper = queue_wrapper
        self._theme.theme_changed.connect(self._apply_queue_page_theme)
        self._apply_queue_page_theme()

    def _apply_queue_page_theme(self) -> None:
        c = get_colors()
        self._queue_wrapper.setStyleSheet(f"""
            QWidget#queuePage {{
                background: {c.bg};
            }}
            QFrame#queueHeroHeader {{
                background: transparent;
                border: none;
            }}
            QLabel#queueHeroTitle {{
                color: {c.text_primary};
                background: transparent;
                font-size: 22px;
                font-weight: 800;
            }}
            QLabel#queueHeroSubtitle {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 12px;
            }}
        """)

    # ──────────────────────────────────────────────────────────────────────────
    # Controller construction
    # ──────────────────────────────────────────────────────────────────────────

    def _build_controllers(self) -> None:
        self._fetch_ctrl    = FetchController(self._cfg, parent=self)
        self._search_ctrl   = SearchController(self._cfg, parent=self)
        self._metadata_ctrl = MetadataController(self._cfg, parent=self)
        self._download_ctrl = DownloadController(
            config=self._cfg,
            engine=self._engine,
            db=self._db,
            parent=self,
        )
        # Fast-start match prefetch for two-stage Spotify catalogs: warms
        # yt-dlp plugins and pre-resolves the first few pending tracks into the
        # match cache while the catalog is on screen, so the first download
        # after a click starts without paying the match round-trip. Cancellable;
        # a single reused instance (see _on_fetch_finished / _start_fetch /
        # _on_download / closeEvent).
        from core.match_prefetcher import MatchPrefetcher
        self._match_prefetcher = MatchPrefetcher()
        # Per-fetch one-shot state: fire the 8-track prefetch the moment the
        # first 8 pending cards exist (mid-scrape), exactly once per fetch.
        self._prefetch_started = False
        self._prefetch_pending_count = 0

    # ──────────────────────────────────────────────────────────────────────────
    # FluentWindow configuration
    # ──────────────────────────────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self.setWindowTitle(t("app_name"))
        self.setMinimumSize(980, 680)
        self.resize(1100, 760)
        self.setObjectName("appWindow")
        self.stackedWidget.setObjectName("stackedWidget")
        self.navigationInterface.setObjectName("navigationInterface")
        self.titleBar.setObjectName("titleBar")
        self.navigationInterface.panel.setObjectName("navigationPanel")
        self.navigationInterface.setExpandWidth(200)
        if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
            self.navigationInterface.panel.returnButton.setIcon(FluentIcon.RIGHT_ARROW)

    def _register_navigation(self) -> None:
        self.addSubInterface(
            self._queue_wrapper, FluentIcon.DOWNLOAD, t("queue"),
            position=NavigationItemPosition.TOP,
        )
        self._search_panel.setObjectName("searchPage")
        self.addSubInterface(
            self._search_panel, FluentIcon.SEARCH, t("search"),
            position=NavigationItemPosition.TOP,
        )
        self._history_panel.setObjectName("historyPage")
        self.addSubInterface(
            self._history_panel, FluentIcon.HISTORY, t("history"),
            position=NavigationItemPosition.TOP,
        )
        self._converter_panel.setObjectName("converterPage")
        self.addSubInterface(
            self._converter_panel, CustomIcon("document_arrow_right"), t("converter"),
            position=NavigationItemPosition.TOP,
        )
        self.addSubInterface(
            self._metadata_page, FluentIcon.TAG, t("tag_editor"),
            position=NavigationItemPosition.TOP,
        )
        self.addSubInterface(
            self._settings_page, FluentIcon.SETTING, t("settings"),
            position=NavigationItemPosition.BOTTOM,
        )
        self.stackedWidget.currentChanged.connect(self._on_navigation_page_changed)

    # ──────────────────────────────────────────────────────────────────────────
    # Signal wiring  (AppWindow is the mediator — all connections live here)
    # ──────────────────────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        # ── URL bar → FetchController ──────────────────────────────────────────
        self._url_bar.fetch_requested.connect(self._start_fetch)
        self._url_bar.batch_import_requested.connect(self._fetch_ctrl.batch_import)
        self._url_bar.scrape_requested.connect(self._on_scrape)

        # ── FetchController → panels / AppWindow ──────────────────────────────
        self._fetch_ctrl.track_fetched.connect(self._add_track_to_queue)
        self._fetch_ctrl.fetch_finished.connect(self._on_fetch_finished)
        self._fetch_ctrl.fetch_error.connect(self._on_fetch_error)
        self._fetch_ctrl.fetching_changed.connect(self._url_bar.set_fetching)
        # A fetch/scrape/import is work of unknown length → indeterminate.
        self._fetch_ctrl.status_update.connect(self._on_activity_status)
        self._fetch_ctrl.temporary_status.connect(self._on_temporary_warning)
        self._fetch_ctrl.scrape_finished.connect(self._on_scrape_done)

        # ── Options bar ────────────────────────────────────────────────────────
        self._options_bar.options_changed.connect(self._on_options_changed)

        # ── Queue panel ────────────────────────────────────────────────────────
        self._queue_panel.selection_changed.connect(self._on_selection_changed)
        self._queue_panel.pause_resume_triggered.connect(self._on_global_pause_resume)
        self._queue_panel.card_removed.connect(self._on_card_removed)

        # ── Download bar → download flow ───────────────────────────────────────
        self._dl_bar.download_clicked.connect(self._on_download)

        # ── Status bar cancel ──────────────────────────────────────────────────
        self._status_bar.cancel_requested.connect(self._on_cancel)

        # ── SearchPanel → SearchController → AppWindow ────────────────────────
        self._search_panel.search_requested.connect(self._on_search)
        self._search_panel.add_to_queue_requested.connect(
            self._on_add_search_result_to_queue
        )
        self._search_panel.drill_down_requested.connect(self._on_search_drill_down)
        self._search_panel.source_repair_cancelled.connect(
            self._cancel_failure_source_repair
        )

        self._search_ctrl.result_ready.connect(self._on_search_result_ready)
        self._search_ctrl.result_to_queue.connect(self._on_result_to_queue)
        self._search_ctrl.search_error.connect(self._on_search_error)
        self._search_ctrl.searching_changed.connect(self._search_panel.set_searching)

        # ── DownloadController → panels / AppWindow ───────────────────────────
        # The whole-batch footer is driven by one coherent snapshot; the
        # controller no longer pokes text / progress / metrics independently.
        self._download_ctrl.batch_snapshot.connect(self._on_batch_snapshot)
        self._download_ctrl.downloading_changed.connect(self._dl_bar.set_downloading)
        self._download_ctrl.show_success_bar.connect(self._on_track_finished_ui)
        self._download_ctrl.user_action_required.connect(
            self._on_user_action_required_ui
        )
        self._download_ctrl.failure_incident_updated.connect(
            self._on_failure_incident_updated_ui
        )
        self._download_ctrl.rate_limit_waiting.connect(
            self._on_rate_limit_waiting_ui
        )
        self._download_ctrl.batch_finished.connect(self._on_all_downloads_finished)
        self._download_ctrl.batch_started.connect(self._on_batch_started)
        self._download_ctrl.track_thumbnail.connect(self._on_track_thumbnail_update)

        # ── History panel ──────────────────────────────────────────────────────
        self._history_panel.redownload_requested.connect(self._on_redownload)
        self._history_panel.open_folder_requested.connect(self._on_open_folder)

        # ── Metadata (Tag Editor) ──────────────────────────────────────────────
        # The Tag Editor is connected when its deferred widget tree is built.

    def _check_tag_apply_recovery(self) -> None:
        """Scan retained disk journals and proposal drafts without writing media."""
        try:
            recovery = self._metadata_ctrl.check_for_recovery()
            from core.runtime_mode import is_internal_smoke
            if is_internal_smoke():
                # The packaged smoke runs with no human present, so the modal
                # recovery prompt would block it forever (F-13). Suppressed here
                # and *only* here: the check still runs and still reports what it
                # found, and a real startup is unaffected because nothing outside
                # the smoke process can set this flag.
                found = self._metadata_ctrl.peek_draft()
                logger.info("[AppWindow] Internal smoke: draft recovery prompt "
                            "suppressed (draft present: %s)", bool(found))
                return
            if recovery is not None and self._metadata_panel is None:
                # The signal was emitted before the deferred panel existed.
                # Build it only for real recovery work, then deliver the same
                # read-only summary directly.
                self._ensure_metadata_panel().on_recovery_available(recovery)

            panel_existed = self._metadata_panel is not None
            draft = self._metadata_ctrl.check_for_draft()
            if draft is not None and not panel_existed:
                self._ensure_metadata_panel().on_draft_available(draft)
        except Exception:
            logger.debug("[AppWindow] Tag apply recovery check failed", exc_info=True)

    def _on_metadata_shutdown_timeout(self) -> None:
        """A Tag Editor disk op is still finishing after the bounded timeout —
        keep the app open and tell the user (never kill the thread; defect 6)."""
        self._metadata_shutdown_wired = False   # allow a later close to re-arm
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.warning(
                title=t("meta_apply_blocked_title"),
                content=t("md_shutdown_still_finishing"),
                parent=self, position=InfoBarPosition.BOTTOM_RIGHT, duration=6000,
            )
        except Exception:
            logger.info("[AppWindow] Tag Editor disk op still finishing; app kept open")

    def _on_navigation_page_changed(self, _index: int) -> None:
        """Materialize expensive pages only after the user selects them."""
        current = self.stackedWidget.currentWidget()
        if current is self._metadata_page and self._metadata_panel is None:
            QTimer.singleShot(0, self._ensure_metadata_panel)
        elif current is self._settings_page and self._settings_panel is None:
            QTimer.singleShot(0, self._ensure_settings_panel)

    def _ensure_settings_panel(self) -> "SettingsPanel":
        """Build and wire Settings on first visit."""
        if self._settings_panel is not None:
            return self._settings_panel

        from ui.panels.settings_panel import SettingsPanel

        panel = SettingsPanel(self._cfg, self._theme)
        self._settings_panel = panel
        self._settings_page_layout.replaceWidget(self._settings_loading_label, panel)
        self._settings_loading_label.hide()
        self._settings_loading_label.deleteLater()
        panel.clipboard_monitor_changed.connect(self._on_clipboard_setting_change)
        panel.accessibility_changed.connect(self._apply_accessibility)
        panel.login_fix_requested.connect(
            lambda: self._run_cookie_wizard_ui(prompt_for_url=True)
        )
        panel.settings_saved.connect(lambda: self._options_bar.apply_config(self._cfg))
        panel.settings_saved.connect(self._on_settings_saved)
        apply_touch_support(panel)
        logger.info("[AppWindow] Deferred Settings initialized")
        return panel

    def _ensure_metadata_panel(self) -> "MetadataEditorPanel":
        """Build and wire the Tag Editor once, preserving its safety lifecycle."""
        if self._metadata_panel is not None:
            return self._metadata_panel

        from ui.panels.metadata_editor_panel import MetadataEditorPanel

        panel = MetadataEditorPanel(config=self._cfg)
        self._metadata_panel = panel
        self._metadata_page_layout.replaceWidget(self._metadata_loading_label, panel)
        self._metadata_loading_label.hide()
        self._metadata_loading_label.deleteLater()
        self._connect_metadata_signals()
        apply_touch_support(panel)
        logger.info("[AppWindow] Deferred Tag Editor initialized")
        return panel

    def prepare_navigation_pages(self) -> None:
        """Finish primary navigation before exposing the interactive window.

        ``main`` calls this while the startup splash is visible. Keeping the
        fallback placeholders and on-demand builders remains useful for
        embedded/test construction and recovery, but a normal packaged launch
        hands control to the user only after both pages are ready. Qt widgets
        must be constructed on the GUI thread; a worker thread is not safe.
        """
        self._ensure_metadata_panel()
        self._ensure_settings_panel()
        logger.info("[AppWindow] Primary navigation pages prepared")

    def _connect_metadata_signals(self) -> None:
        """Wire MetadataEditorPanel ↔ MetadataController."""
        if self._metadata_signals_connected:
            return
        p  = self._metadata_panel
        c  = self._metadata_ctrl
        if p is None:
            return
        # Phase 2: panel and controller consult one workspace state object for
        # edit/visibility/Apply semantics; Qt selection is never Apply scope.
        p.set_workspace_state(c.workspace_state)
        c.set_incremental_updater(p.incremental_workspace_updater())
        # Rename/Move/Recycle/New folder are controller-owned so monitoring is
        # paused, own-operation evidence is registered before the disk changes,
        # and one authoritative reconciliation follows the terminal outcome.
        p.set_file_operation_owner(c)
        p.set_tag_action_preview_acceptor(c.accept_tag_action_preview)
        p.set_metadata_io_callbacks({
            "create_csv_export_plan": c.create_metadata_csv_export_plan,
            "start_csv_export": c.start_metadata_csv_export,
            "start_csv_header_preview": c.start_metadata_csv_header_preview,
            "start_csv_import_preview": c.start_metadata_csv_import_preview,
            "accept_csv_import": c.accept_metadata_csv_import,
            "create_change_report": c.create_change_report_snapshot,
            "create_problems_report": c.create_problems_report_snapshot,
            "start_report_export": c.start_report_export,
            "create_playlist": c.create_playlist_export_plan,
            "start_playlist_export": c.start_playlist_export,
            "cancel_io": c.cancel_metadata_io,
        })

        # Panel → Controller
        p.scan_requested.connect(lambda folder, rec: c.scan(folder, rec))
        p.scan_cancel_requested.connect(c.cancel_scan)
        p.unsaved_choice_requested.connect(c.resolve_unsaved_changes)
        p.review_opened.connect(c.acknowledge_draft_review)
        p.auto_requested.connect(c.apply_auto_rules)
        p.auto_sequence_requested.connect(c.apply_auto_sequence)
        p.apply_requested.connect(c.apply_changes)
        p.revert_requested.connect(c.revert_all)
        p.undo_requested.connect(c.undo_proposals)
        p.redo_requested.connect(c.redo_proposals)
        p.review_include_requested.connect(c.set_apply_excluded_ids)
        p.review_revert_records_requested.connect(c.revert_review_records)
        p.review_revert_files_requested.connect(c.revert_review_files)
        p.restore_requested.connect(c.restore_from_backup)
        p.undo_applied_requested.connect(c.undo_applied_batch)
        p.draft_restore_requested.connect(c.restore_draft_from_info)
        p.draft_discard_requested.connect(c.discard_draft)
        p.recover_requested.connect(c.recover_from_journal_backup)
        p.keep_recovery_requested.connect(c.keep_recovery_for_later)
        p.forget_recovery_requested.connect(c.forget_recovery)
        p.artist_to_scope.connect(c.apply_artist_to_scope)
        p.album_to_scope.connect(c.apply_album_to_scope)
        p.title_from_filename.connect(c.apply_title_from_filename)
        p.track_from_filename.connect(c.apply_track_from_filename)
        p.rename_from_title.connect(c.rename_filename_from_title)
        p.clear_comments.connect(c.clear_comments)
        p.album_artist_from_artist.connect(c.apply_album_artist_from_artist)
        p.split_artist_title.connect(c.split_artist_title_from_filename)
        p.clear_track_num.connect(c.clear_track_num)
        p.clear_year.connect(c.clear_year)
        p.clear_genre.connect(c.clear_genre)
        p.clear_title.connect(c.clear_title)
        p.clear_artist.connect(c.clear_artist)
        p.clear_album.connect(c.clear_album)
        p.clear_album_artist.connect(c.clear_album_artist)
        p.normalize_title_spaces.connect(c.normalize_title_spaces)
        p.strip_web_junk.connect(c.strip_web_junk_from_title)
        p.clean_filename.connect(c.clean_filename)
        p.strip_filename_numbering.connect(c.strip_filename_numbering)
        p.find_duplicates_requested.connect(lambda f, r: c.find_duplicates(f, r))
        p.delete_duplicates_requested.connect(c.delete_duplicate_files)
        p.revalidate_problems_requested.connect(c.revalidate_problems)
        p.problem_fix_requested.connect(c.apply_problem_fix)
        p.problem_fix_preview_requested.connect(c.create_problem_fix_preview)
        p.problem_fix_accept_requested.connect(c.accept_problem_fix)
        p.online_search_requested.connect(c.start_online_lookup)
        p.online_cancel_requested.connect(c.cancel_online_lookup)
        p.online_preview_requested.connect(c.preview_online_candidate)
        p.online_artwork_requested.connect(c.preview_online_artwork)
        p.online_accept_requested.connect(c.accept_online_match)
        p.delete_files_requested.connect(c.delete_files)
        p.replaygain_track_requested.connect(c.analyze_replaygain_tracks)
        p.replaygain_album_requested.connect(c.analyze_replaygain_album)
        p.replaygain_cancel_requested.connect(c.cancel_replaygain_analysis)
        p.manual_refresh_requested.connect(c.manual_filesystem_refresh)
        p.conflict_resolution_requested.connect(c.resolve_external_conflict)

        # Controller → Panel
        c.track_discovered.connect(p.on_track_discovered)
        c.workspace_replacement_started.connect(p.on_workspace_replacement_started)
        c.track_batch_discovered.connect(p.on_tracks_discovered)
        c.scan_progress.connect(p.on_scan_progress)
        c.scan_failed.connect(p.on_scan_error)
        c.scan_complete.connect(p.on_scan_complete)
        c.auto_rules_applied.connect(p.on_auto_rules_applied)
        c.tags_modified.connect(p.on_auto_rules_applied)   # refresh table after any in-memory edit
        c.apply_started.connect(p.on_apply_started)
        c.apply_progress.connect(p.on_apply_progress)
        c.apply_file_outcome.connect(p.on_apply_file_outcome)
        c.apply_batch_complete.connect(p.on_apply_batch_complete)
        c.apply_error.connect(p.on_apply_error)
        c.recovery_available.connect(p.on_recovery_available)
        c.draft_available.connect(p.on_draft_available)
        c.unsaved_changes_action_required.connect(p.on_unsaved_changes_action_required)
        c.restore_started.connect(p.on_restore_started)
        c.restore_progress.connect(p.on_restore_progress)
        c.restore_complete.connect(p.on_restore_complete)
        c.status_update.connect(p.on_status_update)
        c.duplicate_scan_progress.connect(p.on_duplicate_scan_progress)
        c.duplicate_scan_complete.connect(p.on_duplicate_scan_complete)
        c.duplicate_scan_error.connect(p.on_duplicate_scan_error)
        c.duplicate_delete_complete.connect(p.on_duplicate_delete_complete)
        c.validation_updated.connect(p.on_validation_updated)
        c.problem_fix_preview_ready.connect(p.on_problem_fix_preview)
        c.problem_fix_preview_failed.connect(p.on_problem_fix_preview_failed)
        c.online_lookup_started.connect(p.on_online_lookup_started)
        c.online_lookup_finished.connect(p.on_online_lookup_finished)
        c.online_release_detail_finished.connect(p.on_online_release_detail_finished)
        c.online_match_preview_ready.connect(p.on_online_match_preview)
        c.online_artwork_ready.connect(p.on_online_artwork_ready)
        c.online_artwork_error.connect(p.on_online_artwork_error)
        c.online_acceptance_error.connect(p.on_online_acceptance_error)
        c.online_acceptance_complete.connect(p.on_online_acceptance_complete)
        c.metadata_io_started.connect(p.on_metadata_io_started)
        c.metadata_io_finished.connect(p.on_metadata_io_finished)
        c.metadata_io_error.connect(p.on_metadata_io_error)
        c.replaygain_analysis_started.connect(p.on_replaygain_analysis_started)
        c.replaygain_analysis_progress.connect(p.on_replaygain_analysis_progress)
        c.replaygain_analysis_complete.connect(p.on_replaygain_analysis_complete)
        c.monitoring_state_changed.connect(p.on_monitoring_state_changed)
        c.external_changes_updated.connect(p.on_external_changes_updated)
        c.workspace_refresh_applied.connect(p.on_workspace_refresh_applied)
        c.conflict_resolution_finished.connect(p.on_conflict_resolution_finished)
        self._metadata_signals_connected = True

    # ──────────────────────────────────────────────────────────────────────────
    # Background workers startup
    # ──────────────────────────────────────────────────────────────────────────

    def _start_background_workers(self) -> None:
        from core.runtime_mode import is_internal_smoke
        if is_internal_smoke():
            logger.info("[AppWindow] Internal smoke: background workers suppressed")
            return
        self._clipboard_worker = ClipboardWorker(parent=self)
        self._clipboard_worker.url_detected.connect(self._on_clipboard_url)
        if self._cfg.clipboard_monitor:
            self._clipboard_worker.start()
        self._url_bar.set_clipboard_monitor_active(self._cfg.clipboard_monitor)

        if self._cfg.check_updates:
            from ui.workers.update_worker import UpdateWorker
            self._update_worker = UpdateWorker(
                check_app=True, check_components=True, parent=self,
            )
            self._update_worker.results_ready.connect(self._on_update_results)
            self._update_worker.start()

        self._net_monitor = OfflineMonitor(parent=self)
        self._net_monitor.went_offline.connect(self._on_went_offline)
        self._net_monitor.came_online.connect(self._on_came_online)
        self._net_monitor.start()

        if self._cfg.global_hotkeys_enabled:
            self._register_hotkeys()

    # ──────────────────────────────────────────────────────────────────────────
    # Offline monitor
    # ──────────────────────────────────────────────────────────────────────────

    def _on_went_offline(self) -> None:
        # Connectivity is the OfflineBanner's job — do NOT duplicate it in the
        # footer, and never clobber a live download's progress line.
        self._offline_banner.show()

    def _on_came_online(self) -> None:
        self._offline_banner.hide()
        # A brief "connection restored" note is optional and must not stomp on
        # an operation that's still (or again) running.
        if self._status_bar.state == StatusState.IDLE:
            self._status_bar.show_temporary(t("status_online"), StatusKind.SUCCESS)

    # ──────────────────────────────────────────────────────────────────────────
    # System Tray
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip(t("tray_tooltip"))
        try:
            from PySide6.QtGui import QPixmap
            px = QPixmap(32, 32)
            px.fill(Qt.GlobalColor.transparent)
            self._tray.setIcon(QIcon(px))
        except Exception:
            pass
        menu = QMenu(self)
        menu.addAction(t("tray_open"), self._tray_open)
        menu.addSeparator()
        menu.addAction(t("tray_cancel_all"), self._on_cancel)
        menu.addSeparator()
        menu.addAction(t("tray_quit"), self._tray_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _tray_open(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_quit(self) -> None:
        self._cfg.tray_on_close = False
        self.close()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_open()

    # ──────────────────────────────────────────────────────────────────────────
    # Drag & Drop URL support
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_drag_drop(self) -> None:
        wrapper = self._queue_wrapper

        def _drag_enter(event) -> None:
            md = event.mimeData()
            if md.hasUrls() or md.hasText():
                event.acceptProposedAction()
            else:
                event.ignore()

        def _drop(event) -> None:
            md = event.mimeData()
            urls: list[str] = []
            if md.hasUrls():
                urls = [u.toString() for u in md.urls() if u.scheme() in ("http", "https")]
            elif md.hasText():
                for line in md.text().splitlines():
                    line = line.strip()
                    if line.startswith(("http://", "https://")):
                        urls.append(line)
            for url in urls:
                try:
                    classify_url(url)
                    self._url_bar.set_url(url)
                    self._start_fetch(url)
                    break
                except Exception:
                    continue
            event.acceptProposedAction()

        wrapper.dragEnterEvent = _drag_enter   # type: ignore[method-assign]
        wrapper.dropEvent      = _drop          # type: ignore[method-assign]

    # ──────────────────────────────────────────────────────────────────────────
    # Accessibility
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_accessibility(self, enabled: bool) -> None:
        # ThemeManager owns stylesheet application (it styles the main
        # window, which outranks any QApplication-level stylesheet), so
        # the high-contrast overlay must go through it — both to actually
        # take effect and to survive later theme/accent switches.
        self._theme.set_accessibility_qss(_A11Y_QSS if enabled else "")

    # ──────────────────────────────────────────────────────────────────────────
    # Global hotkeys
    # ──────────────────────────────────────────────────────────────────────────

    def _register_hotkeys(self) -> None:
        try:
            import keyboard  # type: ignore[import]
            keyboard.add_hotkey("ctrl+alt+p", self._on_cancel)
            keyboard.add_hotkey("ctrl+alt+y", self._tray_open)
            logger.info("[AppWindow] Global hotkeys registered.")
        except ImportError:
            logger.warning("[AppWindow] 'keyboard' library not installed; hotkeys disabled.")
        except Exception as exc:
            logger.warning("[AppWindow] Global hotkey registration failed: %s", exc)

    # ──────────────────────────────────────────────────────────────────────────
    # Auto-resume  (queue persistence)
    # ──────────────────────────────────────────────────────────────────────────

    def _check_auto_resume(self) -> None:
        saved = self._cfg.queue_state
        if not saved:
            return
        if confirm(
            self,
            t("resume_downloads_title"),
            t("resume_downloads_msg", count=len(saved)),
            accept_text=t("meta_ok"),
            cancel_text=t("cancel_btn"),
        ):
            self._restore_queue_state(saved)
            self._cfg.queue_state = []
            self._cfg.save()

    def _restore_queue_state(self, saved: list[dict]) -> None:
        from core.playlist_parser import TrackMeta
        logger.debug("[AppWindow] Restoring %d queue items", len(saved))
        for item in saved:
            try:
                try:
                    platform = SourcePlatform(item.get("platform", "youtube"))
                except ValueError:
                    platform = SourcePlatform.YOUTUBE
                meta = TrackMeta(
                    title=item.get("title", "Unknown"),
                    artist=item.get("artist", ""),
                    url=item.get("url", ""),
                    duration_str=item.get("duration_str", ""),
                    thumbnail_url=item.get("thumbnail_url", ""),
                    platform=platform,
                    album=item.get("album", ""),
                    collection_title=item.get("collection_title", ""),
                    parent_artist=item.get("parent_artist", ""),
                    release_type=item.get("release_type", ""),
                    category=item.get("category", ""),
                    album_index=item.get("album_index", 0),
                    disc_number=item.get("disc_number", 0),
                    disc_total=item.get("disc_total", 0),
                    total_tracks=item.get("total_tracks", 0),
                    duration_sec=item.get("duration_sec"),
                    spotify_id=item.get("spotify_id", ""),
                    spotify_key_kind=item.get("spotify_key_kind", "spotify_id"),
                    match_status=item.get("match_status", "matched"),
                    resolution_error=item.get("resolution_error", ""),
                    source_kind=item.get("source_kind", ""),
                    source_url=item.get("source_url", ""),
                )
                self._add_track_to_queue(meta)
            except Exception as exc:
                logger.debug("[AppWindow] Failed to restore queue item: %s", exc)

    def _save_queue_state(self) -> None:
        cards = self._queue_panel.get_all_cards()
        state = [
            {
                "title":         c.title,
                "artist":        c.artist,
                "url":           c.track_url,
                "duration_str":  c.duration,
                "thumbnail_url": c.thumbnail_url,
                "platform":      c.platform,
                "album":         c.album,
                "collection_title": getattr(c, "collection_title", ""),
                "parent_artist": c.parent_artist,
                "release_type":  c.release_type,
                "category":      c.category,
                "album_index":   c.album_index,
                "disc_number":   getattr(c, "disc_number", 0),
                "disc_total":    getattr(c, "disc_total", 0),
                "total_tracks":  c.total_tracks,
                "duration_sec":  getattr(c, "duration_sec", None),
                "spotify_id":    getattr(c, "spotify_id", ""),
                "spotify_key_kind": getattr(c, "spotify_key_kind", "spotify_id"),
                "match_status":  getattr(c, "match_status", "matched"),
                "resolution_error": getattr(c, "resolution_error", ""),
                "source_kind":    getattr(c, "source_kind", ""),
                "source_url":     getattr(c, "source_url", ""),
            }
            for c in cards
            # "done" cards are finished; "paused" cards are owned by the
            # authoritative PausedBatchStore (core.paused_batch_store) and
            # restored from there on startup — saving them here too would
            # restore each paused card twice.
            if c.get_status() not in ("done", "paused")
        ]
        self._cfg.queue_state = state
        self._cfg.save()

    # ──────────────────────────────────────────────────────────────────────────
    # Download flow  (thin delegates to DownloadController)
    # ──────────────────────────────────────────────────────────────────────────

    def _on_download(self) -> None:
        selected = self._queue_panel.get_selected_cards()
        if not selected:
            # The Download button is disabled when nothing is selected, so
            # this only happens via an edge case (e.g. a stale hotkey). Show a
            # lightweight local hint \u2014 never the global status footer.
            InfoBar.warning(
                title=t("no_tracks_selected"),
                content="",
                orient=Qt.Orientation.Horizontal,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        # Keep an in-flight fast-start match alive. The resolver's single-flight
        # boundary lets the download pipeline join that exact work instead of
        # cancelling it or starting the same cold lookup again.
        opts = self._options_bar.get_options()
        self._download_ctrl.start_batch(
            selected, opts, self._last_url_kind, self._last_playlist_title
        )

    def _on_global_pause_resume(self, pause: bool) -> None:
        if pause:
            self._match_prefetcher.cancel()
            # Pause, not cancel: the outcome model keeps the distinction, so
            # the footer shows "paused" and the queue stays resumable.
            self._download_ctrl.global_pause()
            self._queue_panel.set_pause_resume_state(True)
            self._status_bar.show_paused()
        else:
            # Resume All CONTINUES the same paused batch — same jobs, same
            # workspaces, same partial downloads — via resume_all(). It must
            # NOT go through _on_download()/start_batch(), which would rebuild
            # requests from cards, re-run the duplicate policy and discard the
            # partial state. resume_all() is a no-op if nothing was paused.
            self._queue_panel.set_pause_resume_state(False)
            self._download_ctrl.resume_all()

    def _on_pause_track(self, queue_index: int) -> None:
        card = self._index_to_card.get(queue_index)
        if card:
            self._download_ctrl.pause_track(card)

    def _on_resume_track(self, queue_index: int) -> None:
        card = self._index_to_card.get(queue_index)
        if card:
            self._download_ctrl.resume_track(card)

    # ── Download signal handlers (UI-only — card updates done in controller) ──

    def _on_track_finished_ui(self, output_path: str) -> None:
        InfoBar.success(
            title=t("download_toast_title"),
            content=Path(output_path).name[:60] if output_path else t("download_toast_fallback"),
            orient=Qt.Orientation.Horizontal,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=4000,
            parent=self,
        )

    def _on_failure_incident_updated_ui(self, incident: object) -> None:
        """Open or update one live non-modal dialog for an error group."""
        incident_id = str(getattr(incident, "incident_id", "") or "")
        if not incident_id:
            return
        incidents = getattr(self, "_download_failure_incidents", None)
        if incidents is None:
            incidents = {}
            self._download_failure_incidents = incidents
        incidents[incident_id] = incident

        # Source repair owns the search surface. Continue aggregating incident
        # state, but serialize any unrelated dialog until the user finishes or
        # cancels choosing alternatives.
        repair_id = getattr(self, "_source_repair_incident_id", "")
        if repair_id:
            if incident_id != repair_id:
                pending = getattr(self, "_pending_failure_incident_ids", None)
                if pending is None:
                    pending = []
                    self._pending_failure_incident_ids = pending
                if incident_id not in pending:
                    pending.append(incident_id)
            return

        active_id = getattr(self, "_active_failure_incident_id", "")
        dialog = getattr(self, "_active_failure_incident_dialog", None)
        if active_id == incident_id and dialog is not None:
            title, text, details = self._failure_incident_text(incident)
            dialog.update_message(title, text, details)
            return

        pending = getattr(self, "_pending_failure_incident_ids", None)
        if pending is None:
            pending = []
            self._pending_failure_incident_ids = pending
        if active_id or dialog is not None:
            if incident_id not in pending:
                pending.append(incident_id)
            return
        self._open_failure_incident_dialog(incident_id)

    def _failure_incident_text(self, incident: object) -> tuple[str, str, str]:
        err = getattr(incident, "error", None)
        if isinstance(err, ErrorInfo):
            err = self._localized_error_info(err)
        headline = str(getattr(err, "headline", t("err_generic_title")) or "")
        detail = str(getattr(err, "detail", err) or "")
        if str(getattr(err, "message_key", "") or "") == "err_no_search_results":
            detail = f"{detail}\n\n{t('download_incident_no_result_help')}"
        count = max(1, int(getattr(incident, "count", 1) or 1))
        title = t(
            "download_incident_title_one" if count == 1 else "download_incident_title_many",
            count=count,
            headline=headline,
        )
        if bool(getattr(incident, "stopped_all", False)):
            prefix_key = (
                "download_incident_all_stopped_one"
                if count == 1 else "download_incident_all_stopped_many"
            )
        elif bool(getattr(incident, "authentication", False)):
            prefix_key = (
                "download_incident_auth_continuing_one"
                if count == 1 else "download_incident_auth_continuing_many"
            )
        else:
            prefix_key = (
                "download_incident_local_continuing_one"
                if count == 1 else "download_incident_local_continuing_many"
            )
        prefix = t(prefix_key, count=count)
        text = f"{prefix}\n\n{detail}"

        detail_lines: list[str] = []
        raw_seen: set[str] = set()
        for number, item in enumerate(getattr(incident, "items", []) or [], 1):
            item_title = str(getattr(item, "title", "") or getattr(item, "key", ""))
            item_url = str(getattr(item, "url", "") or "")
            detail_lines.append(f"{number}. {item_title}")
            if item_url:
                detail_lines.append(f"   {item_url}")
            raw = str(getattr(item, "raw", "") or "").strip()
            if raw and raw not in raw_seen:
                raw_seen.add(raw)
                detail_lines.append(f"   {raw}")
        return title, text, "\n".join(detail_lines)

    def _open_failure_incident_dialog(self, incident_id: str) -> None:
        incident = getattr(self, "_download_failure_incidents", {}).get(incident_id)
        if incident is None:
            self._open_next_failure_incident_dialog()
            return
        title, text, details = self._failure_incident_text(incident)
        auth = bool(getattr(incident, "authentication", False))
        no_result = str(
            getattr(getattr(incident, "error", None), "message_key", "") or ""
        ) == "err_no_search_results"
        dialog = StyledMessageDialog(
            title,
            text,
            self,
            kind="question" if auth else "warning",
            accept_text=(
                t("download_incident_fix_auth_btn")
                if auth else t("download_incident_retry_btn")
            ),
            cancel_text=t("download_incident_skip_btn"),
            auxiliary_text=(
                t("download_incident_find_alternatives_btn") if no_result else None
            ),
            show_cancel=True,
            details=details,
        )
        self._download_recovery_workflow_active = True
        self._active_failure_incident_id = incident_id
        self._active_failure_incident_dialog = dialog
        dialog.finished.connect(
            lambda result, iid=incident_id, dlg=dialog:
                self._finish_failure_incident_dialog(iid, dlg, result)
        )
        dialog.open()

    def _finish_failure_incident_dialog(
        self,
        incident_id: str,
        dialog: QDialog,
        result: int,
    ) -> None:
        if getattr(self, "_active_failure_incident_dialog", None) is dialog:
            self._active_failure_incident_dialog = None
            self._active_failure_incident_id = ""
        incident = getattr(self, "_download_failure_incidents", {}).get(incident_id)
        retry = result == int(QDialog.DialogCode.Accepted)
        find_alternatives = result == _SOURCE_ALTERNATIVE_DIALOG_RESULT
        source_repair_started = False
        keep_open = False
        try:
            if find_alternatives and incident is not None:
                self._begin_failure_source_repair(incident_id)
                source_repair_started = True
            elif retry and incident is not None and bool(
                getattr(incident, "authentication", False)
            ):
                retry = self._run_cookie_wizard_ui()
                keep_open = not retry
            if not keep_open and not find_alternatives:
                decision = RecoveryDecision.RETRY if retry else RecoveryDecision.SKIP
                self._download_ctrl.resolve_failure_incident(incident_id, decision)
                getattr(self, "_download_failure_incidents", {}).pop(incident_id, None)
        except Exception:
            logger.exception("[AppWindow] Could not settle failure incident")
            keep_open = True
        finally:
            dialog.deleteLater()
            self._download_recovery_workflow_active = False

        if source_repair_started:
            return
        if keep_open and incident is not None:
            QTimer.singleShot(
                0,
                self,
                lambda iid=incident_id: self._open_failure_incident_dialog(iid),
            )
        else:
            QTimer.singleShot(0, self._open_next_failure_incident_dialog)

    def _begin_failure_source_repair(self, incident_id: str) -> None:
        incident = getattr(self, "_download_failure_incidents", {}).get(incident_id)
        items = list(getattr(incident, "items", []) or []) if incident else []
        if not items:
            self._open_next_failure_incident_dialog()
            return
        self._source_repair_incident_id = incident_id
        self._source_repair_key = ""
        self._source_repair_total = len(items)
        self._source_repair_completed = 0
        self._source_repair_jobs = []
        self._show_next_failure_source_repair()

    def _show_next_failure_source_repair(self) -> None:
        incident_id = getattr(self, "_source_repair_incident_id", "")
        incident = getattr(self, "_download_failure_incidents", {}).get(incident_id)
        items = list(getattr(incident, "items", []) or []) if incident else []
        if not items:
            self._finish_failure_source_repair()
            return
        item = items[0]
        self._source_repair_key = str(getattr(item, "key", "") or "")
        title = str(getattr(item, "title", "") or self._source_repair_key)
        artist = str(getattr(item, "artist", "") or "")
        duration_sec = getattr(item, "duration_sec", None)
        display_parts = [f"{artist} — {title}" if artist else title]
        if duration_sec is not None:
            try:
                from utils.time_format import seconds_to_str
                display_parts.append(seconds_to_str(int(duration_sec)))
            except (TypeError, ValueError):
                pass
        display_title = " · ".join(part for part in display_parts if part)
        query = " ".join(part for part in (artist, title) if part).strip()
        self._search_panel.begin_source_repair(
            display_title,
            self._source_repair_completed + 1,
            self._source_repair_total,
        )
        self.switchTo(self._search_panel)
        # The search controller owns cancelling any older worker. Force is
        # intentional because a still-settling previous search must not leave
        # this recovery screen empty.
        self._search_panel.run_query(query, platform="youtube", force=True)

    def _finish_failure_source_repair(self) -> None:
        incident_id = getattr(self, "_source_repair_incident_id", "")
        jobs = list(getattr(self, "_source_repair_jobs", []) or [])
        self._source_repair_jobs = []
        self._download_ctrl.start_failure_source_jobs(jobs)
        self._source_repair_incident_id = ""
        self._source_repair_key = ""
        self._source_repair_total = 0
        self._source_repair_completed = 0
        self._search_panel.end_source_repair()
        if incident_id:
            getattr(self, "_download_failure_incidents", {}).pop(incident_id, None)
        self.switchTo(self._queue_wrapper)
        InfoBar.success(
            title=t("search_source_repair_done_title"),
            content=t("search_source_repair_done_text"),
            orient=Qt.Orientation.Horizontal,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=4000,
            parent=self,
        )
        QTimer.singleShot(0, self._open_next_failure_incident_dialog)

    def _cancel_failure_source_repair(self) -> None:
        incident_id = getattr(self, "_source_repair_incident_id", "")
        self._search_ctrl.cancel()
        jobs = list(getattr(self, "_source_repair_jobs", []) or [])
        self._source_repair_jobs = []
        self._download_ctrl.start_failure_source_jobs(jobs)
        self._source_repair_incident_id = ""
        self._source_repair_key = ""
        self._source_repair_total = 0
        self._source_repair_completed = 0
        self._search_panel.end_source_repair()
        self.switchTo(self._queue_wrapper)
        if incident_id in getattr(self, "_download_failure_incidents", {}):
            QTimer.singleShot(
                0, self, lambda iid=incident_id: self._open_failure_incident_dialog(iid)
            )
        else:
            QTimer.singleShot(0, self._open_next_failure_incident_dialog)

    def _open_next_failure_incident_dialog(self) -> None:
        if getattr(self, "_active_failure_incident_dialog", None) is not None:
            return
        pending = getattr(self, "_pending_failure_incident_ids", [])
        incidents = getattr(self, "_download_failure_incidents", {})
        while pending:
            incident_id = pending.pop(0)
            if incident_id in incidents:
                self._open_failure_incident_dialog(incident_id)
                return

    def _on_user_action_required_ui(self, request: UserActionRequest) -> None:
        """Queue one non-nested error decision dialog.

        ``QDialog.exec()`` starts a nested event loop. The old five-second
        throttle allowed another worker signal to enter that loop and call
        exec() again, eventually overflowing Qt's stack. ``open()`` returns to
        the main loop immediately; the coordinator keeps downloads paused
        until ``finished`` resolves this request.
        """
        if request.decision is not None:
            return
        pending = getattr(self, "_pending_download_error_requests", None)
        if pending is None:
            pending = []
            self._pending_download_error_requests = pending
        seen = getattr(self, "_download_error_request_ids", None)
        if seen is None:
            seen = set()
            self._download_error_request_ids = seen
        request_id = id(request)
        if request_id in seen:
            return
        seen.add(request_id)
        try:
            if getattr(self, "_active_download_error_dialog", None) is not None:
                pending.append(request)
                return
            self._open_user_action_dialog(request)
        except Exception:  # noqa: BLE001 - never strand the producer thread
            seen.discard(request_id)
            failed_dialog = getattr(self, "_active_download_error_dialog", None)
            if getattr(self, "_active_download_error_request", None) is request:
                self._active_download_error_dialog = None
                self._active_download_error_request = None
            self._download_recovery_workflow_active = False
            logger.exception("[AppWindow] Could not open download recovery dialog")
            request.resolve(RecoveryDecision.SKIP)
            if failed_dialog is not None:
                try:
                    failed_dialog.deleteLater()
                except Exception:
                    pass
            if pending:
                QTimer.singleShot(0, self._open_next_download_error_request)

    def _open_user_action_dialog(self, request: UserActionRequest) -> None:
        err = request.error
        if isinstance(err, ErrorInfo):
            err = self._localized_error_info(err)
        headline = getattr(err, "headline", t("err_generic_title"))
        detail = getattr(err, "detail", str(err))
        raw = str(getattr(err, "raw", "") or "")
        raw_error_text = f"{headline}\n{detail}\n{raw}"
        message_key = str(getattr(err, "message_key", "") or "")
        auth_related = message_key in {
            "err_browser_cookie_access",
            "err_cookies_expired",
            "err_signin_required",
        }
        if not message_key:
            auth_related = (
                self._is_browser_cookie_error_text(raw_error_text)
                or self._is_auth_error_text(raw_error_text)
            )
        headline, detail = self._localized_error_text(headline, detail, raw)

        dialog = StyledMessageDialog(
            headline,
            detail,
            self,
            kind="question" if auth_related else "warning",
            accept_text=(
                t("auth_wizard_open_btn")
                if auth_related else t("download_error_skip_btn")
            ),
            cancel_text=t("download_error_skip_btn"),
            show_cancel=auth_related,
            details=raw,
        )
        self._download_recovery_workflow_active = True
        self._active_download_error_dialog = dialog
        self._active_download_error_request = request
        dialog.finished.connect(
            lambda result, req=request, dlg=dialog, auth=auth_related:
                self._finish_user_action_dialog(req, dlg, auth, result)
        )
        dialog.open()

    def _finish_user_action_dialog(
        self,
        request: UserActionRequest,
        dialog: QDialog,
        auth_related: bool,
        result: int,
    ) -> None:
        if getattr(self, "_active_download_error_dialog", None) is dialog:
            self._active_download_error_dialog = None
            self._active_download_error_request = None

        decision = RecoveryDecision.SKIP
        try:
            if (
                auth_related
                and result == int(QDialog.DialogCode.Accepted)
                and self._run_cookie_wizard_ui()
            ):
                decision = RecoveryDecision.RETRY
        except Exception:  # noqa: BLE001 - always release the waiting worker
            logger.exception("[AppWindow] Authentication recovery failed")
        finally:
            try:
                self._download_ctrl.resolve_user_action(request, decision)
            except Exception:  # noqa: BLE001 - direct fallback always unblocks
                logger.exception("[AppWindow] Could not forward recovery decision")
                request.resolve(decision)
            dialog.deleteLater()
            getattr(self, "_download_error_request_ids", set()).discard(id(request))
            self._download_recovery_workflow_active = False

        pending = getattr(self, "_pending_download_error_requests", [])
        if pending:
            next_request = pending.pop(0)
            if next_request.decision is not None:
                getattr(self, "_download_error_request_ids", set()).discard(
                    id(next_request)
                )
                QTimer.singleShot(0, self._open_next_download_error_request)
            else:
                getattr(self, "_download_error_request_ids", set()).discard(
                    id(next_request)
                )
                QTimer.singleShot(
                    0, self, lambda req=next_request: self._on_user_action_required_ui(req)
                )

    def _open_next_download_error_request(self) -> None:
        pending = getattr(self, "_pending_download_error_requests", [])
        while pending:
            request = pending.pop(0)
            if request.decision is not None:
                getattr(self, "_download_error_request_ids", set()).discard(id(request))
                continue
            getattr(self, "_download_error_request_ids", set()).discard(id(request))
            self._on_user_action_required_ui(request)
            return

    def _cancel_download_error_requests(self) -> None:
        """Resolve and close every active/queued recovery question."""
        if getattr(self, "_source_repair_incident_id", ""):
            jobs = list(getattr(self, "_source_repair_jobs", []) or [])
            self._source_repair_jobs = []
            self._download_ctrl.discard_failure_source_jobs(jobs)
            self._source_repair_incident_id = ""
            self._source_repair_key = ""
            self._source_repair_total = 0
            self._source_repair_completed = 0
            self._search_panel.end_source_repair()
        incidents = getattr(self, "_download_failure_incidents", {})
        for incident_id in list(incidents):
            try:
                self._download_ctrl.resolve_failure_incident(
                    incident_id, RecoveryDecision.CANCEL
                )
            except Exception:
                logger.exception("[AppWindow] Could not cancel failure incident")
        incidents.clear()
        getattr(self, "_pending_failure_incident_ids", []).clear()
        incident_dialog = getattr(self, "_active_failure_incident_dialog", None)
        self._active_failure_incident_dialog = None
        self._active_failure_incident_id = ""
        if incident_dialog is not None:
            try:
                incident_dialog.blockSignals(True)
                incident_dialog.close()
                incident_dialog.deleteLater()
            except Exception:
                pass

        active_request = getattr(self, "_active_download_error_request", None)
        if active_request is not None:
            active_request.resolve(RecoveryDecision.CANCEL)
        pending = getattr(self, "_pending_download_error_requests", [])
        for request in list(pending):
            request.resolve(RecoveryDecision.CANCEL)
        pending.clear()
        getattr(self, "_download_error_request_ids", set()).clear()
        dialog = getattr(self, "_active_download_error_dialog", None)
        if dialog is not None:
            dialog.reject()

    def _show_async_warning(self, headline: str, detail: str) -> None:
        """Defer unrelated async errors while download recovery owns dialogs."""
        if getattr(self, "_download_recovery_workflow_active", False):
            QTimer.singleShot(
                200,
                self,
                lambda h=headline, d=detail: self._show_async_warning(h, d),
            )
            return
        show_warning(self, headline, detail)

    def _on_rate_limit_waiting_ui(self, remaining_seconds: float) -> None:
        if remaining_seconds <= 0:
            self._rate_limit_network_paused = False
            dialog = getattr(self, "_rate_limit_dialog", None)
            if dialog is not None:
                self._rate_limit_dialog = None
                dialog.close()
                dialog.deleteLater()
            return
        coordinator = shared_download_recovery_coordinator()
        rate = coordinator.rate_snapshot()
        epoch = int(rate.get("epoch", 0) or 0)
        if not getattr(self, "_rate_limit_network_paused", False):
            self._rate_limit_network_paused = True
            # Stop every optional YouTube-producing activity outside the
            # download worker too. Existing provider calls settle
            # cooperatively; no new fetch/search/prefetch is admitted below.
            self._match_prefetcher.cancel()
            self._fetch_ctrl.cancel()
            self._search_ctrl.cancel()
        self._update_rate_limit_dialog(rate, remaining_seconds, epoch)
        snapshot = getattr(self, "_last_snapshot", None)
        if snapshot is None:
            return
        if self._status_bar.state not in (
            StatusState.INDETERMINATE, StatusState.DOWNLOADING,
        ):
            return
        from utils.time_format import seconds_to_str
        self._status_bar.show_batch_progress(
            snapshot,
            message=t(
                "status_rate_limit_waiting",
                time=seconds_to_str(remaining_seconds),
            ),
        )

    def _update_rate_limit_dialog(
        self,
        rate: dict,
        remaining_seconds: float,
        epoch: int,
    ) -> None:
        if epoch <= 0 or getattr(self, "_hidden_rate_limit_epoch", 0) == epoch:
            return
        from utils.time_format import seconds_to_str

        advertised = float(rate.get("advertised_seconds", 0.0) or 0.0)
        margin = float(rate.get("margin_seconds", 0.0) or 0.0)
        title = t("rate_limit_dialog_title")
        text = t(
            "rate_limit_dialog_text",
            time=seconds_to_str(remaining_seconds),
            advertised=seconds_to_str(advertised),
            margin=seconds_to_str(margin),
        )
        details = str(rate.get("message", "") or "")
        dialog = getattr(self, "_rate_limit_dialog", None)
        if dialog is not None and getattr(self, "_rate_limit_dialog_epoch", 0) == epoch:
            dialog.update_message(title, text, details)
            return
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        dialog = StyledMessageDialog(
            title,
            text,
            self,
            kind="warning",
            accept_text=t("rate_limit_hide_btn"),
            details=details,
        )
        self._rate_limit_dialog = dialog
        self._rate_limit_dialog_epoch = epoch
        dialog.finished.connect(
            lambda _result, ep=epoch, dlg=dialog:
                self._finish_rate_limit_dialog(ep, dlg)
        )
        dialog.open()

    def _finish_rate_limit_dialog(self, epoch: int, dialog: QDialog) -> None:
        if getattr(self, "_rate_limit_dialog", None) is dialog:
            self._rate_limit_dialog = None
        if shared_download_recovery_coordinator().rate_incident_active():
            self._hidden_rate_limit_epoch = epoch
        dialog.deleteLater()

    def _on_track_error_ui(self, err: object, failing_url: str = "") -> None:
        """Compatibility entry point routed through the serialized dialog queue."""
        request = UserActionRequest(
            key=failing_url or "legacy-error",
            error=err,
            failing_url=failing_url,
        )
        self._on_user_action_required_ui(request)

    def _run_cookie_wizard_ui(self, prompt_for_url: bool = False) -> bool:
        target_url = "https://www.youtube.com"
        if prompt_for_url:
            url, ok = get_text(
                self, t("auth_wizard_title"), t("auth_wizard_url_prompt"),
                text=target_url
            )
            if not ok or not url:
                return False
            target_url = url

        choice = ask_cookie_auth_choice(self)
        if choice == "app_browser":
            return self._run_app_browser_wizard(target_url)
        if choice == "manual":
            return self._run_manual_cookie_import()
        return False

    def _run_app_browser_wizard(self, target_url: str) -> bool:
        """Sign in via a dedicated, app-owned Chromium (never the user's real Chrome)."""
        from core.cookie_wizard import run_cookie_wizard
        from utils.cookie_validator import check_cookies_valid
        from utils.paths import get_app_cookies_path
        from utils.playwright_check import PlaywrightNotAvailable

        try:
            saved = run_cookie_wizard(target_url)
        except PlaywrightNotAvailable as exc:
            show_warning(
                self,
                t("auth_wizard_title"),
                exc.message_he if current_language() == "he" else exc.message_en,
            )
            return self._run_manual_cookie_import()

        if not saved:
            show_warning(self, t("auth_wizard_aborted_title"), t("auth_wizard_aborted_msg"))
            return False

        cookie_path = get_app_cookies_path()
        valid, warn_msg = check_cookies_valid(cookie_path)
        if not valid:
            show_warning(self, t("auth_wizard_aborted_title"), warn_msg)
            return False

        if not self._apply_saved_cookies_file(str(cookie_path)):
            return False
        show_info(self, t("auth_wizard_success_title"), t("auth_wizard_success_msg"))
        return True

    def _run_manual_cookie_import(self) -> bool:
        """Fallback path: user exports cookies.txt via a browser extension and picks the file."""
        from utils.cookie_validator import check_cookies_valid

        dlg = ManualCookieImportDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False

        path, _ = QFileDialog.getOpenFileName(
            self, t("select_cookies_file"), "", "Cookies (*.txt);;All Files (*)"
        )
        if not path:
            return False

        valid, warn_msg = check_cookies_valid(path)
        if not valid:
            show_warning(self, t("auth_wizard_aborted_title"), warn_msg)
            return False

        if not self._apply_saved_cookies_file(path):
            return False
        show_info(self, t("auth_wizard_success_title"), t("auth_wizard_success_msg"))
        return True

    def _apply_saved_cookies_file(self, path: str) -> bool:
        """
        Merge a validated cookies file into the app's own storage and point
        the app there — so deleting or moving the original file the user
        picked (e.g. a manual export sitting in Downloads) doesn't break
        future scrapes/downloads, and re-importing later only refreshes the
        matching cookies instead of discarding everything else.
        """
        from utils.paths import get_app_cookies_path
        from utils.cookie_validator import merge_cookies_file

        internal_path = get_app_cookies_path()
        try:
            if Path(path).resolve() != internal_path.resolve():
                merge_cookies_file(path, internal_path)
        except OSError:
            logger.warning("[AppWindow] Failed to store imported cookies safely")
            show_warning(
                self,
                t("cookies_store_failed_title"),
                t("cookies_store_failed_msg"),
            )
            return False

        self._cfg.cookies_file = str(internal_path)
        self._cfg.cookies_browser = ""
        self._cfg.save()
        self._options_bar.apply_config(self._cfg)
        self._settings_panel.refresh()
        return True

    def _localized_error_info(self, err: ErrorInfo) -> ErrorInfo:
        headline, detail = self._localized_error_text(err.headline, err.detail, err.raw)
        if (headline, detail) == (err.headline, err.detail):
            # Not one of the special-cased errors above — re-render from the
            # classifier's stable message key for non-English UIs. English
            # keeps the core-rendered text verbatim (the "en" table is the
            # same template dict, so re-rendering would be a no-op anyway).
            if err.message_key and current_language() != "en":
                headline = t(f"{err.message_key}_title", **err.message_params)
                detail = t(f"{err.message_key}_detail", **err.message_params)
                if err.doctor_key:
                    detail += (
                        "\n\n" + t("err_doctor_prefix")
                        + t(err.doctor_key, **err.doctor_params)
                    )
        if (headline, detail) == (err.headline, err.detail):
            return err
        return ErrorInfo(
            severity=err.severity,
            headline=headline,
            detail=detail,
            raw=err.raw,
            retriable=err.retriable,
            message_key=err.message_key,
            message_params=err.message_params,
            doctor_key=err.doctor_key,
            doctor_params=err.doctor_params,
        )

    def _localized_error_text(self, headline: str, detail: str, raw: str = "") -> tuple[str, str]:
        text = f"{headline}\n{detail}\n{raw}"
        if self._is_browser_cookie_error_text(text):
            return t("browser_cookie_read_failed_title"), t("browser_cookie_read_failed_detail")
        if self._is_auth_error_text(text):
            return t("signin_required_title"), t("signin_required_detail")
        return headline, detail

    def _is_browser_cookie_error_text(self, text: str) -> bool:
        haystack = text.lower()
        return (
            "browser_cookie_unsupported" in haystack
            or "cannot be read safely on windows" in haystack
            or "cookie database" in haystack
            or "database is locked" in haystack
            or "could not copy chrome" in haystack
            or ("could not copy" in haystack and "cookie" in haystack)
            or ("decrypt" in haystack and "cookie" in haystack)
            or "failed to decrypt with dpapi" in haystack
            or "dpapi" in haystack
        )

    def _is_auth_error_text(self, text: str) -> bool:
        haystack = text.lower()
        if "not a bot" in haystack or "bot challenge" in haystack:
            return False
        return (
            "sign-in required" in haystack
            or "age-restricted" in haystack
            or "requires a youtube account" in haystack
            or "account cookies" in haystack
            or "sign in to confirm" in haystack
            or "po token" in haystack
            or "visitor_data" in haystack
        )

    # ── Status footer routing (single translator: controller → StatusBar) ──

    def _on_activity_status(self, message: str) -> None:
        """A fetch / scrape / channel-import activity message of unknown length."""
        self._status_bar.show_indeterminate(message)

    def _on_temporary_warning(self, message: str) -> None:
        """A short, non-critical terminal note (e.g. 'no URLs found')."""
        self._status_bar.show_temporary(message, StatusKind.WARNING)

    def _on_batch_started(self) -> None:
        self._last_snapshot = None
        self._save_queue_state()
        self._status_bar.show_indeterminate(t("status_starting"))

    def _on_batch_snapshot(self, snapshot) -> None:
        """One coherent whole-batch progress update → determinate footer.

        Keep the snapshot for the finish summary regardless, but only repaint
        progress while the footer is actually showing a live download — a late
        in-flight snapshot must not clobber a pause / cancel / error state the
        user just triggered."""
        self._last_snapshot = snapshot
        if self._status_bar.state in (
            StatusState.INDETERMINATE, StatusState.DOWNLOADING
        ):
            remaining = self._download_ctrl.rate_limit_remaining()
            if remaining > 0:
                from utils.time_format import seconds_to_str
                self._status_bar.show_batch_progress(
                    snapshot,
                    message=t(
                        "status_rate_limit_waiting",
                        time=seconds_to_str(remaining),
                    ),
                )
            else:
                self._status_bar.show_batch_progress(snapshot)

    def _on_all_downloads_finished(self, outcome: BatchOutcome) -> None:
        snap = self._last_snapshot
        done   = snap.completed if snap else 0
        failed = snap.failed if snap else 0
        total  = snap.total if snap else 0

        clear_queue = True
        if outcome == BatchOutcome.PAUSED_BY_USER:
            # Resumable: keep the queue and offer Resume.
            self._queue_panel.set_pause_resume_state(True)
            self._status_bar.show_paused()
            clear_queue = False
        elif outcome == BatchOutcome.CANCELLED_BY_USER:
            self._queue_panel.set_pause_resume_state(False)
            self._status_bar.show_temporary(
                t("status_stopped_summary", done=done, total=total),
                StatusKind.CANCELLING,
            )
        elif outcome == BatchOutcome.STOPPED_BY_FATAL_ERROR:
            # The actionable error dialog was already raised; keep the queue so
            # the user can retry, and echo a persistent one-line summary.
            self._queue_panel.set_pause_resume_state(False)
            self._status_bar.show_error_summary(t("status_stopped_error"))
            clear_queue = False
        elif outcome == BatchOutcome.COMPLETED_WITH_ERRORS:
            self._queue_panel.set_pause_resume_state(False)
            self._status_bar.show_temporary(
                t("status_completed_with_errors", ok=done, failed=failed),
                StatusKind.WARNING,
            )
        else:  # COMPLETED
            self._queue_panel.set_pause_resume_state(False)
            preexisting = snap.preexisting if snap else 0
            cancelled = snap.cancelled if snap else 0
            if cancelled > 0:
                # Cancelling single tracks never cancels the batch, so this
                # still ends as a plain COMPLETED run. Reporting only the
                # successes would quietly drop the tracks the user stopped
                # and leave the summary short of the total.
                self._status_bar.show_temporary(
                    t(
                        "status_completed_with_cancelled",
                        completed=done, total=total, cancelled=cancelled,
                    ),
                    StatusKind.CANCELLING,
                )
            elif preexisting > 0:
                # Anchor on completed/total, not a bare "N downloads" count —
                # some of "completed" was a duplicate-skip that never
                # downloaded anything, so the whole batch must not be
                # labelled "downloads".
                self._status_bar.show_temporary(
                    t(
                        "status_completed_with_preexisting",
                        completed=done, total=total,
                        downloaded=done - preexisting, preexisting=preexisting,
                    ),
                    StatusKind.SUCCESS,
                )
            else:
                self._status_bar.show_temporary(
                    t("status_completed_summary", n=done, plural=("" if done == 1 else "s")),
                    StatusKind.SUCCESS,
                )

        if clear_queue:
            self._cfg.queue_state = []
            self._cfg.save()

        if outcome.is_success and self._tray and not self.isVisible():
            self._tray.showMessage(
                t("tray_tooltip"),
                t("tray_all_done"),
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Fetch flow  (delegates to FetchController; AppWindow updates routing state)
    # ──────────────────────────────────────────────────────────────────────────

    def _start_fetch(self, url: str) -> None:
        """Entry point for all fetching, intercepting channel URLs to ask what to scrape."""
        rate_remaining = self._download_ctrl.rate_limit_remaining()
        if getattr(self, "_rate_limit_network_paused", False) or rate_remaining > 0:
            # Keep manual fetches closed through the same-track canary, not
            # merely until the numeric countdown reaches zero.
            self._on_rate_limit_waiting_ui(max(1.0, rate_remaining))
            return
        # A new fetch supersedes any in-flight fast-start prefetch from the
        # previous catalog, and re-arms the one-shot early-prefetch trigger.
        self._match_prefetcher.cancel()
        self._prefetch_started = False
        self._prefetch_pending_count = 0
        # Warm yt-dlp plugins now, at fetch start — independent of (and earlier
        # than) the per-track pre-resolve, so the one-time load never lands on
        # the first download's critical path.
        self._match_prefetcher.warm_up_async()
        if not looks_like_url(url):
            if is_malformed_url_attempt(url):
                # Starts with "scheme://" but isn't a real URL (broken/typo'd
                # paste) — tell the user instead of silently treating it as
                # a search query, which would be a confusing thing to search for.
                self._status_bar.show_temporary(t("invalid_url_title"), StatusKind.WARNING)
                show_warning(self, t("invalid_url_title"), t("invalid_url_detail"))
                return
            # The URL bar's placeholder invites free-text search too — forward
            # non-URL input to the Search tab instead of handing raw text to
            # yt-dlp/Playwright, which can only navigate to real URLs.
            self._url_bar.clear_url()
            self.switchTo(self._search_panel)
            self._search_panel.run_query(url)
            return

        platform, kind = classify_url(url)
        if platform == SourcePlatform.YOUTUBE and kind == UrlKind.ARTIST:
            from ui.controllers.channel_flow_controller import ChannelFlowController

            ctrl = ChannelFlowController(
                channel_url=url,
                channel_name="",
                config=self._cfg,
                parent_widget=self,
                parent=self,
            )
            ctrl.tracks_ready.connect(
                lambda tracks: [self._add_track_to_queue(t) for t in tracks]
            )
            ctrl.status_update.connect(self._on_activity_status)
            ctrl.finished.connect(lambda: self._on_channel_flow_finished(ctrl))
            ctrl.run()
        elif (
            platform in (SourcePlatform.SPOTIFY, SourcePlatform.YOUTUBE_MUSIC)
            and kind == UrlKind.ARTIST
        ):
            from ui.controllers.artist_flow_controller import ArtistFlowController

            ctrl = ArtistFlowController(
                artist_url=url,
                platform=platform,
                config=self._cfg,
                parent_widget=self,
                parent=self,
            )
            ctrl.tracks_ready.connect(
                lambda tracks: [self._add_track_to_queue(track) for track in tracks]
            )
            ctrl.status_update.connect(self._on_activity_status)
            ctrl.finished.connect(lambda: self._on_artist_flow_finished(ctrl))
            ctrl.run()
        else:
            self._fetch_ctrl.fetch(url)

    def _on_artist_flow_finished(self, ctrl) -> None:
        self._last_url_kind = UrlKind.ARTIST
        if ctrl.artist_name:
            self._last_playlist_title = ctrl.artist_name
        if self._status_bar.state == StatusState.INDETERMINATE:
            self._status_bar.reset_to_idle()

    def _on_channel_flow_finished(self, ctrl) -> None:
        self._last_url_kind = UrlKind.ARTIST
        channel_name = getattr(ctrl, "_channel_name", "")
        if channel_name:
            self._last_playlist_title = channel_name
        # Import is done; the queue now speaks for itself → clean idle footer.
        if self._status_bar.state == StatusState.INDETERMINATE:
            self._status_bar.reset_to_idle()

    def _on_fetch_finished(self, result) -> None:
        if hasattr(result, "playlist_title") and result.playlist_title:
            self._last_playlist_title = result.playlist_title
        if hasattr(result, "kind"):
            self._last_url_kind = result.kind

        if hasattr(result, "error") and result.error and not getattr(result, "tracks", None):
            err = classify_error(Exception(result.error))
            err = self._localized_error_info(err)
            self._status_bar.show_error_summary(err.headline)
            self._show_async_warning(err.headline, err.detail)
            return

        n = len(self._queue_panel.get_all_cards())
        self._status_bar.show_temporary(
            t("fetch_done", n=n, plural=("" if n == 1 else "s")),
            StatusKind.SUCCESS,
        )

        # Fallback for catalogs with fewer than 8 pending tracks: the mid-scrape
        # trigger in _add_track_to_queue never reached the window, so fire once
        # here now the full (small) catalog is known. One-shot guard prevents a
        # double start for catalogs that already tripped the early trigger.
        self._start_match_prefetch()

    def _start_match_prefetch(self) -> None:
        """Kick the cancellable fast-start prefetch for the leading pending
        (two-stage Spotify) tracks in the queue. One-shot per fetch, and a
        no-op when nothing is pending.

        Only the first few pending cards are pre-resolved — never the whole
        catalog — and via the same resolve path, so match quality is unchanged.
        """
        if self._prefetch_started:
            return
        tds: list[dict] = []
        for card in self._queue_panel.get_all_cards():
            if getattr(card, "match_status", "matched") != "pending":
                continue
            tds.append({
                "spotify_id":   getattr(card, "spotify_id", ""),
                "title":        card.title,
                "artist":       card.artist,
                "duration_sec": getattr(card, "duration_sec", None),
            })
            if len(tds) >= 8:  # matches MatchPrefetcher's default window
                break
        if tds:
            self._prefetch_started = True  # one-shot per fetch
            self._match_prefetcher.start(
                tds, cookies_file=self._cfg.cookies_file or None
            )

    def _on_fetch_error(self, msg: str) -> None:
        err = classify_error(Exception(msg))
        err = self._localized_error_info(err)
        self._status_bar.show_error_summary(err.headline)
        self._show_async_warning(err.headline, err.detail)

    def _on_scrape(self, url: str) -> None:
        rate_remaining = self._download_ctrl.rate_limit_remaining()
        if getattr(self, "_rate_limit_network_paused", False) or rate_remaining > 0:
            self._on_rate_limit_waiting_ui(max(1.0, rate_remaining))
            return
        self._status_bar.show_indeterminate(t("scraping"))
        self._fetch_ctrl.scrape(url)

    def _on_scrape_done(self, urls: list) -> None:
        if not urls:
            self._status_bar.show_temporary(t("scrape_no_urls"), StatusKind.WARNING)
            return
        self._url_bar.set_url(urls[0])
        self._status_bar.show_temporary(
            t("scrape_multi_found", count=len(urls)), StatusKind.SUCCESS
        )

    def _on_redownload(self, record: DownloadRecord) -> None:
        self._url_bar.set_url(record.url)
        self.switchTo(self._queue_wrapper)
        # Re-enter through the normal admission boundary so a history action
        # cannot start fresh YouTube work during a shared rate-limit cooldown.
        self._start_fetch(record.url)

    def _on_open_folder(self, record: DownloadRecord) -> None:
        folder = Path(record.output_path).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ──────────────────────────────────────────────────────────────────────────
    # Search flow  (delegates to SearchController; AppWindow mediates drill-down)
    # ──────────────────────────────────────────────────────────────────────────

    def _on_search(self, query: str) -> None:
        rate_remaining = self._download_ctrl.rate_limit_remaining()
        if getattr(self, "_rate_limit_network_paused", False) or rate_remaining > 0:
            self._on_rate_limit_waiting_ui(max(1.0, rate_remaining))
            return
        self._search_ctrl.search(query, self._search_panel._current_platform)

    def _on_search_result_ready(self, result: SearchResult) -> None:
        card = self._search_panel.add_result(result)
        if result.thumbnail_url:
            tw = ThumbnailWorker(0, result.thumbnail_url, parent=self)
            tw.thumbnail_ready.connect(lambda _, data, c=card: c.set_thumbnail(data))
            tw.start()

    def _on_add_search_result_to_queue(self, result: SearchResult) -> None:
        incident_id = getattr(self, "_source_repair_incident_id", "")
        key = getattr(self, "_source_repair_key", "")
        if incident_id and key:
            if result.kind != ResultKind.TRACK:
                return
            job = self._download_ctrl.prepare_failure_source(
                incident_id, key, result.url
            )
            if job is None:
                self._show_async_warning(
                    t("search_source_repair_invalid_title"),
                    t("search_source_repair_invalid_text"),
                )
                return
            self._source_repair_jobs.append(job)
            self._source_repair_completed += 1
            InfoBar.success(
                title=t("search_source_repair_selected_title"),
                content=t("search_source_repair_selected_text", title=result.title),
                orient=Qt.Orientation.Horizontal,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000,
                parent=self,
            )
            QTimer.singleShot(0, self._show_next_failure_source_repair)
            return
        self._search_ctrl.add_to_queue(result)

    def _on_result_to_queue(self, meta) -> None:
        self._add_track_to_queue(meta)
        self.switchTo(self._queue_wrapper)

    def _on_search_drill_down(self, result: SearchResult) -> None:
        """Cross-controller: search drill-down triggers a fetch."""
        logger.debug(
            "[AppWindow] Drill-down: kind=%s url=%s", result.kind.value, result.url
        )
        self._last_playlist_title = (
            result.title if result.kind in (ResultKind.ALBUM, ResultKind.PLAYLIST) else ""
        )
        self._last_url_kind = (
            UrlKind.PLAYLIST if result.kind == ResultKind.PLAYLIST else
            (UrlKind.ALBUM   if result.kind == ResultKind.ALBUM    else UrlKind.ARTIST)
        )
        self._url_bar.set_url(result.url)
        self.switchTo(self._queue_wrapper)
        self._start_fetch(result.url)

    def _on_search_error(self, msg: str) -> None:
        err = classify_error(Exception(msg))
        err = self._localized_error_info(err)
        self._status_bar.show_error_summary(err.headline)
        self._show_async_warning(err.headline, err.detail)

    # ──────────────────────────────────────────────────────────────────────────
    # Queue card management  (AppWindow owns card creation and index routing)
    # ──────────────────────────────────────────────────────────────────────────

    def _add_track_to_queue(self, data) -> None:
        idx = len(self._queue_panel.get_all_cards()) + 1
        get = (
            lambda k, d="": data.get(k, d) if isinstance(data, dict)
            else getattr(data, k, d)
        )

        card = self._queue_panel.add_card(
            index=idx,
            title=get("title", "Unknown"),
            artist=get("artist", ""),
            duration=(
                (get("duration", "") or get("duration_str", ""))
                if isinstance(data, dict) else get("duration_str", "")
            ),
            platform=get("platform", "youtube"),
            track_url=(
                (get("track_url", "") or get("url", ""))
                if isinstance(data, dict) else get("url", "")
            ),
            album=get("album", ""),
            collection_title=get("collection_title", ""),
            parent_artist=get("parent_artist", ""),
            release_type=get("release_type", ""),
            album_index=get("album_index", 0),
            disc_number=get("disc_number", 0),
            disc_total=get("disc_total", 0),
            thumbnail_url=get("thumbnail_url", ""),
            category=get("category", ""),
            total_tracks=get("total_tracks", 0),
            duration_sec=get("duration_sec", None),
            spotify_id=get("spotify_id", ""),
            spotify_key_kind=get("spotify_key_kind", "spotify_id"),
            match_status=get("match_status", "matched"),
            resolution_error=get("resolution_error", ""),
            source_kind=get("source_kind", ""),
            source_url=get("source_url", ""),
        )

        card.remove_requested.connect(self._on_card_removed)
        card.pause_requested.connect(self._on_pause_track)
        card.resume_requested.connect(self._on_resume_track)

        self._index_to_card[idx] = card
        self._update_dl_bar()

        # Fast-start: fire the 8-track pre-resolve the instant the first 8
        # pending (two-stage Spotify) cards exist — mid-scrape, not after the
        # whole catalog finishes. One-shot per fetch (guarded in the helper),
        # so this never restarts the prefetch on every card.
        if get("match_status", "matched") == "pending" and not self._prefetch_started:
            self._prefetch_pending_count += 1
            if self._prefetch_pending_count >= 8:
                self._start_match_prefetch()

        thumb_url = get("thumbnail_url", "")
        if thumb_url:
            tw = ThumbnailWorker(idx, thumb_url, parent=self)
            self._thumb_workers.add(tw)
            tw.finished.connect(lambda t=tw: self._thumb_workers.discard(t))
            tw.thumbnail_ready.connect(
                lambda idx, data, c=card: self._set_card_thumb(c, data)
            )
            tw.start()

        return card

    def _restore_paused_batches(self) -> None:
        """On startup, reclaim abandoned download workspaces and restore any
        valid paused jobs so the user can Resume All where they left off.

        Fully guarded — a persistence/filesystem hiccup here must never block
        the app from starting. The sweep runs even when nothing is paused, so
        stale workspaces from crashed or completed batches are reclaimed."""
        try:
            def _factory(card_dict):
                card = self._add_track_to_queue(card_dict)
                if card is not None:
                    card.set_status("paused")
                return card

            restored = self._download_ctrl.restore_paused_on_startup(_factory)
            if restored:
                # Paused work is present — surface Resume All.
                self._queue_panel.set_pause_resume_state(True)
                self._status_bar.show_paused()
        except Exception:
            logger.exception("[AppWindow] Restoring paused batches failed")

    def _set_card_thumb(self, card: TrackCard, data: bytes) -> None:
        from PySide6.QtGui import QPixmap
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            card.set_thumbnail(px)

    def _on_track_thumbnail_update(self, queue_index: int, thumb_url: str) -> None:
        card = self._index_to_card.get(queue_index)
        if card:
            tw = ThumbnailWorker(queue_index, thumb_url, parent=self)
            self._thumb_workers.add(tw)
            tw.finished.connect(lambda t=tw: self._thumb_workers.discard(t))
            tw.thumbnail_ready.connect(
                lambda idx, data, c=card: self._set_card_thumb(c, data)
            )
            tw.start()

    def _on_selection_changed(self, count: int) -> None:
        total = len(self._queue_panel.get_all_cards())
        self._dl_bar.set_count(count, total)

    def _on_card_removed(self, queue_index: int) -> None:
        self._index_to_card.pop(queue_index, None)
        self._update_dl_bar()

    def _on_options_changed(self) -> None:
        pass

    def _update_dl_bar(self) -> None:
        cards    = self._queue_panel.get_all_cards()
        selected = self._queue_panel.get_selected_cards()
        self._dl_bar.set_count(len(selected), len(cards))

    # ──────────────────────────────────────────────────────────────────────────
    # Clipboard
    # ──────────────────────────────────────────────────────────────────────────

    def _on_clipboard_url(self, url: str) -> None:
        self._url_bar.set_url(url)
        InfoBar.info(
            title=t("clipboard_toast_title"),
            content=t("clipboard_toast_detected", url=url[:60]),
            orient=Qt.Orientation.Horizontal,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _on_clipboard_setting_change(self, checked: bool) -> None:
        self._cfg.clipboard_monitor = checked
        self._cfg.save()
        if checked:
            self._clipboard_worker.start()
        else:
            self._clipboard_worker.stop()
        self._url_bar.set_clipboard_monitor_active(checked)

    # ──────────────────────────────────────────────────────────────────────────
    # Updates
    # ──────────────────────────────────────────────────────────────────────────

    def _on_update_results(self, results: UpdateCheckResults) -> None:
        """Startup check finished. Show one prompt covering everything the
        user hasn't already dismissed or snoozed; stay silent otherwise
        (including on check failures — startup checks never nag)."""
        store = UpdateStateStore()

        app_release = None
        if results.app is not None and results.app.update_available:
            release = results.app.release
            if store.should_notify(app_update_id(release.version)):
                app_release = release

        component_updates = []
        if results.components is not None:
            component_updates = [
                status for status in results.components.updates
                if store.should_notify(
                    component_update_id(status.key, status.latest_version)
                )
            ]

        if app_release is None and not component_updates:
            return
        UpdatePromptDialog(
            store, app_release, component_updates, parent=self,
        ).exec()

    # ──────────────────────────────────────────────────────────────────────────
    # Cancel  (cancels all in-flight operations)
    # ──────────────────────────────────────────────────────────────────────────

    def _on_cancel(self) -> None:
        was_downloading = self._download_ctrl.is_downloading()
        self._cancel_download_error_requests()
        self._match_prefetcher.cancel()
        self._fetch_ctrl.cancel()
        self._search_ctrl.cancel()
        self._download_ctrl.cancel_all()
        if was_downloading:
            # The batch is shutting down — show "cancelling…" now; the batch's
            # own finish handler will replace it with the stopped summary.
            self._status_bar.show_cancelling()
        else:
            # Only a fetch/search was running — nothing terminal to summarize.
            self._status_bar.reset_to_idle()

    # ──────────────────────────────────────────────────────────────────────────
    # Settings
    # ──────────────────────────────────────────────────────────────────────────

    def _on_settings_saved(self) -> None:
        # Language change is restart-based for this stage: rebuilding every
        # widget tree (tooltips, dialog texts initialised in __init__) is
        # less reliable than a clean restart. See ui/i18n.py for the
        # language_changed signal that future live-retranslate work will use.
        if self._cfg.language != self._previous_language:
            new_lang = self._cfg.language
            restart_now = confirm(
                self,
                t("restart_required_title"),
                t("restart_required_msg"),
                accept_text=t("restart_now_btn"),
                cancel_text=t("restart_later_btn"),
            )
            if restart_now:
                request_language_restart(QApplication.instance(), new_lang)
                return
            # User declined — revert config so the in-memory state matches
            # what the user will see until they restart manually.
            self._cfg.language = self._previous_language
            self._cfg.save()
            try:
                self._settings_panel.refresh()
            except Exception:
                pass
            return

        try:
            self.setWindowTitle(t("app_name"))
        except Exception:
            pass
        try:
            self._settings_panel.refresh()
        except Exception:
            pass
        self._options_bar.apply_config(self._cfg)
        self._update_dl_bar()

    # ──────────────────────────────────────────────────────────────────────────
    # Window state
    # ──────────────────────────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        state_hex = self._cfg.window_state
        if state_hex:
            try:
                self.restoreGeometry(QByteArray.fromHex(state_hex.encode()))
            except Exception:
                pass

    def _save_state(self) -> None:
        self._cfg.window_state = self.saveGeometry().toHex().data().decode()
        self._cfg.save()

    # ──────────────────────────────────────────────────────────────────────────
    # Resize event
    # ──────────────────────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
            self.titleBar.move(0, 0)
            self.titleBar.resize(self.width() - 46, self.titleBar.height())

    # ──────────────────────────────────────────────────────────────────────────
    # Click-to-defocus
    # ──────────────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        # Only reached when the click lands on a plain, non-interactive area
        # (labels, background frames) — Qt bubbles ignored mouse events up
        # to here. Clicking an actual input/dropdown/table already moves
        # focus there on its own; this just clears a stuck focus ring (e.g.
        # the URL field's underline) when the user clicks empty space.
        fw = QApplication.focusWidget()
        if fw is not None:
            fw.clearFocus()
        super().mousePressEvent(event)

    # ──────────────────────────────────────────────────────────────────────────
    # Close event
    # ──────────────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """
        Clean shutdown sequence:
        1. Tray intercept (if enabled)
        2. Persist window state and queue
        3. Stop non-threaded monitors (clipboard, network)
        4. Cancel + join threaded workers (download first, then others)
        5. Unregister global hotkeys
        6. Close the database (via ServiceContainer)
        7. Accept the close event
        """
        # 1. Tray intercept
        if self._cfg.tray_on_close and self._tray and self._tray.isVisible():
            event.ignore()
            self.hide()
            self._tray.showMessage(
                t("tray_minimized_title"),
                t("tray_minimized_message"),
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return

        # Pending Tag Editor proposals have their own lifecycle.  The dialog
        # continuation re-enters close() only after Apply/Keep Draft/Discard;
        # Cancel leaves the window and workspace untouched.
        if not self._metadata_ctrl.guard_lifecycle_action("close", self.close):
            event.ignore()
            return

        metadata_panel = getattr(self, "_metadata_panel", None)
        if metadata_panel is not None and not metadata_panel.shutdown_artwork_workers():
            event.ignore()
            logger.warning("[AppWindow] Artwork thumbnail worker still finishing; close deferred")
            return

        # 1b. Bounded, event-loop-safe shutdown for any in-flight Tag Editor
        #     worker, including ReplayGain-only analysis. Defer
        #     the close, cancel at the next safe boundary, and retry the close
        #     when the worker has actually finished (TE-SAFE-13).
        if (
            self._metadata_ctrl.has_active_shutdown_work()
            and not self._metadata_ctrl.request_shutdown()
        ):
            event.ignore()
            if not getattr(self, "_metadata_shutdown_wired", False):
                self._metadata_shutdown_wired = True
                self._metadata_ctrl.shutdown_ready.connect(self.close)
                # Bounded timeout: keep the app open and inform the user rather
                # than ever killing the running thread (defect 6).
                self._metadata_ctrl.shutdown_timed_out.connect(
                    self._on_metadata_shutdown_timeout)
            logger.info("[AppWindow] Deferring close — Tag Editor disk op finishing safely")
            return

        logger.info("[AppWindow] closeEvent — beginning shutdown sequence")

        # Match prefetch owns Python threads and a resolver executor. Keep the
        # UI and services alive until cooperative cancellation reaches the
        # provider boundary, polling from Qt instead of blocking this thread.
        prefetcher = getattr(self, "_match_prefetcher", None)
        if prefetcher is not None and not prefetcher.shutdown(timeout_s=0.0):
            event.ignore()
            if not getattr(self, "_prefetch_shutdown_retry_scheduled", False):
                self._prefetch_shutdown_retry_scheduled = True

                def _retry_close_after_prefetch() -> None:
                    self._prefetch_shutdown_retry_scheduled = False
                    self.close()

                QTimer.singleShot(50, _retry_close_after_prefetch)
            logger.info("[AppWindow] Deferring close while match prefetch finishes safely")
            return
        self._prefetch_shutdown_retry_scheduled = False

        # Download workers may be parked in an hour-long cooldown or waiting
        # on a user decision. Resolve dialogs first, then cooperatively cancel
        # the main worker and every per-track resume. Keep the QObject tree
        # alive until all QThreads have actually exited; destroying a running
        # QThread is a native Qt abort/crash.
        cancel_recovery = getattr(self, "_cancel_download_error_requests", None)
        if callable(cancel_recovery):
            cancel_recovery()
        request_download_shutdown = getattr(
            self._download_ctrl, "request_shutdown", None,
        )
        downloads_stopped = (
            request_download_shutdown()
            if callable(request_download_shutdown)
            else True
        )
        if not downloads_stopped:
            event.ignore()
            if not getattr(self, "_download_shutdown_retry_scheduled", False):
                self._download_shutdown_retry_scheduled = True

                def _retry_close_after_downloads() -> None:
                    self._download_shutdown_retry_scheduled = False
                    self.close()

                QTimer.singleShot(50, _retry_close_after_downloads)
            logger.info("[AppWindow] Deferring close while downloads stop safely")
            return
        self._download_shutdown_retry_scheduled = False

        # 2. Persist state
        self._save_state()
        self._save_queue_state()

        # 3. Stop non-threaded monitors
        if hasattr(self, "_net_monitor") and self._net_monitor:
            self._net_monitor.stop()
        if self._clipboard_worker:
            self._clipboard_worker.stop()

        # 4. Cancel + join remaining non-download workers. Download workers
        # were drained asynchronously above so no running QThread is destroyed.
        fetch_worker  = self._fetch_ctrl._fetch_worker    # noqa: SLF001
        search_worker = self._search_ctrl._search_worker  # noqa: SLF001
        scraper_worker= self._fetch_ctrl._scraper_worker  # noqa: SLF001
        for attr_name, w in (
            ("FetchWorker",   fetch_worker),
            ("SearchWorker",  search_worker),
            ("ScraperWorker", scraper_worker),
        ):
            if w and w.isRunning():
                if hasattr(w, "cancel"):
                    w.cancel()
                finished = w.wait(2000)
                if not finished:
                    logger.warning("[AppWindow] %s did not finish within 2s", attr_name)

        # Cancel metadata workers
        self._metadata_ctrl.cancel_scan()
        self._metadata_ctrl.cancel_apply()

        # The startup update check can still be inside an HTTP timeout when a
        # user opens and immediately closes the application. A running QThread
        # must never be destroyed with its parent. Hide the already-closing
        # window, request cooperative stop, and finish the same close sequence
        # when the worker exits instead of blocking the GUI thread.
        update_worker = getattr(self, "_update_worker", None)
        if update_worker is not None and update_worker.isRunning():
            event.ignore()
            self._quit_after_deferred_close = True
            self.hide()
            if not getattr(self, "_update_shutdown_wired", False):
                self._update_shutdown_wired = True
                update_worker.requestInterruption()
                update_worker.finished.connect(self.close)
            logger.info("[AppWindow] Deferring close while update check stops")
            return
        self._update_shutdown_wired = False

        # 5. Hotkeys
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass

        # 6. Tray hide + DB close
        if self._tray:
            self._tray.hide()
        if hasattr(self, "_svc"):
            self._svc.close()
            logger.info("[AppWindow] Services closed — shutdown complete")

        event.accept()
        if getattr(self, "_quit_after_deferred_close", False):
            # Hiding the last window while a child QThread drains means Qt no
            # longer emits its normal last-visible-window auto-quit when the
            # retried close is accepted. Exit only after every cleanup stage
            # above has completed successfully.
            QTimer.singleShot(0, QApplication.quit)
