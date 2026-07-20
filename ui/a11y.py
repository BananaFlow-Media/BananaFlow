"""Shared accessibility helpers for the Tag Editor.

A control that carries its whole meaning in an icon and a tooltip is silent to a
screen reader: Qt derives a button's accessible name from its *text*, and an
icon-only button has none.  These helpers describe a control once so its
pointer affordance and its assistive-technology name cannot drift apart.

DPI note: ordinary QWidget/QLayout geometry (minimum/fixed sizes, icon sizes,
splitter widths, margins) must stay in Qt *logical* pixels and must never be
multiplied by ``logicalDotsPerInch()`` here.  Qt 6 already maps logical
geometry to the physical screen through its own High-DPI scaling; multiplying
it again double-scales every control.  This module intentionally has no
generic "scale a widget size" helper for that reason — only
:func:`sanitize_saved_geometry`, which clamps a persisted logical size back
into a sane range, remains.
"""

from __future__ import annotations

from contextlib import contextmanager

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QWidget


def describe(widget: QWidget, name: str, *, description: str = "",
             tooltip: str | None = None) -> QWidget:
    """Give one control its accessible name, description and tooltip together.

    ``tooltip=None`` keeps whatever tooltip the caller already set; pass a
    string to set one explicitly.
    """
    widget.setAccessibleName(str(name or ""))
    if description:
        widget.setAccessibleDescription(str(description))
    if tooltip is not None:
        widget.setToolTip(str(tooltip))
    return widget


def name_from_visible_meaning(widget: QWidget) -> QWidget:
    """Name an icon-only control from the meaning it already shows.

    Falls back to the button's text, then its tooltip.  Used at widget
    factories so a whole family of icon buttons becomes announceable at once
    instead of relying on thirty individual call sites remembering to do it.
    """
    if (widget.accessibleName() or "").strip():
        return widget
    text = ""
    if isinstance(widget, QAbstractButton):
        text = (widget.text() or "").strip()
    widget.setAccessibleName(text or (widget.toolTip() or "").strip())
    return widget


def describe_filter_toggle(button: QAbstractButton, name: str,
                           description: str) -> QAbstractButton:
    """Describe a filter chip so its purpose and state are not colour-only.

    Qt already exposes the checked state to assistive technology; what it
    cannot infer is what checking the chip *does*, which is the part a user
    otherwise has to guess from a highlight.
    """
    return describe(button, name, description=description, tooltip=description)


@contextmanager
def focus_restored_after(initiator: QWidget | None):
    """Restore focus to ``initiator`` after a modal dialog closes.

    Wrap the ``exec()`` call that opens the dialog::

        with a11y.focus_restored_after(self._review_btn):
            accepted = dialog.exec() == QDialog.Accepted

    Focus returns to ``initiator`` only when it is still alive, enabled and
    visible when the block exits; otherwise Qt's own post-modal focus
    behaviour is left alone.  PySide6 does not wrap ``QPointer``, so
    liveness is checked with ``shiboken6.isValid`` before touching the
    widget -- a widget destroyed while the dialog was open still exists as a
    Python object, but calling any method on its already-deleted C++ half
    raises ``RuntimeError``.
    """
    try:
        yield
    finally:
        if (initiator is not None and shiboken6.isValid(initiator)
                and initiator.isEnabled() and initiator.isVisible()):
            initiator.setFocus(Qt.FocusReason.PopupFocusReason)


def sanitize_saved_geometry(value: int | None, *, minimum: int, maximum: int,
                            default: int) -> int:
    """Clamp a persisted logical-pixel size into a usable range.

    A width saved on a larger screen or an older layout can be far too small
    or push a pane off-screen on the one restoring it, so a restored value is
    only honoured when it is still sane for the current layout.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return max(minimum, min(maximum, number))
