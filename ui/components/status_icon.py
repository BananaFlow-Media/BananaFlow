"""
ui/components/status_icon.py  –  One coherent status-icon family
==================================================================
The Downloads-page footer used to communicate state by pasting emoji
(🔍 ✅ ⚠ ❌ 🔴 🚫 📡) straight into the translated status string. Emoji
render as multi-colour glyphs that ignore the theme, differ per platform,
clash with the flat FluentIcon set used everywhere else, and lean on
colour alone.

This widget replaces all of that with a single, theme-aware, high-DPI
vector family drawn from one QPainter factory so every state shares the
same stroke weight and optical size. Severity is carried by **shape**
(check / triangle / cross / bars / …) as well as colour, so it stays
legible in high-contrast mode and for colour-blind users.

qfluentwidgets.FluentIcon has no warning/error member, so mixing it with
custom glyphs would break stroke-weight consistency — hence one centralized
factory here (rule-3 in the icon guidelines).
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ui.theme_manager import (
    ThemeManager,
    get_colors,
    SUCCESS_COLOR,
    ERROR_COLOR,
    WARNING_COLOR,
)


class StatusKind(Enum):
    """Severity/activity categories the footer can express."""

    NONE       = "none"        # idle — nothing drawn
    ACTIVITY   = "activity"    # neutral in-progress (fetch/scan/download)
    SUCCESS    = "success"     # a terminal message went well
    WARNING    = "warning"     # finished with caveats
    ERROR      = "error"       # stopped by an error
    PAUSED     = "paused"      # user paused
    CANCELLING = "cancelling"  # shutdown in progress / stopped
    OFFLINE    = "offline"     # no connectivity


def _color_for(kind: StatusKind) -> str:
    c = get_colors()
    return {
        StatusKind.NONE:       c.text_tertiary,
        StatusKind.ACTIVITY:   c.accent,
        StatusKind.SUCCESS:    SUCCESS_COLOR,
        StatusKind.WARNING:    WARNING_COLOR,
        StatusKind.ERROR:      ERROR_COLOR,
        StatusKind.PAUSED:     WARNING_COLOR,
        StatusKind.CANCELLING: c.text_secondary,
        StatusKind.OFFLINE:    ERROR_COLOR,
    }[kind]


def paint_status_glyph(painter: QPainter, kind: StatusKind, size: float, color: str) -> None:
    """Draw a `kind` glyph filling a `size`×`size` box at the painter origin.

    Kept module-level so the same vectors can be baked into a QIcon (see
    :func:`status_icon`) or painted live by the inline widget.
    """
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    qc = QColor(color)
    stroke = max(1.4, size * 0.11)
    pen = QPen(qc, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

    cx = cy = size / 2.0
    r = size * 0.40

    if kind in (StatusKind.NONE,):
        return

    if kind == StatusKind.ACTIVITY:
        # A neutral 300° ring with a gap — reads as "working".
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        painter.drawArc(rect, 40 * 16, 300 * 16)
        return

    if kind == StatusKind.SUCCESS:
        painter.setPen(pen)
        path = QPainterPath()
        path.moveTo(cx - r * 0.62, cy + r * 0.05)
        path.lineTo(cx - r * 0.15, cy + r * 0.55)
        path.lineTo(cx + r * 0.68, cy - r * 0.52)
        painter.drawPath(path)
        return

    if kind == StatusKind.WARNING:
        # Rounded triangle with a bang.
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx, cy - r * 0.85)
        path.lineTo(cx + r * 0.95, cy + r * 0.72)
        path.lineTo(cx - r * 0.95, cy + r * 0.72)
        path.closeSubpath()
        painter.drawPath(path)
        # exclamation
        bar = QPen(qc, stroke)
        bar.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(bar)
        painter.drawLine(int(cx), int(cy - r * 0.12), int(cx), int(cy + r * 0.28))
        painter.drawPoint(int(cx), int(cy + r * 0.52))
        return

    if kind == StatusKind.PAUSED:
        # Two solid rounded bars.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qc)
        bar_w = size * 0.16
        bar_h = size * 0.5
        gap = size * 0.12
        top = cy - bar_h / 2
        painter.drawRoundedRect(QRectF(cx - gap / 2 - bar_w, top, bar_w, bar_h), 2, 2)
        painter.drawRoundedRect(QRectF(cx + gap / 2, top, bar_w, bar_h), 2, 2)
        return

    if kind == StatusKind.ERROR:
        # A cross (×) — distinct shape from the warning triangle.
        painter.setPen(pen)
        d = r * 0.62
        painter.drawLine(int(cx - d), int(cy - d), int(cx + d), int(cy + d))
        painter.drawLine(int(cx - d), int(cy + d), int(cx + d), int(cy - d))
        return

    if kind == StatusKind.CANCELLING:
        # A hollow octagon (stop) outline.
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        import math
        path = QPainterPath()
        for i in range(8):
            a = math.radians(22.5 + i * 45)
            x = cx + r * math.cos(a)
            y = cy + r * math.sin(a)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        painter.drawPath(path)
        return

    if kind == StatusKind.OFFLINE:
        # A wifi-ish set of arcs with a slash.
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for rr in (r * 0.9, r * 0.55):
            rect = QRectF(cx - rr, cy - rr * 0.4, 2 * rr, 2 * rr)
            painter.drawArc(rect, 30 * 16, 120 * 16)
        painter.setBrush(qc)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(cx - size * 0.05, cy + r * 0.35, size * 0.1, size * 0.1))
        slash = QPen(qc, stroke)
        slash.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(slash)
        painter.drawLine(int(cx - r), int(cy - r * 0.7), int(cx + r), int(cy + r * 0.7))
        return


def status_icon(kind: StatusKind, size: int = 16, color: str | None = None) -> QIcon:
    """Bake a status glyph into a QIcon (for use as a button/label icon)."""
    if kind == StatusKind.NONE:
        return QIcon()
    col = color or _color_for(kind)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    paint_status_glyph(painter, kind, float(size), col)
    painter.end()
    return QIcon(pm)


class StatusIcon(QWidget):
    """Small inline status glyph that follows the current theme.

    Painted live (not a cached pixmap) so a theme/accent switch recolours it
    immediately, and so it stays crisp at every DPI scale factor.
    """

    def __init__(self, size: int = 16, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._kind = StatusKind.NONE
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self.update)

    def set_kind(self, kind: StatusKind) -> None:
        if kind != self._kind:
            self._kind = kind
            self.setVisible(kind != StatusKind.NONE)
            self.update()

    def kind(self) -> StatusKind:
        return self._kind

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        if self._kind == StatusKind.NONE:
            return
        painter = QPainter(self)
        paint_status_glyph(painter, self._kind, float(self._size), _color_for(self._kind))
        painter.end()
