"""
ui/panels/metadata_editor/widgets.py  –  small Tag Editor building blocks
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class ArtworkDropPreview(QLabel):
    """Dedicated, narrow image drop target; never intercepts table drops."""
    def __init__(self, callback, parent=None) -> None:
        super().__init__(parent)
        self._callback = callback
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).suffix.lower() in {".jpg", ".jpeg", ".png"}:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self._callback(Path(urls[0].toLocalFile()))
            event.acceptProposedAction()
        else:
            event.ignore()


class VerticalLabel(QLabel):
    """Compact vertical caption used by the prototype's collapsed panes."""

    def sizeHint(self):
        hint = super().sizeHint()
        hint.transpose()
        return hint

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.transpose()
        return hint

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.translate(self.width(), 0)
        painter.rotate(90)
        painter.drawText(0, 0, self.height(), self.width(), self.alignment(), self.text())

class OpRow(QFrame):
    """Clickable, card-style action row with a wrapping label.

    Replaces a plain QPushButton so long Hebrew labels wrap to multiple
    lines instead of being elided/cut off, and gives the inspector a
    modern, touch-friendly look.
    """
    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("metaOpRow")
        self._action_enabled = True
        self.setProperty("actionEnabled", "true")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._row_layout = QHBoxLayout(self)
        self._row_layout.setContentsMargins(10, 9, 10, 9)
        self._row_layout.setSpacing(6)
        self._label = QLabel(text)
        self._label.setObjectName("metaOpRowLabel")
        self._label.setWordWrap(True)
        # Let clicks fall through the label to the frame so the whole row
        # is the click target.
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._row_layout.addWidget(self._label, 1)

    def add_side_button(self, btn: QPushButton) -> None:
        self._row_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)

    def setActionEnabled(self, enabled: bool) -> None:
        self._action_enabled = enabled
        self.setProperty("actionEnabled", "true" if enabled else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if (self._action_enabled
                and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)
