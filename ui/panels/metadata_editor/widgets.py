"""
ui/panels/metadata_editor/widgets.py  –  small Tag Editor building blocks
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


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
