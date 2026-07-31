"""
ui/panels/metadata_editor/widgets.py  –  small Tag Editor building blocks
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
    QWidget,
)


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


def _elide_floor(icon_width: int) -> int:
    """Smallest sensible width for a control that elides its own label.

    Enough for an icon plus a few characters and an ellipsis -- past that the
    control says nothing, so there is no point letting it shrink further.
    """
    return icon_width + 44


class ElidedLabel(QLabel):
    """A QLabel that elides rather than forcing its whole text onto the layout.

    A plain QLabel's minimum width *is* its text: one long file name in the
    inspector is enough to push the entire page wider than the pane, and since
    the inspector's scroll areas have no horizontal bar the overflow is simply
    cut away. This keeps the full string as the widget's real ``text()`` (and
    its tooltip) while showing an elided copy, so the layout can shrink and
    nothing is silently lost.
    """

    _FLOOR = 44

    def __init__(self, text: str = "", parent=None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = mode
        self._eliding = False
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text or ""
        # The elided copy hides part of the string, so the whole of it has to
        # stay reachable somewhere.
        self.setToolTip(self._full_text)
        self._apply_elision()

    def clear(self) -> None:
        # QLabel::clear() reaches setText() in C++, which never dispatches back
        # to the override above -- so without this the label would keep
        # reporting the text it was just told to drop.
        self.setText("")

    def text(self) -> str:
        """The full string -- callers and tests must never see the ellipsis."""
        return self._full_text

    def setElideMode(self, mode: Qt.TextElideMode) -> None:
        self._elide_mode = mode
        self._apply_elision()

    def _apply_elision(self) -> None:
        if self._eliding:
            return
        self._eliding = True
        try:
            width = self.contentsRect().width()
            if width <= 0:
                super().setText(self._full_text)
            else:
                super().setText(self.fontMetrics().elidedText(
                    self._full_text, self._elide_mode, width))
        finally:
            self._eliding = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elision()

    def sizeHint(self) -> QSize:
        # Derived from the full text, so a wide pane still lays the label out
        # at its natural width instead of the truncated one.
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self._full_text) + 2, hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), self._FLOOR), hint.height())


class ElidedPushButton(QPushButton):
    """A QPushButton that shrinks past its label instead of pinning the layout.

    QPushButton reports its full label as its minimum width and neither wraps
    nor elides, so a row of three of them is an immovable floor. Only the
    *painted* text is shortened here: ``text()``, the size hint and the
    accessible name all still carry the real label.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # A push button ships as QSizePolicy::Minimum horizontally, which has
        # no shrink flag -- so Qt's layouts read its *sizeHint* as the floor
        # and never even ask minimumSizeHint(). Without this the elision below
        # would be dead code.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, self.sizePolicy().verticalPolicy())

    def _icon_width(self) -> int:
        return self.iconSize().width() + 6 if not self.icon().isNull() else 0

    def minimumSizeHint(self) -> QSize:
        hint = self.sizeHint()
        return QSize(min(hint.width(), _elide_floor(self._icon_width())), hint.height())

    def paintEvent(self, _event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        if option.text:
            content = self.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, self)
            available = content.width() - self._icon_width()
            option.text = option.fontMetrics.elidedText(
                option.text, Qt.TextElideMode.ElideRight, max(0, available))
        # Through QStylePainter so the widget keeps whatever the active
        # stylesheet draws for it; only the string changed.
        QStylePainter(self).drawControl(QStyle.ControlElement.CE_PushButton, option)


