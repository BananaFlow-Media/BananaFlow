"""
ui/panels/settings_panel.py  –  Settings sub-interface  (v3)
=============================================================
Changelog v3
------------
New setting groups added (all backward-compatible – no removed cards):
  * Accent Color picker (swatch row under Appearance)
  * Accessibility Mode toggle
  * Advanced Audio group:
      - SponsorBlock toggle
      - Lyrics Downloader toggle (Advanced, default OFF)
      - Replay Gain toggle (Advanced, default OFF)
      - Square Thumbnails toggle (Advanced, default OFF)
      - MusicBrainz enrichment toggle
  * Playlist Behaviour group:
      - Playlist sub-folders toggle
      - Track index prefix toggle
      - Duplicate action selector (skip / warn / overwrite)
  * System Integration group:
      - Tray on close toggle
      - Global hotkeys toggle
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox, QBoxLayout, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    ComboBoxSettingCard, ExpandLayout,
    FluentIcon, HyperlinkCard,
    InfoBar, InfoBarPosition,
    OptionsConfigItem, OptionsValidator,
    PushSettingCard, SegmentedWidget, SettingCardGroup,
    SwitchSettingCard,
)

from config import AppConfig
from core.update_checker import CURRENT_VERSION
from core.update_state import UpdateStateStore
from core.youtube_doctor import run_youtube_doctor
from ui.dialogs.update_prompt_dialog import UpdatePromptDialog
from ui.dialogs.styled_dialog import confirm
from ui.dialogs.youtube_doctor_dialog import show_youtube_doctor_dialog
from ui.workers.update_worker import UpdateCheckResults, UpdateWorker
from ui.direction import force_ltr_input
from ui.i18n import t
from ui.theme_manager import ACCENT_PALETTE, ThemeManager
from utils.cookie_validator import check_cookies_valid, merge_cookies_file
from utils.paths import get_app_cookies_path
from utils.security import delete_stored_auth_data


# ──────────────────────────────────────────────────────────────────────────────
# SettingsPanel
# ──────────────────────────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    """
    Full settings sub-interface for FluentWindow.

    Organised into three sections a non-technical user can navigate:
    **Basic** (appearance, language, downloads, sign-in help),
    **Advanced** (playlist, features, system, audio processing, search),
    and **Expert & Diagnostics** (YouTube Doctor, manual update checks,
    cookie files, About). A SegmentedWidget at the top switches between
    the three scrollable pages; no setting was removed in the split.

    Signals
    -------
    theme_changed(str)             – new theme name
    accent_changed(str)            – new accent hex
    clipboard_monitor_changed(bool)
    accessibility_changed(bool)
    settings_saved()
    """

    theme_changed              = Signal(str)
    accent_changed             = Signal(str)
    clipboard_monitor_changed  = Signal(bool)
    accessibility_changed      = Signal(bool)
    login_fix_requested        = Signal()  # NEW
    settings_saved             = Signal()

    def __init__(
        self,
        config: AppConfig,
        theme:  ThemeManager,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._cfg   = config
        self._theme = theme
        self._build()

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    # ── Public API ─────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        self._thumb_card.setChecked(self._cfg.embed_thumbnail)
        self._meta_card.setChecked(self._cfg.embed_metadata)
        self._clip_card.setChecked(self._cfg.clipboard_monitor)
        self._update_card.setChecked(self._cfg.check_updates)
        self._singles_subfolder_card.setChecked(self._cfg.singles_subfolder)
        self._youtube_fast_mode_card.setChecked(self._cfg.youtube_reliability_mode == "fast")
        self._theme_card.setContent(self._theme.theme_display_label())
        try:
            self._lang_card.setValue(self._cfg.language)
        except Exception:
            pass
        self._cookies_card.setContent(
            t("cookies_file_configured") if self._cfg.cookies_file else t("cookies_file_unset")
        )
        try:
            self._browser_card.setValue(self._cfg.cookies_browser)
            self._youtube_results_card.setValue(self._cfg.youtube_max_results)
            self._spotify_results_card.setValue(self._cfg.spotify_max_results)
            self._spotify_proxy_card.setText(self._cfg.proxy_server_url)
            self._spotify_proxy_token_card.setText(self._cfg.spotify_app_api_key)
            self._youtube_proxy_card.setText(self._cfg.get("youtube_proxy_url", ""))
        except Exception:
            pass
        QTimer.singleShot(0, self._adjust_layouts)

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setObjectName("settingsPage")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Section navigation: Basic / Advanced / Expert ──────────────────────
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(36, 20, 36, 4)
        self._section_nav = SegmentedWidget(self)
        nav_row.addWidget(self._section_nav)
        nav_row.addStretch(1)
        outer.addLayout(nav_row)

        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)

        self._section_pages: dict[str, QScrollArea] = {}
        self._page_contents: list[QWidget] = []
        basic_content,    basic_lay    = self._add_section_page("basic",    t("settings_section_basic"))
        advanced_content, advanced_lay = self._add_section_page("advanced", t("settings_section_advanced"))
        expert_content,   expert_lay   = self._add_section_page("expert",   t("settings_section_expert"))
        self._section_nav.currentItemChanged.connect(self._on_section_changed)
        self._section_nav.setCurrentItem("basic")

        self._apply_theme()

        # ════════════════════════ BASIC ════════════════════════
        content = basic_content
        layout = basic_lay

        # ── 1. Appearance ──────────────────────────────────────────────────────
        appearance_grp = SettingCardGroup(t("appearance"), content)

        self._theme_card = PushSettingCard(
            text=self._theme.next_theme_label(),
            icon=FluentIcon.BRUSH,
            title=t("switch_theme"),
            content=self._theme.theme_display_label(),
            parent=appearance_grp,
        )
        self._theme_card.clicked.connect(self._on_theme_click)
        appearance_grp.addSettingCard(self._theme_card)

        # Accent color swatch row
        accent_card = _AccentPickerCard(
            current_accent=self._cfg.accent_color,
            parent=appearance_grp,
        )
        accent_card.accent_changed.connect(self._on_accent_change)
        appearance_grp.addSettingCard(accent_card)

        self._lang_card = _LanguageSettingCard(
            icon=FluentIcon.LANGUAGE,
            title=t("language"),
            content=t("select_language"),
            value=self._cfg.language,
            options=(("en", "English"), ("he", "עברית")),
            parent=appearance_grp,
        )
        self._lang_card.value_changed.connect(self._on_language_change)
        appearance_grp.addSettingCard(self._lang_card)

        # Accessibility mode
        self._a11y_card = SwitchSettingCard(
            icon=FluentIcon.PEOPLE,
            title=t("accessibility_mode"),
            content=t("accessibility_mode_desc"),
            parent=appearance_grp,
        )
        self._a11y_card.setChecked(self._cfg.accessibility_mode)
        self._a11y_card.checkedChanged.connect(self._on_accessibility_toggle)
        appearance_grp.addSettingCard(self._a11y_card)

        layout.addWidget(appearance_grp)

        # ── 2. Downloads ───────────────────────────────────────────────────────
        downloads_grp = SettingCardGroup(t("downloads_group"), content)

        self._thumb_card = SwitchSettingCard(
            icon=FluentIcon.PHOTO,
            title=t("embed_thumbnail"),
            content=t("embed_thumbnail_desc"),
            parent=downloads_grp,
        )
        self._thumb_card.setChecked(self._cfg.embed_thumbnail)
        self._thumb_card.checkedChanged.connect(
            lambda v: self._persist("embed_thumbnail", v)
        )
        downloads_grp.addSettingCard(self._thumb_card)

        self._meta_card = SwitchSettingCard(
            icon=FluentIcon.TAG,
            title=t("embed_metadata"),
            content=t("embed_metadata_desc"),
            parent=downloads_grp,
        )
        self._meta_card.setChecked(self._cfg.embed_metadata)
        self._meta_card.checkedChanged.connect(
            lambda v: self._persist("embed_metadata", v)
        )
        downloads_grp.addSettingCard(self._meta_card)

        self._parallel_card = _SpinnerSettingCard(
            icon=FluentIcon.SPEED_HIGH,
            title=t("concurrent_downloads"),
            content=t("concurrent_downloads_desc"),
            value=self._cfg.max_parallel_downloads,
            min_val=1,
            max_val=6,
            parent=downloads_grp,
        )
        self._parallel_card.value_changed.connect(
            lambda v: self._persist("max_parallel_downloads", v)
        )
        downloads_grp.addSettingCard(self._parallel_card)

        layout.addWidget(downloads_grp)

        # ── Sign-in help (the everyday recovery action for restricted media) ──
        signin_grp = SettingCardGroup(t("signin_group"), content)

        self._login_fix_card = PushSettingCard(
            text=t("external_login_now_btn"),
            icon=FluentIcon.PEOPLE,
            title=t("external_login_title"),
            content=t("external_login_desc"),
            parent=signin_grp,
        )
        self._login_fix_card.clicked.connect(self.login_fix_requested)
        signin_grp.addSettingCard(self._login_fix_card)

        layout.addWidget(signin_grp)

        # ════════════════════════ ADVANCED ════════════════════════
        content = advanced_content
        layout = advanced_lay

        # ── 3. Playlist Behaviour ──────────────────────────────────────────────
        playlist_grp = SettingCardGroup(t("playlist_behaviour"), content)

        self._subfolder_card = SwitchSettingCard(
            icon=FluentIcon.FOLDER,
            title=t("playlist_subfolders"),
            content=t("playlist_subfolders_desc"),
            parent=playlist_grp,
        )
        self._subfolder_card.setChecked(self._cfg.playlist_subfolders)
        self._subfolder_card.checkedChanged.connect(
            lambda v: self._persist("playlist_subfolders", v)
        )
        playlist_grp.addSettingCard(self._subfolder_card)

        self._singles_subfolder_card = SwitchSettingCard(
            icon=FluentIcon.FOLDER,
            title=t("singles_subfolder"),
            content=t("singles_subfolder_desc"),
            parent=playlist_grp,
        )
        self._singles_subfolder_card.setChecked(self._cfg.singles_subfolder)
        self._singles_subfolder_card.checkedChanged.connect(
            lambda v: self._persist("singles_subfolder", v)
        )
        playlist_grp.addSettingCard(self._singles_subfolder_card)

        self._index_card = SwitchSettingCard(
            icon=FluentIcon.LABEL,
            title=t("track_index_prefix"),
            content=t("track_index_prefix_desc"),
            parent=playlist_grp,
        )
        self._index_card.setChecked(self._cfg.playlist_index_prefix)
        self._index_card.checkedChanged.connect(
            lambda v: self._persist("playlist_index_prefix", v)
        )
        playlist_grp.addSettingCard(self._index_card)

        self._dup_card = _LanguageSettingCard(
            icon=FluentIcon.COPY,
            title=t("duplicate_detection"),
            content=t("duplicate_detection_desc"),
            value=self._cfg.duplicate_action,
            options=(
                ("skip",      t("duplicate_skip")),
                ("warn",      t("duplicate_warn")),
                ("overwrite", t("duplicate_overwrite")),
            ),
            parent=playlist_grp,
        )
        self._dup_card.value_changed.connect(
            lambda v: self._persist("duplicate_action", v)
        )
        playlist_grp.addSettingCard(self._dup_card)

        layout.addWidget(playlist_grp)

        # ── 4. Features ────────────────────────────────────────────────────────
        features_grp = SettingCardGroup(t("features"), content)

        self._clip_card = SwitchSettingCard(
            icon=FluentIcon.COPY,
            title=t("clipboard_monitor"),
            content=t("clipboard_monitor_desc"),
            parent=features_grp,
        )
        self._clip_card.setChecked(self._cfg.clipboard_monitor)
        self._clip_card.checkedChanged.connect(self._on_clipboard_toggle)
        features_grp.addSettingCard(self._clip_card)

        self._update_card = SwitchSettingCard(
            icon=FluentIcon.UPDATE,
            title=t("check_updates"),
            content=t("check_updates_desc"),
            parent=features_grp,
        )
        self._update_card.setChecked(self._cfg.check_updates)
        self._update_card.checkedChanged.connect(
            lambda v: self._persist("check_updates", v)
        )
        features_grp.addSettingCard(self._update_card)

        self._browser_card = _LanguageSettingCard(
            icon=FluentIcon.VPN,
            title=t("browser_cookies"),
            content=t("browser_cookies_desc"),
            value=self._cfg.cookies_browser,
            options=(
                ("",        t("disabled")),
                ("chrome",  "Google Chrome"),
                ("firefox", "Mozilla Firefox"),
                ("edge",    "Microsoft Edge"),
                ("brave",   "Brave"),
                ("safari",  "Safari"),
            ),
            parent=features_grp,
        )
        self._browser_card.value_changed.connect(
            lambda v: self._persist("cookies_browser", v)
        )
        features_grp.addSettingCard(self._browser_card)

        layout.addWidget(features_grp)

        # ── 5. System Integration ──────────────────────────────────────────────
        system_grp = SettingCardGroup(t("system_integration"), content)

        self._tray_card = SwitchSettingCard(
            icon=FluentIcon.MINIMIZE,
            title=t("minimise_to_tray"),
            content=t("minimise_to_tray_desc"),
            parent=system_grp,
        )
        self._tray_card.setChecked(self._cfg.tray_on_close)
        self._tray_card.checkedChanged.connect(
            lambda v: self._persist("tray_on_close", v)
        )
        system_grp.addSettingCard(self._tray_card)

        self._hotkeys_card = SwitchSettingCard(
            icon=FluentIcon.COMMAND_PROMPT,
            title=t("global_hotkeys"),
            content=t("global_hotkeys_desc"),
            parent=system_grp,
        )
        self._hotkeys_card.setChecked(self._cfg.global_hotkeys_enabled)
        self._hotkeys_card.checkedChanged.connect(
            lambda v: self._persist("global_hotkeys_enabled", v)
        )
        system_grp.addSettingCard(self._hotkeys_card)

        layout.addWidget(system_grp)

        # ── 6. Advanced Audio Processing ───────────────────────────────────────
        advanced_grp = SettingCardGroup(t("advanced_audio_processing"), content)

        # SponsorBlock
        self._sb_card = SwitchSettingCard(
            icon=FluentIcon.REMOVE,
            title=t("sponsorblock_title"),
            content=t("sponsorblock_desc"),
            parent=advanced_grp,
        )
        self._sb_card.setChecked(self._cfg.sponsorblock_enabled)
        self._sb_card.checkedChanged.connect(
            lambda v: self._persist("sponsorblock_enabled", v)
        )
        advanced_grp.addSettingCard(self._sb_card)

        # MusicBrainz
        self._mb_card = SwitchSettingCard(
            icon=FluentIcon.SEARCH,
            title=t("musicbrainz_title"),
            content=t("musicbrainz_desc"),
            parent=advanced_grp,
        )
        self._mb_card.setChecked(self._cfg.musicbrainz_enabled)
        self._mb_card.checkedChanged.connect(
            lambda v: self._persist("musicbrainz_enabled", v)
        )
        advanced_grp.addSettingCard(self._mb_card)

        # Lyrics (disabled by default)
        self._lyrics_card = SwitchSettingCard(
            icon=FluentIcon.DOCUMENT,
            title=t("lyrics_title"),
            content=t("lyrics_desc"),
            parent=advanced_grp,
        )
        self._lyrics_card.setChecked(self._cfg.lyrics_enabled)
        self._lyrics_card.checkedChanged.connect(
            lambda v: self._persist("lyrics_enabled", v)
        )
        advanced_grp.addSettingCard(self._lyrics_card)

        # Replay Gain (disabled by default)
        self._rg_card = SwitchSettingCard(
            icon=FluentIcon.VOLUME,
            title=t("replay_gain_title"),
            content=t("replay_gain_desc"),
            parent=advanced_grp,
        )
        self._rg_card.setChecked(self._cfg.replay_gain_enabled)
        self._rg_card.checkedChanged.connect(
            lambda v: self._persist("replay_gain_enabled", v)
        )
        advanced_grp.addSettingCard(self._rg_card)

        # Square Thumbnails (disabled by default)
        self._sq_card = SwitchSettingCard(
            icon=FluentIcon.PHOTO,
            title=t("square_thumbnails_title"),
            content=t("square_thumbnails_desc"),
            parent=advanced_grp,
        )
        self._sq_card.setChecked(self._cfg.square_thumbnails)
        self._sq_card.checkedChanged.connect(
            lambda v: self._persist("square_thumbnails", v)
        )
        advanced_grp.addSettingCard(self._sq_card)

        # Expand Thumbnails (disabled by default)
        self._expand_card = SwitchSettingCard(
            icon=FluentIcon.PHOTO,
            title=t("expand_square_to_rectangle_title"),
            content=t("expand_square_to_rectangle_desc"),
            parent=advanced_grp,
        )
        self._expand_card.setChecked(self._cfg.expand_thumbnails)
        self._expand_card.checkedChanged.connect(
            lambda v: self._persist("expand_thumbnails", v)
        )
        advanced_grp.addSettingCard(self._expand_card)

        layout.addWidget(advanced_grp)

        # ════════════════════════ EXPERT & DIAGNOSTICS ════════════════════════
        content = expert_content
        layout = expert_lay

        # ── 7b. Diagnostics ──────────────────────────────────────────────────────
        diag_grp = SettingCardGroup(t("youtube_doctor_group"), content)

        self._youtube_doctor_card = PushSettingCard(
            text=t("youtube_doctor_run_btn"),
            icon=FluentIcon.HELP,
            title=t("youtube_doctor_card_title"),
            content=t("youtube_doctor_card_desc"),
            parent=diag_grp,
        )
        self._youtube_doctor_card.clicked.connect(self._on_run_youtube_doctor)
        diag_grp.addSettingCard(self._youtube_doctor_card)

        self._youtube_fast_mode_card = SwitchSettingCard(
            icon=FluentIcon.SPEED_HIGH,
            title=t("youtube_fast_mode_title"),
            content=t("youtube_fast_mode_desc"),
            parent=diag_grp,
        )
        self._youtube_fast_mode_card.setChecked(self._cfg.youtube_reliability_mode == "fast")
        self._youtube_fast_mode_card.checkedChanged.connect(self._on_youtube_fast_mode_toggle)
        diag_grp.addSettingCard(self._youtube_fast_mode_card)

        layout.addWidget(diag_grp)

        # ── 7c. Updates ──────────────────────────────────────────────────────────
        updates_grp = SettingCardGroup(t("updates_group"), content)

        self._check_app_updates_card = PushSettingCard(
            text=t("update_check_btn"),
            icon=FluentIcon.UPDATE,
            title=t("check_app_updates_title"),
            content=t("check_app_updates_desc"),
            parent=updates_grp,
        )
        self._check_app_updates_card.clicked.connect(self._on_check_app_updates)
        updates_grp.addSettingCard(self._check_app_updates_card)

        self._check_component_updates_card = PushSettingCard(
            text=t("update_check_btn"),
            icon=FluentIcon.SYNC,
            title=t("check_component_updates_title"),
            content=t("check_component_updates_desc"),
            parent=updates_grp,
        )
        self._check_component_updates_card.clicked.connect(
            self._on_check_component_updates
        )
        updates_grp.addSettingCard(self._check_component_updates_card)

        layout.addWidget(updates_grp)

        # ── 7. Authentication / Cookies (cookie-file management) ──────────────
        auth_grp = SettingCardGroup(t("authentication"), content)

        self._cookies_card = PushSettingCard(
            text=t("browse"),
            icon=FluentIcon.CERTIFICATE,
            title=t("cookies_file"),
            content=(
                t("cookies_file_configured")
                if self._cfg.cookies_file
                else t("cookies_file_unset")
            ),
            parent=auth_grp,
        )
        self._cookies_card.clicked.connect(self._on_browse_cookies)
        auth_grp.addSettingCard(self._cookies_card)

        self._clear_cookies_card = PushSettingCard(
            text=t("clear"),
            icon=FluentIcon.DELETE,
            title=t("clear_cookies"),
            content=t("clear_cookies_desc"),
            parent=auth_grp,
        )
        self._clear_cookies_card.clicked.connect(self._on_clear_cookies)
        auth_grp.addSettingCard(self._clear_cookies_card)

        layout.addWidget(auth_grp)

        # ════════════════════════ ADVANCED (continued) ════════════════════════
        content = advanced_content
        layout = advanced_lay

        # ── 8. Search settings ─────────────────────────────────────────────────
        search_grp = SettingCardGroup(t("search_group"), content)

        self._youtube_results_card = _SpinnerSettingCard(
            icon=FluentIcon.SEARCH,
            title=t("max_youtube_results"),
            content=t("max_youtube_results_desc"),
            value=self._cfg.youtube_max_results,
            min_val=1,
            max_val=100,
            parent=search_grp,
        )
        self._youtube_results_card.value_changed.connect(
            lambda v: self._persist("youtube_max_results", v)
        )
        search_grp.addSettingCard(self._youtube_results_card)

        self._spotify_results_card = _SpinnerSettingCard(
            icon=FluentIcon.SEARCH,
            title=t("max_spotify_results"),
            content=t("max_spotify_results_desc"),
            value=self._cfg.spotify_max_results,
            min_val=1,
            max_val=100,
            parent=search_grp,
        )
        self._spotify_results_card.value_changed.connect(
            lambda v: self._persist("spotify_max_results", v)
        )
        search_grp.addSettingCard(self._spotify_results_card)

        self._spotify_proxy_card = _TextSettingCard(
            icon=FluentIcon.GLOBE,
            title=t("spotify_proxy"),
            content=t("spotify_proxy_desc"),
            value=self._cfg.proxy_server_url,
            parent=search_grp,
        )
        self._spotify_proxy_card.value_changed.connect(
            lambda v: self._persist("proxy_server_url", v)
        )
        search_grp.addSettingCard(self._spotify_proxy_card)

        self._spotify_proxy_token_card = _TextSettingCard(
            icon=FluentIcon.VPN,
            title=t("spotify_proxy_api_key"),
            content=t("spotify_proxy_api_key_desc"),
            value=self._cfg.spotify_app_api_key,
            secret=True,
            parent=search_grp,
        )
        self._spotify_proxy_token_card.value_changed.connect(
            lambda v: self._persist("spotify_app_api_key", v)
        )
        search_grp.addSettingCard(self._spotify_proxy_token_card)

        self._youtube_proxy_card = _TextSettingCard(
            icon=FluentIcon.VPN,
            title=t("youtube_proxy_title"),
            content=t("youtube_proxy_desc"),
            value=self._cfg.get("youtube_proxy_url", ""),
            parent=search_grp,
        )
        self._youtube_proxy_card.value_changed.connect(
            lambda v: self._persist("youtube_proxy_url", v)
        )
        search_grp.addSettingCard(self._youtube_proxy_card)

        layout.addWidget(search_grp)

        # ════════════════════════ EXPERT (continued) ════════════════════════
        content = expert_content
        layout = expert_lay

        # ── 9. About ───────────────────────────────────────────────────────────
        about_grp = SettingCardGroup(t("about"), content)
        about_grp.addSettingCard(HyperlinkCard(
            url="https://github.com/BananaFlow-Media/BananaFlow",
            text="GitHub",
            icon=FluentIcon.GITHUB,
            title=t("about_app"),
            content=f"BananaFlow  v{CURRENT_VERSION}",
            parent=about_grp,
        ))
        layout.addWidget(about_grp)

        # After all cards are built and parented, refresh layout once more.
        self._apply_theme()
        QTimer.singleShot(0, self._adjust_layouts)

    def _add_section_page(self, route_key: str, title: str) -> tuple[QWidget, QVBoxLayout]:
        """Create one scrollable settings page and its segmented-nav entry."""
        page = QScrollArea(self._stack)
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(36, 16, 36, 40)
        lay.setSpacing(20)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        page.setWidget(content)

        self._stack.addWidget(page)
        self._section_pages[route_key] = page
        self._page_contents.append(content)
        self._section_nav.addItem(routeKey=route_key, text=title)
        return content, lay

    def _on_section_changed(self, route_key: str) -> None:
        page = self._section_pages.get(route_key)
        if page is not None:
            self._stack.setCurrentWidget(page)

    def _apply_theme(self) -> None:
        for content in getattr(self, "_page_contents", []):
            content.update()

    def _apply_card_alignment(self, card, rtl: bool) -> None:
        """Apply layout direction, margins, and alignments to a card based on RTL state."""
        if isinstance(card, _AccentPickerCard):
            card.apply_direction(rtl)
            return

        # Force LTR layout direction on the card container itself to prevent
        # double-mirroring of the horizontal QBoxLayout direction.
        card.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        # 1. Defensively set horizontal layout direction and margins
        h_layout = getattr(card, "hBoxLayout", None) or (card.layout() if isinstance(card.layout(), QHBoxLayout) else None)
        if h_layout is not None:
            try:
                if rtl:
                    h_layout.setDirection(QBoxLayout.Direction.RightToLeft)
                    h_layout.setContentsMargins(0, 0, 16, 0)
                else:
                    h_layout.setDirection(QBoxLayout.Direction.LeftToRight)
                    h_layout.setContentsMargins(16, 0, 0, 0)
            except Exception:
                pass

        # 2. Defensively set alignment of icon label
        icon_label = getattr(card, "iconLabel", None)
        if icon_label is not None:
            try:
                align_icon = (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if rtl else (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                if h_layout is not None:
                    h_layout.setAlignment(icon_label, align_icon)
            except Exception:
                pass

        # 3. Defensively set alignment of internal vertical text layout
        v_layout = getattr(card, "vBoxLayout", None)
        if v_layout is not None:
            try:
                align_v = (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if rtl else (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                v_layout.setAlignment(align_v)

                # Defensively align children inside the v_layout so they align right/left
                title_label = getattr(card, "titleLabel", None)
                if title_label is not None and not isinstance(card, (_SpinnerSettingCard, _TextSettingCard, _LanguageSettingCard)):
                    v_layout.setAlignment(title_label, align_v)
                content_label = getattr(card, "contentLabel", None)
                if content_label is not None and not isinstance(card, (_SpinnerSettingCard, _TextSettingCard, _LanguageSettingCard)):
                    v_layout.setAlignment(content_label, align_v)

                # Support custom card fallback attributes
                title_lbl_custom = getattr(card, "_title_lbl", None)
                if title_lbl_custom is not None and not isinstance(card, (_SpinnerSettingCard, _TextSettingCard, _LanguageSettingCard)):
                    v_layout.setAlignment(title_lbl_custom, align_v)
                sub_lbl_custom = getattr(card, "_sub_lbl", None)
                if sub_lbl_custom is not None and not isinstance(card, (_SpinnerSettingCard, _TextSettingCard, _LanguageSettingCard)):
                    v_layout.setAlignment(sub_lbl_custom, align_v)
            except Exception:
                pass

        # 4. Defensively set layout direction and alignment of labels
        align_text = (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if rtl else (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        
        labels_to_adjust = [
            getattr(card, "titleLabel", None),
            getattr(card, "contentLabel", None),
            getattr(card, "_title_lbl", None),
            getattr(card, "_sub_lbl", None)
        ]
        for lbl in labels_to_adjust:
            if lbl is not None:
                try:
                    lbl.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
                    lbl.setAlignment(align_text)
                except Exception:
                    pass

        # Defensively set child QComboBox layout directions to RTL if needed
        from PySide6.QtWidgets import QComboBox
        for combo in card.findChildren(QComboBox):
            try:
                combo.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
            except Exception:
                pass

    def _adjust_layouts(self) -> None:
        """Walk all setting cards and apply proper RTL/LTR alignment rules."""
        is_hebrew = (self._cfg.language == "he")
        
        # 1. Walk and adjust all standard QFluentWidgets SettingCards
        from qfluentwidgets import SettingCard
        for card in self.findChildren(SettingCard):
            self._apply_card_alignment(card, is_hebrew)

        # 2. Walk and adjust custom cards
        custom_classes = (_LanguageSettingCard, _SpinnerSettingCard, _TextSettingCard, _AccentPickerCard)
        for cls in custom_classes:
            for card in self.findChildren(cls):
                self._apply_card_alignment(card, is_hebrew)

        # 3. Walk and adjust SettingCardGroup titles
        from qfluentwidgets import SettingCardGroup
        for grp in self.findChildren(SettingCardGroup):
            if hasattr(grp, "titleLabel") and grp.titleLabel is not None:
                try:
                    grp.titleLabel.setLayoutDirection(Qt.LayoutDirection.RightToLeft if is_hebrew else Qt.LayoutDirection.LeftToRight)
                    align_text = (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if is_hebrew else (Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    grp.titleLabel.setAlignment(align_text)
                except Exception:
                    pass

    # ── Handlers ───────────────────────────────────────────────────────────────

    def _on_theme_click(self) -> None:
        new_theme = self._theme.cycle()
        self._theme_card.setContent(self._theme.theme_display_label())
        self._theme_card.button.setText(self._theme.next_theme_label())
        self.theme_changed.emit(new_theme)
        self.settings_saved.emit()

    def _on_accent_change(self, hex_color: str) -> None:
        self._theme.set_accent(hex_color)
        self._persist("accent_color", hex_color)
        self.accent_changed.emit(hex_color)

    def _on_clipboard_toggle(self, checked: bool) -> None:
        self._persist("clipboard_monitor", checked)
        self.clipboard_monitor_changed.emit(checked)

    def _on_accessibility_toggle(self, checked: bool) -> None:
        self._persist("accessibility_mode", checked)
        self.accessibility_changed.emit(checked)

    def _on_browse_cookies(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("select_cookies_file"), "", "Cookies (*.txt);;All Files (*)"
        )
        if not path:
            return

        valid, warn_msg = check_cookies_valid(path)
        if not valid:
            InfoBar.warning(
                title=t("cookies_file"),
                content=warn_msg,
                orient=Qt.Orientation.Horizontal,
                position=InfoBarPosition.TOP,
                duration=6000,
                parent=self,
            )
            return

        internal_path = get_app_cookies_path()
        try:
            merge_cookies_file(path, internal_path)
        except OSError:
            InfoBar.error(
                title=t("cookies_store_failed_title"),
                content=t("cookies_store_failed_msg"),
                orient=Qt.Orientation.Horizontal,
                position=InfoBarPosition.TOP,
                duration=6000,
                parent=self,
            )
            return
        self._persist("cookies_file", str(internal_path))
        self._cookies_card.setContent(t("cookies_file_configured"))
        InfoBar.success(
            title=t("cookies_updated_title"),
            content=t("cookies_updated_msg"),
            orient=Qt.Orientation.Horizontal,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _on_clear_cookies(self) -> None:
        if not confirm(
            self,
            t("clear_cookies_title"),
            t("clear_cookies_confirm"),
            accept_text=t("clear_cookies_confirm_yes"),
            cancel_text=t("clear_cookies_confirm_no"),
            danger=True,
        ):
            return

        result = delete_stored_auth_data()
        if not result.success:
            InfoBar.error(
                title=t("clear_cookies_failed_title"),
                content=t("clear_cookies_failed_msg"),
                orient=Qt.Orientation.Horizontal,
                position=InfoBarPosition.TOP,
                duration=6000,
                parent=self,
            )
            return

        self._cfg.cookies_file = ""
        self._cfg.cookies_browser = ""
        self._cfg.save()
        self.settings_saved.emit()
        self._cookies_card.setContent(t("cookies_file_unset"))
        self._browser_card.setValue("")
        InfoBar.success(
            title=t("clear_cookies_success_title"),
            content=t("clear_cookies_success_msg"),
            orient=Qt.Orientation.Horizontal,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_youtube_fast_mode_toggle(self, checked: bool) -> None:
        # youtube_reliability_mode is a typed "conservative"/"fast" string
        # property (not a plain dict key), so it goes through its own
        # setter rather than the generic _persist() helper.
        self._cfg.youtube_reliability_mode = "fast" if checked else "conservative"
        self._cfg.save()
        self.settings_saved.emit()

    def _on_run_youtube_doctor(self) -> None:
        # Fully offline — no network calls, no cookie values read/shown.
        report = run_youtube_doctor(
            cookies_file=self._cfg.cookies_file,
            cookies_browser=self._cfg.cookies_browser,
            youtube_reliability_mode=self._cfg.youtube_reliability_mode,
        )
        show_youtube_doctor_dialog(report, parent=self)

    # ── Manual update checks ───────────────────────────────────────────────────

    def _on_check_app_updates(self) -> None:
        self._start_update_check(
            check_app=True, check_components=False,
            card=self._check_app_updates_card,
        )

    def _on_check_component_updates(self) -> None:
        self._start_update_check(
            check_app=False, check_components=True,
            card=self._check_component_updates_card,
        )

    def _start_update_check(self, *, check_app: bool, check_components: bool, card) -> None:
        card.button.setEnabled(False)
        worker = UpdateWorker(
            check_app=check_app,
            check_components=check_components,
            parent=self,
        )
        worker.results_ready.connect(
            lambda results, c=card: self._on_manual_check_done(results, c)
        )
        # Keep a reference so the thread isn't garbage-collected mid-run.
        self._update_check_worker = worker
        worker.start()

    def _on_manual_check_done(self, results: UpdateCheckResults, card) -> None:
        card.button.setEnabled(True)

        app_release = None
        if results.app is not None:
            if results.app.status == "error":
                self._show_update_infobar(
                    "warning", t("update_check_failed_title"), t("update_check_failed_msg"),
                )
                return
            if results.app.update_available:
                app_release = results.app.release

        component_updates = []
        if results.components is not None:
            report = results.components
            component_updates = report.updates
            if not component_updates and not report.all_checks_ok:
                self._show_update_infobar(
                    "warning", t("update_check_failed_title"), t("update_check_failed_msg"),
                )
                return

        if app_release is None and not component_updates:
            # Everything the user asked about is current.
            if results.app is not None:
                msg = t("app_up_to_date_msg").format(version=CURRENT_VERSION)
            else:
                versions = ",  ".join(
                    f"{c.display_name} {c.installed_version}"
                    for c in results.components.components if c.check_ok
                )
                msg = t("components_up_to_date_msg").format(versions=versions)
            self._show_update_infobar("success", t("up_to_date_title"), msg)
            return

        # A manual check is an explicit request — show the prompt even for
        # versions the user previously snoozed or skipped. Whatever they
        # choose now is recorded again the same way.
        UpdatePromptDialog(
            UpdateStateStore(), app_release, component_updates, parent=self.window(),
        ).exec()

    def _show_update_infobar(self, kind: str, title: str, content: str) -> None:
        maker = InfoBar.success if kind == "success" else InfoBar.warning
        maker(
            title=title,
            content=content,
            orient=Qt.Orientation.Horizontal,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _on_language_change(self, lang_code: str) -> None:
        self._persist("language", lang_code)
        QTimer.singleShot(0, self._adjust_layouts)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _persist(self, key: str, value) -> None:
        self._cfg.set(key, value)
        self._cfg.save()
        self.settings_saved.emit()


# ──────────────────────────────────────────────────────────────────────────────
# _AccentPickerCard
# ──────────────────────────────────────────────────────────────────────────────

class _AccentPickerCard(QFrame):
    """A row of coloured circle swatches for picking the accent color."""

    from PySide6.QtCore import Signal as _S
    accent_changed = _S(str)   # emits hex string

    def __init__(self, current_accent: str, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._current = current_accent
        self._build()

    def _build(self) -> None:
        self.setFixedHeight(64)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(0)
        self.hBoxLayout = row

        self._title_lbl = QLabel(t("accent_color"))
        self._title_lbl.setObjectName("titleLabel")
        self.titleLabel = self._title_lbl

        self._swatch_box = QWidget(self)
        swatches = QHBoxLayout(self._swatch_box)
        swatches.setContentsMargins(0, 0, 0, 0)
        swatches.setSpacing(10)
        swatches.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self._buttons: list[_SwatchButton] = []
        for name, hex_color in ACCENT_PALETTE.items():
            btn = _SwatchButton(name, hex_color, selected=(hex_color == self._current))
            btn.clicked.connect(lambda _checked, h=hex_color: self._on_swatch(h))
            swatches.addWidget(btn)
            self._buttons.append(btn)

        self.apply_direction(True)

    def apply_direction(self, rtl: bool) -> None:
        row = self.hBoxLayout
        while row.count():
            row.takeAt(0)

        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._title_lbl.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight
        )
        self._title_lbl.setAlignment(
            (Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
            | Qt.AlignmentFlag.AlignVCenter
        )
        row.setDirection(QBoxLayout.Direction.LeftToRight)
        row.setContentsMargins(16, 0, 16, 0)

        if rtl:
            row.addWidget(self._swatch_box, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            row.addStretch()
            row.addWidget(self._title_lbl, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            row.addWidget(self._title_lbl, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            row.addStretch()
            row.addWidget(self._swatch_box, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _on_swatch(self, hex_color: str) -> None:
        self._current = hex_color
        # Update selected state on all swatches
        for btn in self._buttons:
            btn.set_selected(btn.hex_color == hex_color)
        self.accent_changed.emit(hex_color)


class _SwatchButton(QPushButton):
    """A circular colour swatch button."""

    def __init__(self, name: str, hex_color: str, selected: bool = False) -> None:
        super().__init__()
        self.hex_color = hex_color
        self._selected = selected
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(name)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")
        self.set_selected(selected)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect().adjusted(4, 4, -4, -4)
        if self.underMouse():
            rect = rect.adjusted(-1, -1, 1, 1)

        painter.setBrush(QColor(self.hex_color))
        if self._selected:
            painter.setPen(QPen(QColor("#ffffff"), 3))
        elif self.hasFocus():
            painter.setPen(QPen(QColor("#8b7cf6"), 2))
        elif self.underMouse():
            painter.setPen(QPen(QColor("#ffffff"), 2))
        else:
            painter.setPen(QPen(QColor(0, 0, 0, 51), 1))
        painter.drawEllipse(rect)


# ──────────────────────────────────────────────────────────────────────────────
# _SpinnerSettingCard
# ──────────────────────────────────────────────────────────────────────────────

class _SpinnerSettingCard(QFrame):
    from PySide6.QtCore import Signal as _S
    value_changed = _S(int)

    def __init__(
        self, icon, title: str, content: str,
        value: int, min_val: int, max_val: int,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._value   = value
        self._min_val = min_val
        self._max_val = max_val
        self._build(icon, title, content)

    def _build(self, icon, title: str, content: str) -> None:
        from qfluentwidgets import IconWidget

        self.setFixedHeight(90)
        row = QHBoxLayout(self)
        row.setSpacing(0)
        row.setContentsMargins(16, 0, 0, 0)

        self.hBoxLayout = row

        self.iconLabel = IconWidget(icon, self)
        self.iconLabel.setFixedSize(16, 16)
        row.addWidget(self.iconLabel)
        row.addSpacing(16)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.vBoxLayout = text_col

        self.titleLabel = QLabel(title)
        self.titleLabel.setObjectName("titleLabel")
        self.contentLabel = QLabel(content)
        self.contentLabel.setObjectName("contentLabel")
        self.contentLabel.setWordWrap(True)
        self.titleLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.contentLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.titleLabel.setMinimumWidth(0)
        self.contentLabel.setMinimumWidth(0)
        text_col.addWidget(self.titleLabel)
        text_col.addWidget(self.contentLabel)
        row.addLayout(text_col, stretch=1)

        row.addSpacing(16)

        self._stepper = QFrame(self)
        self._stepper.setObjectName("settingsNumberStepper")
        self._stepper.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._stepper.setFixedWidth(156)
        self._stepper.setFixedHeight(38)
        self._stepper.setStyleSheet("""
            QFrame#settingsNumberStepper {
                background: transparent;
                border: 1px solid #d9d3f4;
                border-radius: 7px;
            }
            QFrame#settingsNumberStepper QToolButton {
                background: transparent;
                border: none;
                padding: 0;
            }
            QFrame#settingsNumberStepper QToolButton:hover {
                background: rgba(139, 124, 246, 0.10);
                border-radius: 5px;
            }
            QFrame#settingsNumberStepper QSpinBox {
                background: transparent;
                border: none;
                padding: 0 6px;
                min-height: 30px;
            }
        """)

        stepper_row = QHBoxLayout(self._stepper)
        stepper_row.setContentsMargins(5, 3, 5, 3)
        stepper_row.setSpacing(2)

        self._dec_btn = QToolButton(self._stepper)
        self._dec_btn.setArrowType(Qt.ArrowType.DownArrow)
        self._dec_btn.setFixedSize(30, 30)
        self._dec_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dec_btn.setAutoRepeat(True)
        self._dec_btn.clicked.connect(lambda: self._spin_box.stepDown())

        self._spin_box = QSpinBox(self._stepper)
        self._spin_box.setRange(self._min_val, self._max_val)
        self._spin_box.setValue(self._value)
        self._spin_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self._spin_box.setKeyboardTracking(False)
        self._spin_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin_box.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._spin_box.lineEdit().setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._spin_box.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._spin_box.valueChanged.connect(self._on_spin_changed)

        self._inc_btn = QToolButton(self._stepper)
        self._inc_btn.setArrowType(Qt.ArrowType.UpArrow)
        self._inc_btn.setFixedSize(30, 30)
        self._inc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._inc_btn.setAutoRepeat(True)
        self._inc_btn.clicked.connect(lambda: self._spin_box.stepUp())

        stepper_row.addWidget(self._dec_btn)
        stepper_row.addWidget(self._spin_box, 1)
        stepper_row.addWidget(self._inc_btn)
        row.addWidget(self._stepper)

        row.addSpacing(16)

        self._title_lbl = self.titleLabel
        self._sub_lbl = self.contentLabel

    def _on_spin_changed(self, value: int) -> None:
        if value != self._value:
            self._value = value
            self.value_changed.emit(value)

    def setValue(self, value: int) -> None:
        self._value = value
        self._spin_box.setValue(value)


# ──────────────────────────────────────────────────────────────────────────────
# _TextSettingCard
# ──────────────────────────────────────────────────────────────────────────────

class _TextSettingCard(QFrame):
    from PySide6.QtCore import Signal as _S
    value_changed = _S(str)

    def __init__(
        self, icon, title: str, content: str, value: str,
        secret: bool = False,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._value = value
        self._secret = secret
        self._build(icon, title, content)

    def _build(self, icon, title: str, content: str) -> None:
        from PySide6.QtWidgets import QLineEdit
        from qfluentwidgets import IconWidget, LineEdit

        self.setFixedHeight(90)
        row = QHBoxLayout(self)
        row.setSpacing(0)
        row.setContentsMargins(16, 0, 0, 0)

        self.hBoxLayout = row

        self.iconLabel = IconWidget(icon, self)
        self.iconLabel.setFixedSize(16, 16)
        row.addWidget(self.iconLabel)
        row.addSpacing(16)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.vBoxLayout = text_col

        self.titleLabel = QLabel(title)
        self.titleLabel.setObjectName("titleLabel")
        self.contentLabel = QLabel(content)
        self.contentLabel.setObjectName("contentLabel")
        self.contentLabel.setWordWrap(True)
        self.titleLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.contentLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.titleLabel.setMinimumWidth(0)
        self.contentLabel.setMinimumWidth(0)
        text_col.addWidget(self.titleLabel)
        text_col.addWidget(self.contentLabel)
        row.addLayout(text_col, stretch=1)

        row.addSpacing(16)

        self._edit = LineEdit(self)
        self._edit.setText(self._value)
        if self._secret:
            self._edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit.setMinimumWidth(300)
        self._edit.setMaximumWidth(420)
        self._edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._edit.setMinimumHeight(36)
        self._edit.setTextMargins(8, 0, 8, 0)
        # All three _TextSettingCard instances hold technical values (proxy
        # URL, API token, server URL) that must read L→R even in Hebrew.
        force_ltr_input(self._edit)
        self._edit.editingFinished.connect(self._on_editing_finished)
        row.addWidget(self._edit)

        row.addSpacing(16)

        self._title_lbl = self.titleLabel
        self._sub_lbl = self.contentLabel

    def _on_editing_finished(self) -> None:
        v = self._edit.text().strip()
        if v != self._value:
            self._value = v
            self.value_changed.emit(v)

    def setText(self, value: str) -> None:
        self._value = value
        self._edit.setText(value)


# ──────────────────────────────────────────────────────────────────────────────
# _LanguageSettingCard  (reused for any combo-selection cards)
# ──────────────────────────────────────────────────────────────────────────────

class _LanguageSettingCard(QFrame):
    from PySide6.QtCore import Signal as _S
    value_changed = _S(str)

    def __init__(
        self, icon, title: str, content: str,
        value: str, options: tuple,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._value   = value
        self._options = list(options)
        self._build(icon, title, content)

    def _build(self, icon, title: str, content: str) -> None:
        from PySide6.QtWidgets import QComboBox
        from qfluentwidgets import IconWidget

        self.setFixedHeight(90)
        row = QHBoxLayout(self)
        row.setSpacing(0)
        row.setContentsMargins(16, 0, 0, 0)

        self.hBoxLayout = row

        self.iconLabel = IconWidget(icon, self)
        self.iconLabel.setFixedSize(16, 16)
        row.addWidget(self.iconLabel)
        row.addSpacing(16)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.vBoxLayout = text_col

        self.titleLabel = QLabel(title)
        self.titleLabel.setObjectName("titleLabel")
        self.contentLabel = QLabel(content)
        self.contentLabel.setObjectName("contentLabel")
        self.contentLabel.setWordWrap(True)
        self.titleLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.contentLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.titleLabel.setMinimumWidth(0)
        self.contentLabel.setMinimumWidth(0)
        text_col.addWidget(self.titleLabel)
        text_col.addWidget(self.contentLabel)
        row.addLayout(text_col, stretch=1)

        row.addSpacing(16)

        self._combo = QComboBox(self)
        for code, label in self._options:
            self._combo.addItem(label, userData=code)
        self.setValue(self._value)
        self._combo.currentIndexChanged.connect(self._on_index_changed)
        row.addWidget(self._combo)

        row.addSpacing(16)

        self._title_lbl = self.titleLabel
        self._sub_lbl = self.contentLabel

    def _on_index_changed(self, index: int) -> None:
        code = self._combo.itemData(index)
        if code is None:
            return
        self._value = code
        self.value_changed.emit(code)

    def setValue(self, value: str) -> None:
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == value:
                self._combo.setCurrentIndex(i)
                return
        if self._combo.count() > 0:
            self._combo.setCurrentIndex(0)
