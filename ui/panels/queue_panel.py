"""
ui/panels/queue_panel.py  –  Smart drag-and-drop download queue
================================================================
The main panel shown on the Queue navigation tab.  Responsibilities:
  - Add / remove TrackCards and keep a canonical ordered list.
  - Accept drag-and-drop reordering from TrackCard drag sources.
  - "Select All / Deselect All" header checkbox.
  - Expose get_selected_cards() for DownloadWorker job building.
  - Show a friendly empty state before any tracks are loaded.
  - Draw a live drop-indicator line between cards during a drag.

Signals emitted upward
----------------------
selection_changed(int)    Number of currently selected cards changed.
card_removed(int)         A card's remove button was clicked; payload is queue_index.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    Qt, QPoint, QSize, Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, PushButton,
    DropDownPushButton, RoundMenu, Action, ToolButton
)

from ui.components.empty_state_icon import EmptyStateIcon
from ui.components.track_card import TrackCard
from ui.i18n import t
from ui.theme_manager import get_colors


# ──────────────────────────────────────────────────────────────────────────────
# Drop-indicator overlay
# ──────────────────────────────────────────────────────────────────────────────

class _DropIndicator(QWidget):
    """
    A 2-px horizontal accent line drawn between cards to show the drop target.
    Parented to the scroll-content widget and positioned manually.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(2)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(QPen(QColor(get_colors().accent), 2))
        painter.drawLine(0, 0, self.width(), 0)

    def show_at_y(self, y: int) -> None:
        self.setGeometry(8, y - 1, self.parent().width() - 16, 2)
        self.raise_()
        self.show()


# ──────────────────────────────────────────────────────────────────────────────
# Scroll content widget  (accepts drops)
# ──────────────────────────────────────────────────────────────────────────────

class _DropArea(QWidget):
    """
    The inner widget of the scroll area.  Overrides drag events so that
    TrackCards can be reordered by dragging.
    """

    reorder_requested = Signal(int, int)   # (from_queue_index, to_queue_index)

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._indicator = _DropIndicator(self)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 12)
        self._layout.setSpacing(4)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

    @property
    def cards_layout(self) -> QVBoxLayout:
        return self._layout

    # ── Drag-and-drop target ──────────────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasText():
            event.acceptProposedAction()
            insert_y = self._insertion_y(event.position().toPoint())
            self._indicator.show_at_y(insert_y)

    def dragLeaveEvent(self, event) -> None:
        self._indicator.hide()

    def dropEvent(self, event) -> None:
        self._indicator.hide()
        if not event.mimeData().hasText():
            return

        try:
            from_index = int(event.mimeData().text())
        except ValueError:
            return

        drop_pos    = event.position().toPoint()
        to_index    = self._card_index_at(drop_pos)

        if to_index != from_index:
            self.reorder_requested.emit(from_index, to_index)

        event.acceptProposedAction()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _insertion_y(self, pos: QPoint) -> int:
        """Return the Y coordinate of the nearest card gap to the cursor."""
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                mid_y  = widget.y() + widget.height() // 2
                if pos.y() < mid_y:
                    return widget.y()
        # Below all cards
        last = self._layout.itemAt(self._layout.count() - 1)
        if last and last.widget():
            w = last.widget()
            return w.y() + w.height()
        return pos.y()

    def _card_index_at(self, pos: QPoint) -> int:
        """Return the logical queue_index of the card nearest the drop position."""
        best_index    = 0
        best_distance = float("inf")
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), TrackCard):
                card    = item.widget()
                card_cy = card.y() + card.height() // 2
                dist    = abs(pos.y() - card_cy)
                if dist < best_distance:
                    best_distance = dist
                    best_index    = card.queue_index
        return best_index


def _make_pause_icon(color: str, size: int = 24) -> QIcon:
    """Solid two-bar pause glyph (FluentIcon.PAUSE renders as an outline)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    bar_w = size * 0.22
    bar_h = size * 0.72
    gap = size * 0.14
    top = (size - bar_h) / 2
    left_x = size / 2 - gap / 2 - bar_w
    right_x = size / 2 + gap / 2
    radius = bar_w * 0.3
    painter.drawRoundedRect(int(left_x), int(top), int(bar_w), int(bar_h), radius, radius)
    painter.drawRoundedRect(int(right_x), int(top), int(bar_w), int(bar_h), radius, radius)
    painter.end()
    return QIcon(pm)


def _make_play_icon(color: str, size: int = 24) -> QIcon:
    """Solid play-triangle glyph (FluentIcon.PLAY renders as an outline)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    w = size * 0.68
    h = size * 0.76
    x0 = (size - w) / 2 + w * 0.08
    y0 = (size - h) / 2
    path = QPainterPath()
    path.moveTo(x0, y0)
    path.lineTo(x0, y0 + h)
    path.lineTo(x0 + w, y0 + h / 2)
    path.closeSubpath()
    painter.drawPath(path)
    painter.end()
    return QIcon(pm)