class ElidedToolButton(QToolButton):
    """``ElidedPushButton`` for the text-beside-icon tool buttons."""

    _CHROME = 18

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, self.sizePolicy().verticalPolicy())

    def _icon_width(self) -> int:
        if self.icon().isNull() or self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextOnly:
            return 0
        return self.iconSize().width() + 6

    def minimumSizeHint(self) -> QSize:
        hint = self.sizeHint()
        return QSize(min(hint.width(), _elide_floor(self._icon_width()) + self._CHROME),
                     hint.height())

    def paintEvent(self, _event) -> None:
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        if option.text and option.toolButtonStyle != Qt.ToolButtonStyle.ToolButtonIconOnly:
            available = self.width() - self._icon_width() - self._CHROME
            option.text = option.fontMetrics.elidedText(
                option.text, Qt.TextElideMode.ElideRight, max(0, available))
        QStylePainter(self).drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)


class ResponsiveButtonFlow(QWidget):
    """A compact control grid that adds rows as the available width shrinks.

    ``fill=True`` stretches each row's controls to the full width, which is
    what a row or grid of equal buttons looked like before it became
    responsive; the default packs them at their natural width.
    """

    def __init__(self, parent=None, *, fill: bool = False) -> None:
        super().__init__(parent)
        self._buttons: list[QWidget] = []
        self._horizontal_spacing = 6
        self._vertical_spacing = 6
        self._fill = fill
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def add_button(self, button: QWidget) -> None:
        button.setParent(self)
        # A real layout drops a hidden child and closes the gap; this one has
        # to be told when one comes or goes.
        button.installEventFilter(self)
        self._buttons.append(button)
        self._reflow()

    # Rows of combo boxes and line edits reflow the same way buttons do.
    add_widget = add_button

    def _laid_out(self) -> list[QWidget]:
        """The controls that currently take part in the flow.

        Before the page itself is on screen Qt reports every child as hidden,
        and skipping them all would collapse the widget to nothing; the
        geometry computed then is provisional and redone on show anyway.
        """
        if not self.isVisible():
            return list(self._buttons)
        return [button for button in self._buttons if not button.isHidden()]

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide) and obj in self._buttons:
            self._reflow()
            self.updateGeometry()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
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
        buttons = self._laid_out()
        if not buttons:
            return QSize()
        natural_width = sum(button.sizeHint().width() for button in buttons) + (
            len(buttons) - 1) * self._horizontal_spacing
        return QSize(natural_width, self.heightForWidth(natural_width))

    def minimumSizeHint(self) -> QSize:
        buttons = self._laid_out()
        if not buttons:
            return QSize()
        widest = max(self._floor_width(button) for button in buttons)
        return QSize(widest, self.heightForWidth(widest))

    @staticmethod
    def _floor_width(widget: QWidget) -> int:
        """How narrow one control may get, the way Qt's own layouts read it:
        an explicitly set minimum wins over the content-derived hint."""
        explicit = widget.minimumWidth()
        if explicit > 0:
            return explicit
        return max(1, widget.minimumSizeHint().width())

    def _rows_for_width(self, width: int) -> list[list[QWidget]]:
        available = max(1, width)
        rows: list[list[QWidget]] = [[]]
        row_width = 0
        for button in self._laid_out():
            button_width = button.sizeHint().width()
            needed = button_width if not rows[-1] else self._horizontal_spacing + button_width
            if rows[-1] and row_width + needed > available:
                rows.append([])
                row_width = 0
            rows[-1].append(button)
            row_width += button_width if row_width == 0 else needed
        return [row for row in rows if row]

    def _row_widths(self, row: list[QWidget], available: int) -> list[int]:
        """Widths for one row, never exceeding the space the row actually has.

        A single control wider than the pane is clamped rather than left to
        overflow -- that is the whole point of pairing this with the elided
        controls above.
        """
        gaps = self._horizontal_spacing * (len(row) - 1)
        widths = [button.sizeHint().width() for button in row]
        budget = max(0, available - gaps)
        total = sum(widths)
        if total > budget and total > 0:
            floors = [self._floor_width(button) for button in row]
            slack = total - budget
            shrinkable = sum(max(0, w - f) for w, f in zip(widths, floors))
            if shrinkable > 0:
                taken = 0
                for i, (w, f) in enumerate(zip(widths, floors)):
                    room = max(0, w - f)
                    cut = min(room, round(slack * room / shrinkable))
                    widths[i] = w - cut
                    taken += cut
                # Rounding can leave a pixel or two; take it off the widest.
                if taken < slack:
                    widest = max(range(len(widths)), key=lambda i: widths[i])
                    widths[widest] = max(floors[widest], widths[widest] - (slack - taken))
        elif self._fill and total < budget:
            extra, remainder = divmod(budget - total, len(row))
            widths = [w + extra for w in widths]
            widths[-1] += remainder
        return widths

    def _reflow(self) -> None:
        available = self.contentsRect().width()
        rows = self._rows_for_width(available)
        required_height = self.heightForWidth(available)
        if self.minimumHeight() != required_height:
            self.setMinimumHeight(required_height)
            self.updateGeometry()

        y = 0
        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        for row in rows:
            row_height = max(button.sizeHint().height() for button in row)
            widths = self._row_widths(row, max(0, available))
            if rtl:
                x = self.width()
                for button, width in zip(row, widths):
                    x -= width
                    button.setGeometry(x, y, width, row_height)
                    x -= self._horizontal_spacing
            else:
                x = 0
                for button, width in zip(row, widths):
                    button.setGeometry(x, y, width, row_height)
                    x += width + self._horizontal_spacing
            y += row_height + self._vertical_spacing

