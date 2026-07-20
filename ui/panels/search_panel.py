"""
ui/panels/search_panel.py  –  Universal search panel
=====================================================
Full-panel search interface with:
  - A SearchLineEdit at the top
  - A SegmentedWidget tab bar for platform selection (YouTube / Spotify)
  - An incrementally populated results list of SearchResultCards
    grouped into coloured section headers: Tracks / Albums / Playlists /
    Artists / Channels
  - A results-count label and a "Clear results" button
  - An empty-state illustration shown before the first search

Signals emitted upward
----------------------
add_to_queue_requested(SearchResult)
    Forwarded from SearchResultCard.add_to_queue → AppWindow.
drill_down_requested(SearchResult)
    Forwarded from SearchResultCard.browse_requested → AppWindow.
    Signals the user wants to drill into an Album / Playlist / Artist /
    Channel; AppWindow starts a FetchWorker for that result's URL.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel,
    QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel, CaptionLabel, IndeterminateProgressRing,
    DropDownPushButton, PushButton, RoundMenu, SearchLineEdit,
)

from config import AppConfig
from core.search_engine import ResultKind, SearchResult
from ui.components.empty_state_icon import EmptyStateIcon
from ui.components.search_result_card import SearchResultCard
from ui.i18n import t
from ui.theme_manager import get_colors

logger = logging.getLogger(__name__)

# Section order and display labels
_SECTION_ORDER: list[ResultKind] = [
    ResultKind.TRACK,
    ResultKind.ALBUM,
    ResultKind.PLAYLIST,
    ResultKind.ARTIST,
    ResultKind.CHANNEL,
]

# Section headers reuse the filter-tab translation keys. Resolved lazily
# through t() at build time (never at import time) so the active language
# is already applied when SearchPanel is constructed.
_SECTION_LABEL_KEYS: dict[ResultKind, str] = {
    ResultKind.TRACK:    "search_filter_tracks",
    ResultKind.ALBUM:    "search_filter_albums",
    ResultKind.PLAYLIST: "search_filter_playlists",
    ResultKind.ARTIST:   "search_filter_artists",
    ResultKind.CHANNEL:  "search_filter_channels",
}

# ──────────────────────────────────────────────────────────────────────────────
# SearchPanel
# ──────────────────────────────────────────────────────────────────────────────

class SearchPanel(QWidget):
    """
    Full-height panel for the Search navigation tab.

    Parameters
    ----------
    config : AppConfig – to remember the last selected search platform.
    parent : Optional Qt parent.
    """

    add_to_queue_requested = Signal(object)   # SearchResult
    drill_down_requested   = Signal(object)   # SearchResult
    search_requested       = Signal(str)      # query to search

    def __init__(self, config: AppConfig, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._config    = config
        self._cards:    list[SearchResultCard] = []
        self._searching = False
        self._current_platform = "youtube"

        # Per-section containers and card lists (populated in _build)
        self._section_widgets:  dict[ResultKind, QWidget]           = {}
        self._section_layouts:  dict[ResultKind, QVBoxLayout]       = {}
        self._section_cards:    dict[ResultKind, list[SearchResultCard]] = {
            k: [] for k in _SECTION_ORDER
        }
        self._current_filter: Optional[ResultKind] = None
        self._filter_buttons: dict[str, QPushButton] = {}
        self._filter_kinds: dict[str, Optional[ResultKind]] = {}

        self._build()
        self._restore_state()
        from ui.theme_manager import ThemeManager
        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_query(self) -> str:
        return self._search_box.text().strip()

    def get_platform(self) -> str:
        return self._current_platform

    def set_searching(self, searching: bool) -> None:
        """Show/hide the progress ring and lock the search field during a query."""
        self._searching = searching
        self._search_box.setEnabled(not searching)
        if searching:
            self._ring.setVisible(True)
            self._ring.start()
            self._results_lbl.setText(t("searching"))
        else:
            self._ring.stop()
            self._ring.setVisible(False)

    def add_result(self, result: SearchResult) -> SearchResultCard:
        """
        Add one SearchResultCard to the appropriate section.
        Called incrementally by AppWindow as SearchWorker emits result_ready.
        """
        logger.debug("[SearchPanel] Adding result to UI: %s (kind=%s)", result.title, result.kind)
        self._empty_widget.setVisible(False)

        kind = result.kind
        # Fallback: unknown kinds go into TRACK section
        if kind not in _SECTION_ORDER:
            kind = ResultKind.TRACK

        # Show the section header on the first card of that kind
        section_w = self._section_widgets.get(kind)
        if section_w and not section_w.isVisible():
            section_w.setVisible(True)

        section_layout = self._section_layouts.get(kind, self._section_layouts[ResultKind.TRACK])
        card = SearchResultCard(result, parent=self._results_container)
        card.add_to_queue.connect(self.add_to_queue_requested)
        card.browse_requested.connect(self.drill_down_requested)
        section_layout.addWidget(card)

        self._section_cards[kind].append(card)
        self._cards.append(card)
        
        # Ensure the section visibility respects the current filter
        self._apply_filter(self._current_filter)
        
        self._update_count()
        return card

    def clear_results(self) -> None:
        """Remove all result cards and show the empty state."""
        for card in self._cards:
            card.deleteLater()
        self._cards.clear()
        for kind in _SECTION_ORDER:
            self._section_cards[kind].clear()
            w = self._section_widgets.get(kind)
            if w:
                w.setVisible(False)
        self._results_lbl.setText("")
        self._empty_widget.setVisible(True)

    def set_result_count(self, count: int) -> None:
        if count == 0:
            self._results_lbl.setText(t("no_results"))
        else:
            self._results_lbl.setText(
                t("results_count", n=count, plural=("s" if count != 1 else ""))
            )

    def save_state(self) -> None:
        """Persist durable search preferences to config."""
        self._config.last_search_query    = ""
        self._config.last_search_platform = self.get_platform()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setObjectName("searchPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 0)
        root.setSpacing(12)

        # ── Top bar: search box + platform selector ───────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self._search_box = SearchLineEdit()
        self._search_box.setObjectName("searchBox")
        self._search_box.setPlaceholderText(t("search_placeholder"))
        self._search_box.setMinimumHeight(42)
        self._search_box.searchSignal.connect(self._on_search)
        self._search_box.returnPressed.connect(self._on_search_return)

        # Platform selector button with dropdown menu
        self._platform_btn = DropDownPushButton(t("platform_youtube"))
        self._platform_btn.setObjectName("searchPlatformBtn")
        self._platform_btn.setFixedSize(150, 42)
        self._platform_btn.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._platform_menu = RoundMenu(parent=self)

        youtube_action = Action(t("platform_youtube"), triggered=lambda: self._set_platform("youtube"))
        self._platform_menu.addAction(youtube_action)

        ytmusic_action = Action(t("platform_ytmusic"), triggered=lambda: self._set_platform("ytmusic"))
        self._platform_menu.addAction(ytmusic_action)

        spotify_action = Action(t("platform_spotify"), triggered=lambda: self._set_platform("spotify"))
        self._platform_menu.addAction(spotify_action)

        both_action = Action(t("platform_both"), triggered=lambda: self._set_platform("both"))
        self._platform_menu.addAction(both_action)

        self._platform_btn.setMenu(self._platform_menu)
        self._current_platform = "youtube"

        platform_wrap = QWidget(self)
        platform_wrap.setObjectName("searchPlatformWrap")
        platform_wrap_layout = QVBoxLayout(platform_wrap)
        platform_wrap_layout.setContentsMargins(0, 8, 0, 0)
        platform_wrap_layout.setSpacing(0)
        platform_wrap_layout.addWidget(self._platform_btn)
        top_row.addWidget(platform_wrap)
        top_row.addWidget(self._search_box, stretch=1)

        root.addLayout(top_row)

        # ── Filter bar ────────────────────────────────────────────────────────
        self.filter_nav = QWidget(self)
        self.filter_nav.setObjectName("searchFilterNav")
        self.filter_nav.setFixedHeight(34)
        self._filter_layout = QHBoxLayout(self.filter_nav)
        self._filter_layout.setContentsMargins(0, 0, 0, 0)
        self._filter_layout.setSpacing(28)
        self._add_filter_item("all", t("search_filter_all"), None)
        self._add_filter_item("tracks", t("search_filter_tracks"), ResultKind.TRACK)
        self._add_filter_item("albums", t("search_filter_albums"), ResultKind.ALBUM)
        self._add_filter_item("artists", t("search_filter_artists"), ResultKind.ARTIST)
        self._add_filter_item("playlists", t("search_filter_playlists"), ResultKind.PLAYLIST)
        self._add_filter_item("channels", t("search_filter_channels"), ResultKind.CHANNEL)
        self._filter_layout.addStretch()
        
        self._set_filter("all")
        root.addWidget(self.filter_nav)

        # ── Sub-bar: result count + ring + clear button ───────────────────────
        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)

        self._results_lbl = CaptionLabel("")
        self._results_lbl.setObjectName("searchResultsLabel")
        sub_row.addWidget(self._results_lbl)

        self._ring = IndeterminateProgressRing()
        self._ring.setFixedSize(20, 20)
        self._ring.setVisible(False)
        sub_row.addWidget(self._ring)

        sub_row.addStretch()

        self._clear_btn = PushButton(t("clear_results"))
        self._clear_btn.setObjectName("searchClearBtn")
        self._clear_btn.setFixedHeight(28)
        self._clear_btn.clicked.connect(self.clear_results)
        sub_row.addWidget(self._clear_btn)

        root.addLayout(sub_row)

        # ── Divider ───────────────────────────────────────────────────────────
        self._divider = QFrame()
        self._divider.setObjectName("searchDivider")
        self._divider.setFrameShape(QFrame.Shape.HLine)
        self._divider.setFixedHeight(1)
        root.addWidget(self._divider)

        # ── Scrollable results area ───────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._results_container = QWidget()
        outer_layout = QVBoxLayout(self._results_container)
        outer_layout.setContentsMargins(0, 4, 0, 16)
        outer_layout.setSpacing(0)
        outer_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Empty state
        self._empty_widget = self._build_empty_state()
        outer_layout.addWidget(self._empty_widget)

        # ── Build one collapsible section per ResultKind ──────────────────────
        for kind in _SECTION_ORDER:
            section_w, cards_layout = self._build_section(kind)
            section_w.setVisible(False)      # hidden until first result arrives
            outer_layout.addWidget(section_w)
            self._section_widgets[kind]  = section_w
            self._section_layouts[kind]  = cards_layout

        outer_layout.addStretch()
        self._scroll.setWidget(self._results_container)
        root.addWidget(self._scroll, stretch=1)

        self._apply_theme()

    def _apply_theme(self) -> None:
        c = get_colors()
        self.setStyleSheet(f"""
            SearchPanel {{
                background: {c.bg};
            }}
            SearchLineEdit#searchBox {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 12px;
                color: {c.text_primary};
                font-size: 13px;
                padding: 0 14px;
            }}
            SearchLineEdit#searchBox:focus {{
                border-color: {c.accent};
            }}
            DropDownPushButton#searchPlatformBtn {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 12px;
                color: {c.text_primary};
                font-size: 12px;
                font-weight: 600;
                padding: 0 28px 0 12px;
            }}
            DropDownPushButton#searchPlatformBtn:hover {{
                border-color: {c.accent};
                color: {c.accent};
            }}
            QWidget#searchFilterNav {{
                background: transparent;
                border: none;
            }}
            QPushButton#searchFilterButton {{
                background: transparent;
                border: none;
                border-bottom: 2px solid transparent;
                border-radius: 0;
                color: {c.text_secondary};
                font-size: 12px;
                font-weight: 600;
                padding: 0 8px;
            }}
            QPushButton#searchFilterButton:hover {{
                color: {c.accent};
                background: transparent;
            }}
            QPushButton#searchFilterButton[active="true"] {{
                color: {c.accent};
                border-bottom: 2px solid {c.accent};
            }}
            QLabel#searchResultsLabel {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 11px;
            }}
            PushButton#searchClearBtn {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 10px;
                color: {c.text_secondary};
                font-size: 11px;
                padding: 0 12px;
            }}
            PushButton#searchClearBtn:hover {{
                border-color: {c.accent};
                color: {c.accent};
            }}
            QFrame#searchDivider {{
                background: {c.border};
                border: none;
            }}
            QLabel#searchSectionHeader {{
                color: {c.text_secondary};
                font-size: 10px;
                font-weight: 700;
                padding-left: 10px;
                border-left: 3px solid {c.accent};
                background: transparent;
            }}
            QWidget#searchEmptyWidget {{
                background: transparent;
            }}
            QLabel#searchEmptyTitle {{
                color: {c.text_primary};
                background: transparent;
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#searchEmptySubtitle {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 11px;
            }}
        """)

    def _build_section(self, kind: ResultKind) -> tuple[QWidget, QVBoxLayout]:
        """Return (section_container, cards_layout) for one ResultKind."""
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 8, 0, 4)
        v.setSpacing(4)

        # Section header (.upper() is a no-op for Hebrew)
        header = QLabel(t(_SECTION_LABEL_KEYS[kind]).upper())
        header.setObjectName("searchSectionHeader")
        header.setFixedHeight(20)
        v.addWidget(header)

        # Cards go here
        cards_layout = QVBoxLayout()
        cards_layout.setSpacing(4)
        cards_layout.setContentsMargins(0, 4, 0, 0)
        v.addLayout(cards_layout)

        return container, cards_layout

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        w.setObjectName("searchEmptyWidget")
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(10)
        v.addWidget(EmptyStateIcon("search", w), alignment=Qt.AlignmentFlag.AlignCenter)

        title = BodyLabel(t("search_empty_title"))
        title.setObjectName("searchEmptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        subtitle = CaptionLabel(t("search_empty_subtitle"))
        subtitle.setObjectName("searchEmptySubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(subtitle)

        return w

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        self._config.last_search_query = ""
        platform = self._config.last_search_platform
        # Must match the allow-list in AppConfig.last_search_platform —
        # "ytmusic" was missing before, so users who last selected
        # YouTube Music silently reverted to YouTube on restart.
        if platform in ("youtube", "ytmusic", "spotify", "both"):
            self._set_platform(platform)

    def _update_count(self) -> None:
        n = len(self._cards)
        self._results_lbl.setText(
            t("results_count", n=n, plural=("s" if n != 1 else ""))
        )

    def _on_search(self, query: str) -> None:
        query = query.strip()
        logger.debug("[SearchPanel] Search via icon: %r (Platform: %s)", query, self._current_platform)
        if query and not self._searching:
            self.clear_results()
            self.save_state()
            self.search_requested.emit(query)

    def _set_platform(self, platform: str) -> None:
        self._current_platform = platform
        if platform == "youtube":
            self._platform_btn.setText(t("platform_youtube"))
        elif platform == "ytmusic":
            self._platform_btn.setText(t("platform_ytmusic"))
        elif platform == "spotify":
            self._platform_btn.setText(t("platform_spotify"))
        elif platform == "both":
            self._platform_btn.setText(t("platform_both"))

    def _on_search_return(self) -> None:
        query = self._search_box.text().strip()
        logger.debug("[SearchPanel] Search via Enter: %r (Platform: %s)", query, self._current_platform)
        if query and not self._searching:
            self.clear_results()
            self.save_state()
            self.search_requested.emit(query)

    def _add_filter_item(self, route: str, text: str, kind: Optional[ResultKind]) -> None:
        button = QPushButton(text)
        button.setObjectName("searchFilterButton")
        button.setProperty("active", False)
        button.setFixedHeight(32)
        button.clicked.connect(lambda checked=False, r=route: self._set_filter(r))
        self._filter_layout.addWidget(button)
        self._filter_buttons[route] = button
        self._filter_kinds[route] = kind

    def _set_filter(self, route: str) -> None:
        for key, button in self._filter_buttons.items():
            button.setProperty("active", key == route)
            button.style().unpolish(button)
            button.style().polish(button)
        self._apply_filter(self._filter_kinds.get(route))

    def _apply_filter(self, kind: Optional[ResultKind]) -> None:
        """Show only sections matching the filter. None means 'All'."""
        self._current_filter = kind
        
        for section_kind, widget in self._section_widgets.items():
            # A section should be visible if:
            # 1. It has cards
            # 2. (Current filter is None) OR (Current filter matches section kind)
            has_cards = len(self._section_cards[section_kind]) > 0
            matches_filter = (kind is None) or (kind == section_kind)
            
            widget.setVisible(has_cards and matches_filter)
