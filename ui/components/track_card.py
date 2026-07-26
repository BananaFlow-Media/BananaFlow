"""
ui/components/track_card.py  –  Draggable download queue entry  (v3)
=====================================================================
Changelog v3
------------
* Pause button (⏸) visible when status == "downloading".
  Emits pause_requested(queue_index).
* Resume button (▶) visible when status == "paused".
  Emits resume_requested(queue_index).
* Both buttons replace the single cancel-level control; the remove (×)
  button is always available on hover.
* Status dot gains a "paused" state (amber/warning colour).
* All existing public API is unchanged (set_status, set_progress, etc.).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QByteArray, QMimeData, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QImage, QPixmap, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, FluentIcon, ToolButton

from ui.direction import isolate_number
from ui.i18n import t
from ui.theme_manager import ThemeManager, get_colors
from utils.time_format import seconds_to_str

# Action-button icon colours (semantic, theme-independent). Pause follows the
# accent and is refreshed on theme change.
_REMOVE_COLOR = "#f87171"
_RESUME_COLOR = "#10b981"


# ── Design tokens ──────────────────────────────────────────────────────────────

_RADIUS      = 10
_THUMB_W     = 114
_THUMB_H     = 64

_PLATFORM_BADGE_LABELS: dict[str, str] = {
    "youtube":  "YT",
    "ytm":      "YTM",
    "ytmusic":  "YTM",
    "spotify":  "SP",
    "generic":  "URL",
    "hls":      "HLS",
    "dash":     "HLS",
}

_PLATFORM_COLORS: dict[str, tuple[str, str]] = {
    "youtube":  ("#cc2200", "#ffffff"),
    "ytmusic":  ("#cc2200", "#ffffff"),
    "ytm":      ("#cc2200", "#ffffff"),
    "spotify":  ("#1aa34a", "#ffffff"),
    "hls":      ("#0ea5e9", "#ffffff"),
    "dash":     ("#0ea5e9", "#ffffff"),
    "generic":  ("#6b65a0", "#ffffff"),
}


def _make_placeholder_pixmap(w: int = _THUMB_W, h: int = _THUMB_H) -> QPixmap:
    c = get_colors()
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(c.border))
    # Draw a simple play triangle
    from PySide6.QtGui import QPainter, QPen, QBrush, QPolygon
    from PySide6.QtCore import QPoint as _QP
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor(c.text_tertiary)))
    painter.setPen(Qt.PenStyle.NoPen)
    cx, cy = w // 2, h // 2
    s = min(w, h) // 5
    tri = QPolygon([_QP(cx - s, cy - s), _QP(cx + s, cy), _QP(cx - s, cy + s)])
    painter.drawPolygon(tri)
    painter.end()
    return QPixmap.fromImage(img)


class ShiftClickCheckBox(QCheckBox):
    shift_clicked = Signal(bool)  # new checked state

    def _get_queue_panel(self):
        curr = self.parent()
        while curr is not None:
            if curr.__class__.__name__ == "QueuePanel":
                return curr
            curr = curr.parent()
        return None

    def nextCheckState(self) -> None:
        if QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.blockSignals(True)
            super().nextCheckState()
            self.blockSignals(False)
            self.shift_clicked.emit(self.isChecked())
        else:
            super().nextCheckState()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key_Down, Qt.Key_Up):
            parent_panel = self._get_queue_panel()
            if parent_panel:
                next_chk = parent_panel.get_next_or_prev_checkbox(self, key == Qt.Key_Down)
                if next_chk:
                    next_chk.setFocus()
                    parent_panel.ensure_checkbox_visible(next_chk)
                    if event.modifiers() & Qt.ShiftModifier:
                        target_state = self.isChecked()
                        next_chk.blockSignals(True)
                        next_chk.setChecked(target_state)
                        next_chk.blockSignals(False)
                        next_chk.shift_clicked.emit(target_state)
                    event.accept()
                    return
        super().keyPressEvent(event)


# Stages that get a caption under the bar, and the string that names each.
# Anything not listed here (queued, done, error, cancelled, paused) shows no
# caption - its coloured dot already says everything there is to say.
_PHASE_CAPTIONS = {
    "matching":    "phase_matching",
    "waiting":     "phase_waiting",
    "starting":    "phase_starting",
    "downloading": "phase_downloading",
    "processing":  "phase_processing",
}


class TrackCard(QFrame):
    """
    One entry in the download queue panel.

    Parameters
    ----------
    title, artist, duration, platform, queue_index : track metadata.
    parent : optional Qt parent.
    """

    # ── Signals ───────────────────────────────────────────────────────────────
    remove_requested  = Signal(int)    # queue_index
    selection_changed = Signal()      # checkbox toggled
    pause_requested   = Signal(int)    # queue_index
    resume_requested  = Signal(int)    # queue_index
    reorder_requested = Signal(int, int)  # (from_index, to_index)
    status_changed    = Signal(str)   # new status string
    shift_selection_triggered = Signal(int, bool)  # queue_index, checked

    # ── Constructor ───────────────────────────────────────────────────────────

    def __init__(
        self,
        title:        str,
        artist:       str          = "",
        duration:     str          = "",
        platform:     str          = "youtube",
        queue_index:  int          = 0,
        track_url:    str          = "",
        album:        str          = "",
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
        parent:       QWidget      = None,
    ) -> None:
        super().__init__(parent)
        self.queue_index = queue_index
        self.track_url   = track_url
        self.title       = title
        self.artist      = artist
        self.album       = album
        self.parent_artist = parent_artist
        self.release_type  = release_type
        self.category      = category
        self.total_tracks  = total_tracks
        self.album_index   = album_index
        self.thumbnail_url = thumbnail_url
        self.duration       = duration
        self.duration_sec   = duration_sec
        # Deferred-match state (Spotify two-stage import). ``match_status`` is
        # "pending" until the YouTube URL is resolved at download time.
        self.spotify_id       = spotify_id
        self.spotify_key_kind = spotify_key_kind
        self.match_status     = match_status
        # Ensure platform is a string
        if hasattr(platform, "value"):
            plat_str = platform.value
        else:
            plat_str = str(platform).lower()
        self._platform = plat_str
        self._status   = "queued"
        # Caption state: which stage, its remaining time, and the live rate.
        self._phase: str = "queued"
        self._phase_remaining: Optional[float] = None
        self._speed_bps: Optional[float] = None
        self._drag_start_pos: Optional[QPoint] = None
        
        # Action buttons (pause/resume are hidden by default)
        self._pause_btn:  Optional[ToolButton] = None
        self._resume_btn: Optional[ToolButton] = None
        self._remove_btn: Optional[ToolButton] = None

        self._build(title, artist, duration, plat_str)
        self._apply_shadow()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(
        self,
        title:    str,
        artist:   str,
        duration: str,
        platform: str,
    ) -> None:
        self.setFixedHeight(90)
        self.setObjectName("trackCard")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(10)

        # Checkbox
        self._check = ShiftClickCheckBox()
        self._check.setChecked(True)
        self._check.setFixedSize(20, 20)
        self._check.stateChanged.connect(lambda: self.selection_changed.emit())
        self._check.shift_clicked.connect(
            lambda checked: self.shift_selection_triggered.emit(self.queue_index, checked)
        )
        outer.addWidget(self._check)

        # Thumbnail
        self._thumb_lbl = QLabel()
        self._thumb_lbl.setObjectName("trackCardThumb")
        self._thumb_lbl.setFixedSize(_THUMB_W, _THUMB_H)
        self._thumb_lbl.setScaledContents(False)
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setPixmap(_make_placeholder_pixmap())
        outer.addWidget(self._thumb_lbl)

        # Status dot
        self._dot = QLabel("●")
        self._dot.setObjectName("trackCardDot")
        self._dot.setFixedWidth(14)
        outer.addWidget(self._dot)

        # Text column
        text_col = QVBoxLayout()
        text_col.setSpacing(1)

        self._title_lbl = BodyLabel(title[:80])
        self._title_lbl.setObjectName("trackCardTitle")
        title_font = QFont()
        title_font.setPointSize(10)
        self._title_lbl.setFont(title_font)
        text_col.addWidget(self._title_lbl)

        self._artist_lbl = CaptionLabel(artist or "—")
        self._artist_lbl.setObjectName("trackCardArtist")
        text_col.addWidget(self._artist_lbl)

        # Progress bar (hidden until download starts)
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("trackCardProgressBar")
        self._progress_bar.setRange(0, 1000)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(3)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        text_col.addWidget(self._progress_bar)

        # Speed/ETA label (hidden until download starts)
        self._speed_lbl = CaptionLabel("")
        self._speed_lbl.setObjectName("trackCardSpeed")
        self._speed_lbl.setVisible(False)
        text_col.addWidget(self._speed_lbl)

        outer.addLayout(text_col, stretch=1)
        outer.addSpacing(8)

        # Badge column — Trailing edge so badges mirror under RTL (left in HE).
        badge_col = QVBoxLayout()
        badge_col.setSpacing(4)
        badge_col.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignTrailing)

        self._dur_badge  = self._make_badge(duration or "--:--", "default")
        self._plat_badge = self._make_badge(platform.upper(), platform)
        badge_col.addWidget(self._dur_badge,  alignment=Qt.AlignmentFlag.AlignTrailing)
        badge_col.addWidget(self._plat_badge, alignment=Qt.AlignmentFlag.AlignTrailing)
        outer.addLayout(badge_col)
        outer.addSpacing(4)

        # Action buttons column
        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        btn_col.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Remove button (shown on hover). Modern FluentIcon glyphs replace the
        # old Unicode ✕ / ⏸ / ▶ text; each carries an accessible name so
        # screen readers announce the icon-only button.
        self._remove_btn = ToolButton()
        self._remove_btn.setObjectName("trackCardRemoveBtn")
        self._remove_btn.setFixedSize(28, 28)
        self._remove_btn.setIconSize(QSize(13, 13))
        self._remove_btn.setVisible(False)
        self._remove_btn.setToolTip(t("card_remove_tooltip"))
        self._remove_btn.setAccessibleName(t("card_remove_tooltip"))
        self._remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(self.queue_index)
        )
        btn_col.addWidget(self._remove_btn)

        # Pause button (hidden by default)
        self._pause_btn = ToolButton()
        self._pause_btn.setObjectName("trackCardPauseBtn")
        self._pause_btn.setFixedSize(28, 28)
        self._pause_btn.setIconSize(QSize(13, 13))
        self._pause_btn.setVisible(False)
        self._pause_btn.setToolTip(t("card_pause_tooltip"))
        self._pause_btn.setAccessibleName(t("card_pause_tooltip"))
        self._pause_btn.clicked.connect(lambda: self.pause_requested.emit(self.queue_index))
        btn_col.addWidget(self._pause_btn)

        # Resume button (hidden by default)
        self._resume_btn = ToolButton()
        self._resume_btn.setObjectName("trackCardResumeBtn")
        self._resume_btn.setFixedSize(28, 28)
        self._resume_btn.setIconSize(QSize(13, 13))
        self._resume_btn.setVisible(False)
        self._resume_btn.setToolTip(t("card_resume_tooltip"))
        self._resume_btn.setAccessibleName(t("card_resume_tooltip"))
        self._resume_btn.clicked.connect(lambda: self.resume_requested.emit(self.queue_index))
        btn_col.addWidget(self._resume_btn)

        outer.addLayout(btn_col)

        self._refresh_action_icons()
        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._refresh_action_icons)

    def _refresh_action_icons(self) -> None:
        """(Re)tint the action-button icons. Pause follows the live accent."""
        accent = get_colors().accent
        self._remove_btn.setIcon(FluentIcon.CLOSE.icon(color=QColor(_REMOVE_COLOR)))
        self._pause_btn.setIcon(FluentIcon.PAUSE_BOLD.icon(color=QColor(accent)))
        self._resume_btn.setIcon(FluentIcon.PLAY_SOLID.icon(color=QColor(_RESUME_COLOR)))

    def _make_badge(self, text: str, kind: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("trackCardPlatBadge")
        plat_key = kind.lower()
        if plat_key in ("ytm", "ytmusic"):
            plat_key = "ytmusic"
        elif plat_key == "youtube":
            plat_key = "youtube"
        elif plat_key == "spotify":
            plat_key = "spotify"
        else:
            plat_key = "unknown"
        lbl.setProperty("platform", plat_key)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return lbl

    def _apply_shadow(self) -> None:
        fx = QGraphicsDropShadowEffect(self)
        fx.setBlurRadius(12)
        fx.setXOffset(0)
        fx.setYOffset(2)
        fx.setColor(QColor(0, 0, 0, 60))
        self.setGraphicsEffect(fx)


    # ── Public API ─────────────────────────────────────────────────────────────

    def is_selected(self) -> bool:
        return self._check.isChecked()

    def set_selected(self, checked: bool) -> None:
        self._check.setChecked(checked)

    @property
    def platform(self) -> str:
        """Return the platform identifier (e.g., 'spotify', 'youtube')."""
        return self._platform

    def get_status(self) -> str:
        return self._status

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        """Load and scale the thumbnail while preserving native aspect ratio."""
        if pixmap.isNull():
            return
            
        w = pixmap.width()
        h = pixmap.height()
        target_h = _THUMB_H
        
        # If the image is square (or very close), make the container square
        if w > 0 and h > 0 and (w / h) < 1.2:
            target_w = _THUMB_H
        else:
            target_w = _THUMB_W
            
        self._thumb_lbl.setFixedSize(target_w, target_h)
        
        # Scale to fit within target bounds (KeepAspectRatio)
        scaled = pixmap.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        
        self._thumb_lbl.setPixmap(scaled)

    def set_progress(self, fraction: float) -> None:
        self._progress_bar.setValue(int(fraction * 1000))
        # Visible for every in-flight stage. Gating on fraction > 0 used to hide
        # the bar for the whole opening stretch, which is precisely when a user
        # is looking for evidence that anything is happening at all.
        self._progress_bar.setVisible(self._status in _PHASE_CAPTIONS)

    def update_speed(self, speed_bps: Optional[float], eta_seconds: Optional[float]) -> None:
        """Record the live transfer rate for the caption line.

        ``eta_seconds`` is yt-dlp's own estimate and covers the byte transfer
        only, so it is deliberately ignored: it reads "0:01" while ffmpeg,
        tagging and publish still have several seconds to run. The caption
        shows the whole-track remaining time from set_phase() instead.
        """
        self._speed_bps = speed_bps if (speed_bps and speed_bps > 0) else None
        self._refresh_caption()

    def set_phase(self, phase: str, remaining_seconds: Optional[float] = None) -> None:
        """Show which stage of its life this track is in, and how long is left.

        A track spends most of its wall time outside the byte transfer — being
        matched, waiting its turn at the conservative YouTube gate, and then in
        ffmpeg, tagging and publish. The card used to render only the transfer,
        so it froze, raced, and froze again. Every stage now names itself.
        """
        self._phase = phase
        self._phase_remaining = remaining_seconds
        self.set_status(phase)
        self._refresh_caption()

    def _refresh_caption(self) -> None:
        """Caption under the bar: what it is doing, how fast, how long left."""
        phase = getattr(self, "_phase", "") or self._status
        if phase not in _PHASE_CAPTIONS:
            self._speed_lbl.setVisible(False)
            self._speed_lbl.setText("")
            return

        parts = [t(_PHASE_CAPTIONS[phase])]
        speed = getattr(self, "_speed_bps", None)
        if phase == "downloading" and speed:
            if speed >= 1_048_576:
                parts.append(isolate_number(f"{speed / 1_048_576:.1f} MB/s"))
            else:
                parts.append(isolate_number(f"{speed / 1024:.0f} KB/s"))

        remaining = getattr(self, "_phase_remaining", None)
        if remaining is not None and remaining > 0:
            parts.append(isolate_number(seconds_to_str(int(remaining))))

        self._speed_lbl.setText(" · ".join(parts))
        self._speed_lbl.setVisible(True)

    def set_status(self, status: str) -> None:
        """
        Update the visual state.

        status : one of "queued" | "matching" | "waiting" | "starting" |
                         "downloading" | "processing" |
                         "done" | "error" | "cancelled" | "paused"
        """
        self._status = status
        self.status_changed.emit(status)
        self._dot.setProperty("status", status)
        self._dot.style().unpolish(self._dot)
        self._dot.style().polish(self._dot)

        # Update button visibility
        self._pause_btn.setVisible(status in _PHASE_CAPTIONS)
        self._resume_btn.setVisible(status == "paused")

        # Hide the caption once the track is no longer in flight.
        if status not in _PHASE_CAPTIONS:
            self._speed_lbl.setVisible(False)
            self._speed_lbl.setText("")

        # Disable checkbox once download is in flight
        self._check.setEnabled(status == "queued")

    def set_artist(self, artist: str) -> None:
        self._artist_lbl.setText(artist or "—")

    def set_title(self, title: str) -> None:
        self._title_lbl.setText(title[:80])

    def update_queue_index(self, new_index: int) -> None:
        self.queue_index = new_index

    # ── Hover events ──────────────────────────────────────────────────────────

    def enterEvent(self, event) -> None:
        if self._status == "queued":
            self._remove_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._remove_btn.setVisible(False)
        super().leaveEvent(event)

    # ── Drag & drop ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_start_pos is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
            and (
                event.position().toPoint() - self._drag_start_pos
            ).manhattanLength() >= QApplication.startDragDistance()
        ):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setText(str(self.queue_index))
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
        super().mouseMoveEvent(event)