class OpRow(QFrame):
    """Clickable, card-style action row with a wrapping label.

    Replaces a plain QPushButton so long Hebrew labels wrap to multiple
    lines instead of being elided/cut off, and gives the inspector a
    modern, touch-friendly look.

    It has to earn back what QPushButton gave away for free: keyboard focus,
    Space/Enter activation and an accessible name.  Without those the whole
    action list is mouse-only, which is how it shipped -- the eight compact
    clear buttons on the same page were reachable by Tab while these nine
    rows were not.

    It stays a QFrame rather than becoming a QAbstractButton because the
    info and settings controls live *inside* the row; a button nested in a
    button is a worse thing to hand a screen reader than a named, focusable
    frame.  The cost is that the row is announced by its name and not as a
    button role.
    """
    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("metaOpRow")
        self._action_enabled = True
        self.setProperty("actionEnabled", "true")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Tab reaches it, and a click still moves focus here so the focus ring
        # follows the pointer the way a real button does.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._row_layout = QHBoxLayout(self)
        self._row_layout.setContentsMargins(10, 9, 10, 9)
        self._row_layout.setSpacing(6)
        self._label = QLabel(text)
        self._label.setObjectName("metaOpRowLabel")
        self._label.setWordWrap(True)
        # A wrapping QLabel still asks for a fairly wide minimum; cap it so a
        # long action name takes another line instead of widening the pane.
        self._label.setMinimumWidth(60)
        # Let clicks fall through the label to the frame so the whole row
        # is the click target.
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._row_layout.addWidget(self._label, 1)
        self.setAccessibleName(text)

    def setText(self, text: str) -> None:
        """Keep the visible label and the announced name in step."""
        self._label.setText(text)
        self.setAccessibleName(text)

    def text(self) -> str:
        return self._label.text()

    def add_side_button(self, btn: QPushButton) -> None:
        self._row_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)

    def setActionEnabled(self, enabled: bool) -> None:
        self._action_enabled = enabled
        self.setProperty("actionEnabled", "true" if enabled else "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        # A row that cannot be run must not be a tab stop either, or keyboard
        # users land on it and nothing happens.
        self.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if enabled else Qt.FocusPolicy.NoFocus)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _activate(self) -> None:
        if self._action_enabled:
            self.clicked.emit()

    def mouseReleaseEvent(self, event) -> None:
        if (self._action_enabled
                and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        # Space and Enter are what a screen-reader user presses on something
        # announced as a button; Return is what a sighted keyboard user tries.
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate()
            event.accept()
            return
        super().keyPressEvent(event)
