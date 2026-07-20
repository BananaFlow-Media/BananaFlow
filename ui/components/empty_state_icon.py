from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from qfluentwidgets import FluentIcon
from ui.theme_manager import ThemeManager, get_colors


class EmptyStateIcon(QWidget):
    """Small accent-aware line icons for panel empty states."""

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        if kind == "bars":
            self.setFixedSize(58, 30)
        elif kind == "sync":
            self.setFixedSize(48, 48)
        else:
            self.setFixedSize(72, 72)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self.update)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        c = get_colors()
        accent = QColor(c.accent)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._kind != "bars":
            bg = QColor(accent)
            bg.setAlpha(24)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            inset = 2
            radius = 14 if self._kind == "sync" else 18
            painter.drawRoundedRect(
                QRectF(inset, inset, self.width() - inset * 2, self.height() - inset * 2),
                radius,
                radius,
            )

        pen = QPen(accent, 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self._kind == "bars":
            self._paint_bars(painter, accent)
        elif self._kind == "search":
            self._paint_search(painter)
        elif self._kind == "history":
            self._paint_history(painter, accent)
        elif self._kind == "tag":
            self._paint_tag(painter, accent)
        elif self._kind == "upload":
            self._paint_upload(painter)
        elif self._kind == "sync":
            self._paint_sync(painter)

    def _paint_bars(self, painter: QPainter, accent: QColor) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        x = 6
        for height in (15, 18, 18, 18, 15):
            top = (self.height() - height) / 2
            painter.drawRoundedRect(QRectF(x, top, 5, height), 2, 2)
            x += 10

    def _paint_search(self, painter: QPainter) -> None:
        painter.drawEllipse(QPointF(33, 31), 10.5, 10.5)
        painter.drawLine(QPointF(41, 39), QPointF(51, 49))

    def _paint_history(self, painter: QPainter, accent: QColor) -> None:
        icon = FluentIcon.HISTORY.icon(color=accent)
        icon.paint(painter, QRect(22, 22, 28, 28))

    def _paint_tag(self, painter: QPainter, accent: QColor) -> None:
        icon = FluentIcon.TAG.icon(color=accent)
        icon.paint(painter, QRect(22, 22, 28, 28))

    def _paint_upload(self, painter: QPainter) -> None:
        painter.drawLine(QPointF(36, 48), QPointF(36, 25))
        painter.drawLine(QPointF(27, 34), QPointF(36, 25))
        painter.drawLine(QPointF(45, 34), QPointF(36, 25))
        painter.drawLine(QPointF(25, 52), QPointF(47, 52))

    def _paint_sync(self, painter: QPainter) -> None:
        """Clean, centered two-arrow refresh glyph that scales with the widget."""
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r = min(self.width(), self.height()) * 0.27
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)

        # Two opposing arcs leaving a gap at each end for an arrowhead.
        painter.drawArc(rect, 35 * 16, 150 * 16)    # upper arc
        painter.drawArc(rect, 215 * 16, 150 * 16)   # lower arc

        head = max(3.0, r * 0.42)
        self._sync_arrow(painter, cx, cy, r, 185, head)   # end of upper arc
        self._sync_arrow(painter, cx, cy, r, 5, head)     # end of lower arc

    @staticmethod
    def _sync_arrow(
        painter: QPainter, cx: float, cy: float, r: float, angle_deg: float, size: float
    ) -> None:
        """Draw a small 'V' arrowhead at a point on the circle, aimed along the
        counter-clockwise tangent (matching the arc sweep direction)."""
        a = math.radians(angle_deg)
        tip = QPointF(cx + r * math.cos(a), cy - r * math.sin(a))
        # CCW tangent (screen coords, y down) and outward normal.
        tx, ty = -math.sin(a), -math.cos(a)
        nx, ny = math.cos(a), -math.sin(a)
        back_x = tip.x() - tx * size
        back_y = tip.y() - ty * size
        b1 = QPointF(back_x + nx * size * 0.75, back_y + ny * size * 0.75)
        b2 = QPointF(back_x - nx * size * 0.75, back_y - ny * size * 0.75)
        painter.drawLine(tip, b1)
        painter.drawLine(tip, b2)
