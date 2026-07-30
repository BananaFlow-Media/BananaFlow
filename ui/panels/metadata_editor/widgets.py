"""
ui/panels/metadata_editor/widgets.py  –  small Tag Editor building blocks
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget


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


class ResponsiveButtonFlow(QWidget):
    """A compact button grid that adds rows as the available width shrinks."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._horizontal_spacing = 6
        self._vertical_spacing = 6
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def add_button(self, button: QPushButton) -> None:
        button.setParent(self)
        self._buttons.append(button)
        self._reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        rows = self._rows_for_width(width)
        if not rows:
            return 0
        return sum(max(button.sizeHint().height() for button in row) for row in rows) + (
            len(rows) - 1) * self._vertical_spacing

    def sizeHint(self) -> QSize:
        if not self._buttons:
            return QSize()
        natural_width = sum(button.sizeHint().width() for button in self._buttons) + (
            len(self._buttons) - 1) * self._horizontal_spacing
        return QSize(natural_width, self.heightForWidth(natural_width))

    def minimumSizeHint(self) -> QSize:
        if not self._buttons:
            return QSize()
        widest = max(button.sizeHint().width() for button in self._buttons)
        return QSize(widest, self.heightForWidth(widest))

    def _rows_for_width(self, width: int) -> list[list[QPushButton]]:
        available = max(1, width)
        rows: list[list[QPushButton]] = [[]]
        row_width = 0
        for button in self._buttons:
            button_width = button.sizeHint().width()
            needed = button_width if not rows[-1] else self._horizontal_spacing + button_width
            if rows[-1] and row_width + needed > available:
                rows.append([])
                row_width = 0
            rows[-1].append(button)
            row_width += button_width if row_width == 0 else needed
        return [row for row in rows if row]

    def _reflow(self) -> None:
        rows = self._rows_for_width(self.contentsRect().width())
        required_height = self.heightForWidth(self.contentsRect().width())
        if self.minimumHeight() != required_height:
            self.setMinimumHeight(required_height)
            self.updateGeometry()

        y = 0
        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        for row in rows:
            row_height = max(button.sizeHint().height() for button in row)
            if rtl:
                x = self.width()
                for button in row:
                    size = button.sizeHint()
                    x -= size.width()
                    button.setGeometry(x, y, size.width(), row_height)
                    x -= self._horizontal_spacing
            else:
                x = 0
                for button in row:
                    size = button.sizeHint()
                    button.setGeometry(x, y, size.width(), row_height)
                    x += size.width() + self._horizontal_spacing
            y += row_height + self._vertical_spacing

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