class _HoverIconButton(ToolButton):
    """ToolButton that reports hover state so its icon color can follow it."""

    hover_changed = Signal(bool)

    def enterEvent(self, e) -> None:
        super().enterEvent(e)
        self.hover_changed.emit(True)

    def leaveEvent(self, e) -> None:
        super().leaveEvent(e)
        self.hover_changed.emit(False)


# ──────────────────────────────────────────────────────────────────────────────
# QueuePanel
# ──────────────────────────────────────────────────────────────────────────────

class QueuePanel(QWidget):
    """
    The download queue panel.

    Parameters
    ----------
    parent : Optional Qt parent widget.
    """

    selection_changed = Signal(int)   # count of selected cards
    card_removed      = Signal(int)   # queue_index of removed card
    pause_resume_triggered = Signal(bool) # True=pause, False=resume

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._cards: list[TrackCard] = []   # ordered list of all cards
        self._selection_anchor: Optional[int] = None
        self._selection_active: Optional[int] = None
        self._build()
        from ui.theme_manager import ThemeManager
        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    # ── Public API ─────────────────────────────────────────────────────────────

    def add_card(
        self,
        index:         int,
        title:         str,
        artist:        str         = "",
        duration:      str         = "",
        platform:      str         = "youtube",
        track_url:     str         = "",
        album:         str         = "",
        parent_artist: str         = "",
        release_type:  str         = "",
        album_index:   int         = 0,
        thumbnail_url: str         = "",
        category:      str         = "",
        total_tracks:  int         = 0,
        duration_sec:  Optional[int] = None,
        spotify_id:    str         = "",
        spotify_key_kind: str      = "spotify_id",
        match_status:  str         = "matched",
        resolution_error: str      = "",
    ) -> TrackCard:
        """
        Create and append a new TrackCard.  Hides the empty state on first add.
        Returns the card so the caller can keep a card_key → card mapping.
        """
        if not self._cards:
            self._empty_widget.setVisible(False)

        card = TrackCard(
            queue_index=index,
            title=title,
            artist=artist,
            duration=duration,
            platform=platform,
            track_url=track_url,
            album=album,
            parent_artist=parent_artist,
            release_type=release_type,
            album_index=album_index,
            thumbnail_url=thumbnail_url,
            category=category,
            total_tracks=total_tracks,
            duration_sec=duration_sec,
            spotify_id=spotify_id,
            spotify_key_kind=spotify_key_kind,
            match_status=match_status,
            resolution_error=resolution_error,
            parent=self._drop_area,
        )
        card.remove_requested.connect(self._on_card_remove)
        card.selection_changed.connect(lambda: self._on_card_selected(card))
        card.shift_selection_triggered.connect(self._on_card_shift_selected)
        card.status_changed.connect(lambda _: self._update_stats())

        self._drop_area.cards_layout.addWidget(card)
        self._cards.append(card)
        self._update_header()
        return card

    def clear(self) -> None:
        """Remove all cards and show the empty state."""
        for card in self._cards:
            card.deleteLater()
        self._cards.clear()
        self._selection_anchor = None
        self._selection_active = None
        self._empty_widget.setVisible(True)
        self._update_header()

    def get_all_cards(self) -> list[TrackCard]:
        return list(self._cards)

    def get_selected_cards(self) -> list[TrackCard]:
        return [c for c in self._cards if c.is_selected() and c.is_downloadable()]

    def card_by_index(self, queue_index: int) -> Optional[TrackCard]:
        for c in self._cards:
            if c.queue_index == queue_index:
                return c
        return None

    def set_all_selected(self, checked: bool) -> None:
        for c in self._cards:
            c.set_selected(checked)
        self._on_selection_change()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setObjectName("queuePanel")
        # QWidget subclasses ignore background/border QSS unless styled-background
        # is enabled — without this the queue "card" frame never renders.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────────
        self._header_frame = QFrame()
        self._header_frame.setObjectName("panelHeaderFrame")
        self._header_frame.setFixedHeight(46)
        h_row = QHBoxLayout(self._header_frame)
        h_row.setContentsMargins(14, 8, 14, 8)
        h_row.setSpacing(8)

        self._all_chk = QCheckBox(t("select_deselect_all"))
        self._all_chk.setObjectName("queueAllCheckBox")
        self._all_chk.setChecked(True)
        self._all_chk.stateChanged.connect(
            lambda s: self.set_all_selected(bool(s))
        )
        h_row.addWidget(self._all_chk)

        self._queue_label_lbl = CaptionLabel(t("queue_label"))
        self._queue_label_lbl.setObjectName("queueLabel")
        h_row.addWidget(self._queue_label_lbl)

        self._count_lbl = CaptionLabel(t("no_tracks_loaded"))
        self._count_lbl.setObjectName("queueCountBadge")
        self._count_lbl.setFixedHeight(20)
        h_row.addWidget(self._count_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

        h_row.addStretch()

        self._stats_lbl = CaptionLabel("")
        self._stats_lbl.setObjectName("queueStatsLabel")
        self._stats_lbl.setVisible(False)
        h_row.addWidget(self._stats_lbl)

        # ── Global Pause/Resume ───────────────────────────────────────────────
        self._pause_resume_btn = _HoverIconButton()
        self._pause_resume_btn.setObjectName("queuePauseResumeBtn")
        self._pause_resume_btn.setFixedSize(26, 26)
        self._pause_resume_btn.setIconSize(QSize(12, 12))
        self._pause_resume_btn.setToolTip(t("pause_all"))
        self._is_paused_state = False
        self._pause_btn_hovered = False
        self._pause_resume_btn.clicked.connect(self._on_global_pause_resume_click)
        self._pause_resume_btn.hover_changed.connect(self._on_pause_btn_hover)
        h_row.addWidget(self._pause_resume_btn)

        # ── Cleanup Dropdown ──────────────────────────────────────────────────
        self._cleanup_btn = DropDownPushButton(t("clear_options"))
        self._cleanup_btn.setObjectName("queueCleanupBtn")
        self._cleanup_btn.setFixedHeight(26)

        menu = RoundMenu(parent=self)
        menu.addAction(Action(t("clear_all"), triggered=self.clear))
        menu.addAction(Action(t("clear_selected"), triggered=self.clear_selected))
        menu.addAction(Action(t("clear_completed"), triggered=self._clear_completed))
        self._cleanup_menu = menu
        self._cleanup_btn.clicked.connect(self._show_cleanup_menu)
        h_row.addWidget(self._cleanup_btn)

        root.addWidget(self._header_frame)

        # ── Scroll area ───────────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setObjectName("queueScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._drop_area = _DropArea()
        self._drop_area.setObjectName("queueDropArea")
        self._drop_area.reorder_requested.connect(self._on_reorder)

        # Empty state (inside drop area so it fills the space)
        self._empty_widget = self._build_empty_state()
        self._drop_area.cards_layout.addWidget(self._empty_widget)

        self._scroll.setWidget(self._drop_area)
        root.addWidget(self._scroll, stretch=1)

        # Apply initial theme styles
        self._apply_theme()

    def _show_cleanup_menu(self) -> None:
        """Show the cleanup menu without letting it spill outside the window."""
        self._cleanup_menu.view.setMinimumWidth(self._cleanup_btn.width())
        self._cleanup_menu.view.adjustSize()
        self._cleanup_menu.adjustSize()

        size = self._cleanup_menu.sizeHint()
        anchor_left = self._cleanup_btn.mapToGlobal(
            QPoint(0, self._cleanup_btn.height())
        )
        window_rect = self.window().frameGeometry()
        margin = 8

        x = max(
            window_rect.left() + margin,
            min(anchor_left.x(), window_rect.right() - size.width() - margin),
        )

        y = max(
            window_rect.top() + margin,
            min(anchor_left.y(), window_rect.bottom() - size.height() - margin),
        )

        self._cleanup_menu.exec(QPoint(x, y))

    def _apply_theme(self) -> None:
        c = get_colors()
        self.setStyleSheet(f"""
            QueuePanel {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 16px;
            }}
            QFrame#panelHeaderFrame {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {c.border};
                border-top-left-radius: 16px;
                border-top-right-radius: 16px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
            QScrollArea#queueScrollArea {{
                background: transparent;
                border: none;
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
            }}
            QWidget#queueDropArea {{
                background: transparent;
            }}
            QLabel#queueStatsLabel {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 10px;
            }}
            QLabel#queueLabel {{
                color: {c.text_primary};
                background: transparent;
                font-size: 12px;
                font-weight: 700;
            }}
            QLabel#queueCountBadge {{
                color: {c.text_secondary};
                background: {c.surface2};
                border-radius: 10px;
                font-size: 10px;
                padding: 0 9px;
            }}
            QCheckBox#queueAllCheckBox {{
                color: {c.text_secondary};
                background: transparent;
                spacing: 7px;
                font-size: 12px;
            }}
            QCheckBox#queueAllCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border: 1px solid {c.border};
                border-radius: 4px;
                background: {c.surface2};
            }}
            QCheckBox#queueAllCheckBox::indicator:checked {{
                background: {c.accent};
                border-color: {c.accent};
            }}
            ToolButton#queuePauseResumeBtn {{
                background: {c.surface2};
                border: 1px solid {c.border};
                border-radius: 7px;
                color: {c.accent};
            }}
            ToolButton#queuePauseResumeBtn:hover {{
                border-color: {c.accent};
            }}
            DropDownPushButton#queueCleanupBtn {{
                background: {c.surface2};
                border: 1px solid {c.border};
                border-radius: 7px;
                color: {c.text_secondary};
                font-size: 11px;
                padding: 0 24px 0 10px;
            }}
            DropDownPushButton#queueCleanupBtn:hover {{
                border-color: {c.accent};
                color: {c.accent};
            }}
            QWidget#queueEmptyWidget {{
                background: transparent;
            }}
            QLabel#queueEmptyTitle {{
                color: {c.text_primary};
                background: transparent;
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#queueEmptyHint {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 11px;
            }}
        """)
        self._update_pause_resume_icon()

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        w.setObjectName("queueEmptyWidget")
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(12)
        v.addWidget(EmptyStateIcon("bars", w), alignment=Qt.AlignmentFlag.AlignCenter)

        title = BodyLabel(t("downloads_empty_title"))
        title.setObjectName("queueEmptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        hint = BodyLabel(t("downloads_empty_subtitle"))
        hint.setObjectName("queueEmptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(hint)

        return w

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_card_remove(self, queue_index: int) -> None:
        card = self.card_by_index(queue_index)
        if card:
            self._cards.remove(card)
            card.deleteLater()
            if not self._cards:
                self._empty_widget.setVisible(True)
            self._update_header()
            self.card_removed.emit(queue_index)
            self._on_selection_change()

    def _on_selection_change(self) -> None:
        self._update_header()
        self.selection_changed.emit(len(self.get_selected_cards()))

    def _on_card_selected(self, card: TrackCard) -> None:
        self._selection_anchor = card.queue_index
        self._selection_active = card.queue_index
        self._on_selection_change()

    def _on_card_shift_selected(self, index: int, checked: bool) -> None:
        if self._selection_anchor is not None:
            anchor_idx = self._selection_anchor
            old_active_idx = self._selection_active if self._selection_active is not None else anchor_idx
            new_active_idx = index
            
            anchor_pos = -1
            old_active_pos = -1
            new_active_pos = -1
            for pos, c in enumerate(self._cards):
                if c.queue_index == anchor_idx:
                    anchor_pos = pos
                if c.queue_index == old_active_idx:
                    old_active_pos = pos
                if c.queue_index == new_active_idx:
                    new_active_pos = pos
                    
            if anchor_pos != -1 and new_active_pos != -1:
                new_min = min(anchor_pos, new_active_pos)
                new_max = max(anchor_pos, new_active_pos)
                new_range = set(range(new_min, new_max + 1))
                
                if old_active_pos != -1:
                    old_min = min(anchor_pos, old_active_pos)
                    old_max = max(anchor_pos, old_active_pos)
                    old_range = set(range(old_min, old_max + 1))
                else:
                    old_range = set()
                    
                for pos in new_range:
                    c = self._cards[pos]
                    if c.get_status() == "queued":
                        c._check.blockSignals(True)
                        c.set_selected(checked)
                        c._check.blockSignals(False)
                        
                for pos in (old_range - new_range):
                    c = self._cards[pos]
                    if c.get_status() == "queued":
                        c._check.blockSignals(True)
                        c.set_selected(not checked)
                        c._check.blockSignals(False)
                        
            self._selection_active = new_active_idx
        else:
            self._selection_anchor = index
            self._selection_active = index
        self._on_selection_change()

    def ensure_checkbox_visible(self, chk: QCheckBox) -> None:
        self._scroll.ensureWidgetVisible(chk, 0, 15)

    def get_next_or_prev_checkbox(self, current_chk: QCheckBox, go_down: bool) -> Optional[QCheckBox]:
        current_pos = -1
        for pos, c in enumerate(self._cards):
            if c._check is current_chk:
                current_pos = pos
                break
        if current_pos == -1:
            return None
        step = 1 if go_down else -1
        target_pos = current_pos + step
        while 0 <= target_pos < len(self._cards):
            target_chk = self._cards[target_pos]._check
            if target_chk.isEnabled():
                return target_chk
            target_pos += step
        return None

    def _on_reorder(self, from_index: int, to_index: int) -> None:
        """
        Reorder _cards list and re-insert the dragged widget in the layout.
        """
        from_card = self.card_by_index(from_index)
        to_card   = self.card_by_index(to_index)
        if from_card is None or to_card is None or from_card is to_card:
            return

        layout = self._drop_area.cards_layout

        # Remove from layout and list
        layout.removeWidget(from_card)
        self._cards.remove(from_card)

        # Find new position in layout
        to_layout_idx = layout.indexOf(to_card)
        if to_layout_idx < 0:
            layout.addWidget(from_card)
            self._cards.append(from_card)
        else:
            layout.insertWidget(to_layout_idx, from_card)
            to_list_idx = self._cards.index(to_card)
            self._cards.insert(to_list_idx, from_card)

    def _clear_completed(self) -> None:
        """Remove all cards whose status is 'done'."""
        to_remove = [c for c in self._cards if c.get_status() == "done"]
        for card in to_remove:
            self._cards.remove(card)
            card.deleteLater()
        if not self._cards:
            self._empty_widget.setVisible(True)
        self._update_header()
        self._on_selection_change()

    def clear_selected(self) -> None:
        """Remove all checked cards."""
        to_remove = [c for c in self._cards if c.is_selected()]
        for card in to_remove:
            self._cards.remove(card)
            card.deleteLater()
        if not self._cards:
            self._empty_widget.setVisible(True)
        self._update_header()
        self._on_selection_change()

    def set_pause_resume_state(self, is_paused: bool) -> None:
        """Update the global button icon and tooltip based on app state."""
        self._is_paused_state = is_paused
        self._pause_resume_btn.setToolTip(t("resume_all") if is_paused else t("pause_all"))
        self._update_pause_resume_icon()

    def _update_pause_resume_icon(self) -> None:
        c = get_colors()
        color = c.accent if self._pause_btn_hovered else c.text_secondary
        icon = _make_play_icon(color) if self._is_paused_state else _make_pause_icon(color)
        self._pause_resume_btn.setIcon(icon)

    def _on_pause_btn_hover(self, hovering: bool) -> None:
        self._pause_btn_hovered = hovering
        self._update_pause_resume_icon()

    def _on_global_pause_resume_click(self) -> None:
        # Toggle then emit
        new_state = not self._is_paused_state
        self.pause_resume_triggered.emit(new_state)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_header(self) -> None:
        n = len(self._cards)
        if n == 0:
            self._count_lbl.setText(t("no_tracks_loaded"))
            self._stats_lbl.setVisible(False)
        else:
            sel = len(self.get_selected_cards())
            self._count_lbl.setText(t("sel_of_n", sel=sel, n=n))
            self._update_stats()

    def _update_stats(self) -> None:
        if not self._cards:
            self._stats_lbl.setVisible(False)
            return
        done = sum(1 for c in self._cards if c.get_status() == "done")
        total = len(self._cards)
        if done > 0 or any(c.get_status() == "downloading" for c in self._cards):
            self._stats_lbl.setText(t("queue_stats_done", done=done, total=total))
            self._stats_lbl.setVisible(True)
        else:
            self._stats_lbl.setVisible(False)
