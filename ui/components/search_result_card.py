"""
ui/components/search_result_card.py  –  Category-aware search result widget
===========================================================================
Displays one result from SearchEngine in the search panel.  Adapts its
layout and action button to the ResultKind of the result:

  TRACK    – thumbnail + title/artist/duration/views  + "＋ Add" button
  ALBUM    – thumbnail + title/item_count             + "Browse" button
  PLAYLIST – thumbnail + title/item_count             + "Browse" button
  ARTIST   – circular avatar placeholder + name       + "Browse" button
  CHANNEL  – square icon + name                       + "Browse" button

Signals
-------
add_to_queue(SearchResult)
    Emitted for TRACK cards when the user clicks "＋ Add".
browse_requested(SearchResult)
    Emitted for ALBUM / PLAYLIST / ARTIST / CHANNEL cards when the user
    clicks "Browse".  SearchPanel forwards this to AppWindow for drill-down.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PrimaryPushButton, PushButton

from core.search_engine import ResultKind, SearchResult
from ui.i18n import t
# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_RADIUS  = 8
_THUMB_W = 60
_THUMB_H = 60

_PLATFORM_COLORS: dict[str, tuple[str, str]] = {
    "YOUTUBE":  ("#cc2200", "#ffffff"),
    "YTMUSIC":  ("#cc2200", "#ffffff"),
    "SPOTIFY":  ("#1aa34a", "#ffffff"),
    "UNKNOWN":  ("#252533", "#8888a8"),
}

# ResultKind icon characters shown in the avatar placeholder for non-track kinds
_KIND_ICONS: dict[ResultKind, str] = {
    ResultKind.ARTIST:   "🎤",
    ResultKind.ALBUM:    "💿",
    ResultKind.PLAYLIST: "🎵",
    ResultKind.CHANNEL:  "📺",
}


def _placeholder_pixmap(w: int = _THUMB_W, h: int = _THUMB_H) -> QPixmap:
    from PySide6.QtGui import QImage
    from ui.theme_manager import get_colors
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(get_colors().border))
    return QPixmap.fromImage(img)


# ──────────────────────────────────────────────────────────────────────────────
# SearchResultCard
# ──────────────────────────────────────────────────────────────────────────────

class SearchResultCard(QFrame):
    """
    Compact, category-aware card for one SearchResult.

    Parameters
    ----------
    result : SearchResult dataclass from core.search_engine.
    parent : Optional Qt parent.
    """

    add_to_queue     = Signal(object)   # SearchResult  – TRACK only
    browse_requested = Signal(object)   # SearchResult  – ALBUM/PLAYLIST/ARTIST/CHANNEL

    def __init__(
        self,
        result: SearchResult,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._result = result
        self._build()

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(10)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)



    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def result(self) -> SearchResult:
        return self._result

    def set_thumbnail(self, raw_bytes: bytes) -> None:
        """Load the thumbnail from raw image bytes (called by ThumbnailWorker)."""
        if not hasattr(self, "_thumb_label"):
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(raw_bytes):
            # Premium center-crop logic
            target_w = _THUMB_W
            target_h = _THUMB_H

            # 1. Scale to cover the target area
            scaled = pixmap.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )

            # 2. Center crop
            x = (scaled.width()  - target_w) // 2
            y = (scaled.height() - target_h) // 2
            cropped = scaled.copy(x, y, target_w, target_h)

            self._thumb_label.setPixmap(cropped)
            # Clear text if it was showing an icon
            self._thumb_label.setText("")
            # Apply border radius if it was lost (though QLabel usually keeps it)
            self._thumb_label.update()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        r    = self._result
        kind = r.kind

        self.setFixedHeight(68)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(0)

        # ── Rank label ────────────────────────────────────────────────────────
        self._rank_lbl = QLabel(f"{r.result_index}.")
        self._rank_lbl.setObjectName("resultRank")
        self._rank_lbl.setFixedWidth(24)
        self._rank_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._rank_lbl)
        row.addSpacing(6)

        # ── Thumbnail / avatar ────────────────────────────────────────────────
        self._thumb_label = self._build_thumb(kind)
        row.addWidget(self._thumb_label)
        row.addSpacing(10)

        # ── Text column ───────────────────────────────────────────────────────
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        self._title_lbl = BodyLabel(r.title)
        self._title_lbl.setObjectName("resultTitle")
        title_font = QFont()
        title_font.setPointSize(10)
        self._title_lbl.setFont(title_font)
        self._title_lbl.setMaximumWidth(360)
        text_col.addWidget(self._title_lbl)

        self._sub_lbl = CaptionLabel(self._build_sub_text(r, kind))
        self._sub_lbl.setObjectName("resultSub")
        text_col.addWidget(self._sub_lbl)

        row.addLayout(text_col, stretch=1)
        row.addSpacing(8)

        # ── Platform badge ────────────────────────────────────────────────────
        platform_str = r.platform.name.upper()
        bg, fg = _PLATFORM_COLORS.get(platform_str, _PLATFORM_COLORS["UNKNOWN"])
        plat_lbl = QLabel(platform_str)
        plat_lbl.setFont(QFont("Consolas", 8))
        plat_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plat_lbl.setFixedHeight(18)
        plat_lbl.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 3px; padding: 0 5px;"
        )
        row.addWidget(plat_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(8)

        # ── Action button ─────────────────────────────────────────────────────
        if kind == ResultKind.TRACK:
            btn = PrimaryPushButton(t("search_card_add_btn"))
            btn.setFixedSize(72, 30)
            btn.clicked.connect(lambda: self.add_to_queue.emit(self._result))
        else:
            btn = PushButton(t("search_card_browse_btn"))
            btn.setObjectName("browseBtn")
            btn.setFixedSize(84, 30)
            btn.clicked.connect(lambda: self.browse_requested.emit(self._result))

        row.addWidget(btn, alignment=Qt.AlignmentFlag.AlignVCenter)

    def _build_thumb(self, kind: ResultKind) -> QLabel:
        """Return a thumbnail label; initialized with placeholder."""
        lbl = QLabel()
        lbl.setFixedSize(_THUMB_W, _THUMB_H)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setPixmap(_placeholder_pixmap())
        self._thumb_is_artist = (kind == ResultKind.ARTIST)
        lbl.setObjectName("resultThumb")
        radius = _THUMB_H // 2 if self._thumb_is_artist else 6
        lbl.setStyleSheet(f"border-radius: {radius}px;")
        return lbl


    @staticmethod
    def _build_sub_text(r: SearchResult, kind: ResultKind) -> str:
        """Build the secondary info line based on the result kind."""
        if kind == ResultKind.TRACK:
            parts = []
            if r.artist:
                parts.append(r.artist)
            if r.duration_str:
                parts.append(r.duration_str)
            vc = r.view_count_str() if hasattr(r, "view_count_str") else ""
            if vc:
                parts.append(vc)
            return "  ·  ".join(parts) if parts else "—"

        if kind == ResultKind.ARTIST:
            return r.artist or r.platform.name.capitalize()

        if kind in (ResultKind.ALBUM, ResultKind.PLAYLIST):
            parts = []

            # 1. Release type (Album/Single/EP/Playlist)
            if r.release_type:
                # Heuristic: Spotify often marks 4-6 tracks as 'single', we call them 'EP'
                display_type = r.release_type
                if display_type == "single" and r.item_count and 4 <= r.item_count <= 6:
                    display_type = "ep"

                parts.append(t(f"release_{display_type}"))
            elif kind == ResultKind.PLAYLIST:
                parts.append(t("release_playlist"))

            # 2. Artist
            if r.artist:
                parts.append(r.artist)

            # 3. Item count
            if r.item_count:
                label_key = "tracks" if kind == ResultKind.ALBUM else "items"
                parts.append(f"{r.item_count} {t(label_key)}")

            return "  ·  ".join(parts) if parts else kind.name.capitalize()

        if kind == ResultKind.CHANNEL:
            vc = r.view_count_str() if hasattr(r, "view_count_str") else ""
            return vc or "Channel"

        return "—"
