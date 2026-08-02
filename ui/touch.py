"""Finger-driven input for a pointer-designed desktop UI.

Why this module exists
----------------------
Qt registers its top-level windows for raw touch input on Windows.  That
registration is what makes multi-touch possible at all, but it also opts the
application *out* of the pan/flick emulation the OS performs for windows that
never claim touch.  The practical consequence is severe and easy to miss on a
development machine with a mouse: every scrollable surface in the application
stops scrolling under a finger.  Nothing throws, nothing is logged — dragging
simply does nothing, and inside an item view it rubber-band selects instead.

Qt's answer is :class:`QScroller`, which must be attached explicitly to each
scrollable viewport.  Doing that at ~40 construction sites by hand guarantees
the next scroll area someone adds will be the one that is forgotten, so the
attachment lives here behind :func:`enable_touch_scroll` and every call site
uses that one function.

The second gap is the *right mouse button*.  A finger does not have one, so
the three ``Qt.CustomContextMenu`` surfaces in the Tag Editor are unreachable
by touch.  :func:`enable_long_press_context_menu` closes that by synthesising
a real ``QContextMenuEvent`` after a stationary hold — see that function for
why synthesising the event beats calling the handler directly.

Scope note: everything here is inert without a finger on the glass.  A mouse
never produces the synthesized-source events the long-press filter accepts,
and ``TouchGesture`` scrollers ignore mouse input by construction.  Enabling
touch support therefore cannot regress the mouse experience, which is why
none of it sits behind a setting.  The one thing that *does* change what a
mouse user sees — enlarging every control to a finger-sized target — is
deliberately kept out of this module and lives behind the ``touch_density``
config flag instead.

DPI note: the pixel constants here are Qt *logical* pixels and must never be
multiplied by ``logicalDotsPerInch()``.  Qt 6 already maps logical geometry
to the physical screen; scaling it again double-scales the control.  This
mirrors the same warning in :mod:`ui.a11y`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSizeF, QTimer, Qt
from PySide6.QtGui import QContextMenuEvent, QInputDevice
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QScroller,
    QScrollerProperties,
    QToolTip,
    QWidget,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TOUCH_TARGET_PX",
    "TOUCH_SCROLLBAR_PX",
    "TOUCH_SPLITTER_HANDLE_PX",
    "has_touch_screen",
    "is_touch_pointer",
    "suspend_touch_scroll",
    "resume_touch_scroll",
    "is_touch_scroll_suspended",
    "set_touch_density",
    "is_touch_density",
    "touch_size",
    "configure_application",
    "apply_touch_support",
    "apply_touch_density_sizes",
    "enable_touch_scroll",
    "enable_pinch_zoom",
    "enable_hold_gesture",
    "enable_long_press_context_menu",
    "enable_long_press_tooltip",
    "enable_long_press",
    "set_touch_override",
]

# ── Sizing tokens ─────────────────────────────────────────────────────────────
# 40 logical px is the smallest target that a finger hits reliably; it is the
# floor shared by the Windows, macOS and Material accessibility guidance.  The
# scrollbar figure is the *grab* width — the painted groove stays thinner via
# a transparent border so widening it does not visually thicken the chrome.
TOUCH_TARGET_PX = 40
TOUCH_SCROLLBAR_PX = 20
TOUCH_SPLITTER_HANDLE_PX = 12

_touch_override: Optional[bool] = None
_touch_density = False


def set_touch_density(enabled: bool) -> None:
    """Set whether controls should be enlarged to finger-sized targets.

    Owned here rather than read from :class:`AppConfig` at each call site so
    that leaf widgets (a queue card, a toolbar button) can consult it without
    importing the config layer, and so tests can flip it without a config
    file.  :class:`ui.theme_manager.ThemeManager` is what keeps it in sync
    with the stored setting and repaints on change.
    """
    global _touch_density
    _touch_density = bool(enabled)


def is_touch_density() -> bool:
    """True when controls should use finger-sized targets."""
    return _touch_density


def touch_size(normal_px: int) -> int:
    """Scale one hard-coded control size for the active density.

    For the handful of controls whose size is fixed in Python rather than in
    QSS — ``setFixedSize`` beats any stylesheet ``min-height``, so the density
    stylesheet cannot reach them.  Sizes already at or above the target are
    left alone instead of being grown further.
    """
    if not _touch_density:
        return normal_px
    return max(normal_px, TOUCH_TARGET_PX)


def set_touch_override(value: Optional[bool]) -> None:
    """Force :func:`has_touch_screen` for tests and for the ``--touch`` flag.

    ``None`` restores real device detection.  Headless test runs
    (``QT_QPA_PLATFORM=offscreen``) report no input devices at all, so
    exercising any touch-conditional branch requires this hook.
    """
    global _touch_override
    _touch_override = value


def has_touch_screen() -> bool:
    """True when this machine has a touch screen attached.

    Not cached: touch screens are hot-pluggable (tablet docks, USB displays),
    and the query is a cheap walk over an in-process device list.
    """
    if _touch_override is not None:
        return _touch_override
    if os.environ.get("BANANAFLOW_FORCE_TOUCH") == "1":
        return True
    try:
        return any(
            device.type() == QInputDevice.DeviceType.TouchScreen
            for device in QInputDevice.devices()
        )
    except (RuntimeError, TypeError):
        # No QGuiApplication yet, or a platform plugin without device
        # enumeration.  Absence of evidence is not touch.
        return False


def configure_application(app: QApplication) -> None:
    """Apply the application-wide input policy touch support depends on.

    Must run after ``QApplication`` construction (these are instance-level
    settings, not the pre-construction ``AA_*`` attributes).
    """
    # Touch events that no widget consumes become mouse events, which is what
    # makes a plain tap press a button.  It is Qt's default, but the whole
    # module is built on the guarantee, so it is stated rather than inherited.
    app.setAttribute(Qt.ApplicationAttribute.AA_SynthesizeMouseForUnhandledTouchEvents, True)

    # Qt's 800 ms default is tuned for a stylus.  650 ms is the Windows touch
    # press-and-hold feel and keeps a long press from feeling like a stall.
    hints = app.styleHints()
    if hints is not None:
        hints.setMousePressAndHoldInterval(650)


# ── Touch scrolling ───────────────────────────────────────────────────────────

def _scroll_target(widget: QWidget) -> QWidget:
    """The widget QScroller must be attached to.

    For a scroll area that is the *viewport*, never the frame: the scroller
    moves the thing the touch lands on, and attaching it to the frame scrolls
    nothing while silently appearing to succeed.
    """
    if isinstance(widget, QAbstractScrollArea):
        return widget.viewport()
    return widget


def _touch_scroller_properties() -> QScrollerProperties:
    """Kinetic tuning that matches what a finger expects elsewhere on Windows.

    Qt's defaults overshoot hard and decelerate slowly, which reads as a
    "slippery" list.  These values keep the flick but stop it in roughly the
    distance a native Windows list would.
    """
    metric = QScrollerProperties.ScrollMetric
    props = QScrollerProperties()
    # Start scrolling almost immediately: a high threshold makes short drags
    # feel dead before the content moves.
    props.setScrollMetric(metric.DragStartDistance, 0.002)
    props.setScrollMetric(metric.DecelerationFactor, 0.35)
    props.setScrollMetric(metric.MaximumVelocity, 0.6)
    # Lock to one axis once the drag commits to a direction. Without it a
    # vertical flick down a wide table also carries the few degrees of
    # sideways drift every real finger has, and the content slides left and
    # right while it scrolls — reported from hardware as "not calm".
    props.setScrollMetric(metric.AxisLockThreshold, 0.8)
    # Rubber-band at the ends, but do not let content be flung past the edge
    # and left there.
    props.setScrollMetric(metric.OvershootDragResistanceFactor, 0.4)
    props.setScrollMetric(metric.OvershootScrollDistanceFactor, 0.4)
    props.setScrollMetric(
        metric.VerticalOvershootPolicy,
        QScrollerProperties.OvershootPolicy.OvershootWhenScrollable,
    )
    props.setScrollMetric(
        metric.HorizontalOvershootPolicy,
        QScrollerProperties.OvershootPolicy.OvershootWhenScrollable,
    )
    return props


class _MirroredScrollFilter(QObject):
    """Corrects the horizontal axis of a kinetic scroll under RTL layout.

    ``QScroller`` talks to a scroll area in "content position" coordinates and
    assumes the left-to-right convention: a larger x means the content has
    advanced further left.  ``QAbstractScrollArea`` maps that position
    straight onto the horizontal scrollbar's value — and under RTL that
    mapping is mirrored.  Measured on a 30-column table:

        LTR   value 0 -> 27 moves column 0 from x=0    to x=-2644  (leftward)
        RTL   value 0 -> 27 moves column 0 from x=256  to x=+2900  (rightward)

    So under RTL a finger dragged left scrolled the content right.  Reported
    from hardware exactly that way: "to move left I have to move right".

    Rather than let ``QAbstractScrollArea`` apply the mapping, this filter
    answers the two scroll events itself and mirrors x about the scrollbar's
    own range.  The mirror is an involution, so the same expression serves
    both directions and there is no second code path to keep in step.

    Vertical scrolling is passed through untouched — only x is mirrored.

    Deliberately holds no state.  A filter that only C++ has a reference to
    can have its Python wrapper collected and rebuilt, and any attribute set
    in ``__init__`` is gone when that happens — which showed up here as an
    ``AttributeError`` raised from inside ``eventFilter``.  Deriving the
    scroll area from the object being filtered removes the failure mode
    instead of guarding against it.
    """

    @staticmethod
    def _area_for(obj: QObject) -> Optional[QAbstractScrollArea]:
        """The scroll area whose viewport is ``obj``, if it still exists."""
        if not isinstance(obj, QWidget) or not shiboken6.isValid(obj):
            return None
        parent = obj.parentWidget()
        if not isinstance(parent, QAbstractScrollArea):
            return None
        return parent if parent.viewport() is obj else None

    @staticmethod
    def _mirror_x(area: QAbstractScrollArea, value: float) -> float:
        bar = area.horizontalScrollBar()
        return float(bar.minimum() + bar.maximum()) - float(value)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() not in (QEvent.Type.ScrollPrepare, QEvent.Type.Scroll):
            return False

        area = self._area_for(obj)
        # Read the direction live: the app mirrors its whole layout when the
        # language changes, and a scroll area outlives that change.
        if area is None or area.layoutDirection() != Qt.LayoutDirection.RightToLeft:
            return False

        hbar = area.horizontalScrollBar()
        vbar = area.verticalScrollBar()

        if event.type() == QEvent.Type.ScrollPrepare:
            event.setViewportSize(QSizeF(obj.size()))
            event.setContentPosRange(
                QRectF(
                    0.0,
                    0.0,
                    float(hbar.maximum() - hbar.minimum()),
                    float(vbar.maximum() - vbar.minimum()),
                )
            )
            event.setContentPos(
                QPointF(self._mirror_x(area, hbar.value()) - hbar.minimum(),
                        float(vbar.value() - vbar.minimum()))
            )
            event.accept()
            return True

        if event.type() == QEvent.Type.Scroll:
            pos = event.contentPos()
            hbar.setValue(int(round(self._mirror_x(area, pos.x() + hbar.minimum()))))
            vbar.setValue(int(round(pos.y() + vbar.minimum())))
            event.accept()
            return True

        return False


def is_touch_pointer(event) -> bool:
    """True when ``event`` came from a finger rather than a real mouse.

    Exposed because a widget with its own press-drag behaviour — a rubber-band
    selection, say — has to tell the two apart itself.  On a touch screen a
    drag belongs to the scroller, and a view that claims it on press wins the
    race and leaves the surface unscrollable.
    """
    return _HoldGestureWatcher._is_touch(event)


#: Tracks whether this module has ungrabbed a target's scroll gesture.
#:
#: ``QScroller.hasScroller()`` cannot answer this: it reports whether a
#: QScroller *object* exists, and that object outlives ``ungrabGesture`` — so
#: using it as the guard made resume a no-op and left the surface permanently
#: unscrollable, which is worse than the bug suspension exists to fix.
_SUSPENDED_PROPERTY = "_bananaflow_scroll_suspended"


def suspend_touch_scroll(widget: Optional[QWidget]) -> None:
    """Hand the touch stream back to ``widget`` for the current gesture.

    For the case where a deliberate gesture means the user wants to drag
    *within* the widget rather than scroll it.  Always pair with
    :func:`resume_touch_scroll` on release — including on the paths where the
    gesture is abandoned.
    """
    if widget is None:
        return
    target = _scroll_target(widget)
    if target is None or bool(target.property(_SUSPENDED_PROPERTY)):
        return
    scroller = QScroller.scroller(target)
    if scroller is not None:
        scroller.stop()
    QScroller.ungrabGesture(target)
    target.setProperty(_SUSPENDED_PROPERTY, True)


def resume_touch_scroll(widget: Optional[QWidget]) -> None:
    """Give the touch stream back to the scroller after a suspension."""
    if widget is None:
        return
    target = _scroll_target(widget)
    if target is None or not bool(target.property(_SUSPENDED_PROPERTY)):
        return
    QScroller.grabGesture(target, QScroller.ScrollerGestureType.TouchGesture)
    scroller = QScroller.scroller(target)
    if scroller is not None:
        scroller.setScrollerProperties(_touch_scroller_properties())
    target.setProperty(_SUSPENDED_PROPERTY, False)


def is_touch_scroll_suspended(widget: Optional[QWidget]) -> bool:
    """Whether :func:`suspend_touch_scroll` currently holds ``widget``'s gesture."""
    if widget is None:
        return False
    target = _scroll_target(widget)
    return target is not None and bool(target.property(_SUSPENDED_PROPERTY))


def enable_touch_scroll(widget: Optional[QWidget]) -> Optional[QWidget]:
    """Make ``widget`` scroll under a finger.  Returns ``widget`` for chaining.

    Safe to call twice on the same widget and safe to call with ``None`` (the
    builders that produce scroll areas sometimes return early), so call sites
    never need a guard.
    """
    if widget is None:
        return None

    target = _scroll_target(widget)
    if target is None:
        return widget

    # An item view scrolling per *item* jumps a whole row at a time, which
    # destroys the 1:1 finger tracking a kinetic scroller depends on.
    #
    # This is the one change here a mouse user can perceive (the wheel scrolls
    # smoothly instead of row-by-row), so it is spent only where it buys
    # something: a machine with no touch screen keeps its existing scroll mode
    # exactly.
    if isinstance(widget, QAbstractItemView) and has_touch_screen():
        widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        widget.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    target.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
    QScroller.grabGesture(target, QScroller.ScrollerGestureType.TouchGesture)
    scroller = QScroller.scroller(target)
    if scroller is not None:
        scroller.setScrollerProperties(_touch_scroller_properties())
    # QScroller delivers its scroll events to the viewport; the mirror filter
    # has to sit there to answer them before QAbstractScrollArea does.
    if isinstance(widget, QAbstractScrollArea):
        target.installEventFilter(_MirroredScrollFilter(target))
    return widget


# ── Pinch to zoom ─────────────────────────────────────────────────────────────

class _PinchZoomFilter(QObject):
    """Routes every way a "zoom" gesture can arrive into one callback.

    There are three, and a surface that handles only one is broken on some
    hardware the user actually has:

    * **Touch screen** — a real two-finger pinch, delivered through Qt's
      gesture framework as ``QPinchGesture`` once the widget grabs it.
    * **Precision touchpad on Windows** — the OS does *not* generally hand Qt
      a pinch here.  It converts the gesture to **Ctrl + wheel** before the
      application sees it, which is the same thing every desktop app has
      always treated as zoom.  Handling Ctrl+wheel is therefore not a mouse
      convenience bolted on: it *is* the touchpad path.
    * **Platform native gesture** — ``ZoomNativeGesture``, which some
      platform plugins send instead.  Cheap to accept, and it is what makes
      this correct on a stack that does deliver it.

    The callback receives an incremental *multiplier* (1.05 = 5% larger), so
    the caller keeps ownership of its own range, rounding and clamping.
    """

    #: One wheel notch. Matches the ±10% the zoom buttons already step by, so
    #: the two ways of zooming do not disagree about how big a step is.
    _WHEEL_FACTOR = 1.1

    def __init__(self, widget: QWidget, on_zoom: Callable[[float], None]) -> None:
        super().__init__(widget)
        self._on_zoom = on_zoom

    def _emit(self, factor: float) -> None:
        # A degenerate factor would collapse or explode the caller's value.
        if factor > 0.0 and abs(factor - 1.0) > 1e-6:
            self._on_zoom(factor)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()

        if kind == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                steps = event.angleDelta().y()
                if steps:
                    self._emit(
                        self._WHEEL_FACTOR if steps > 0 else 1.0 / self._WHEEL_FACTOR
                    )
                # Consumed, or the list would zoom and scroll at the same time.
                return True
            return False

        if kind == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                # value() is an incremental delta around zero, not a scale.
                self._emit(1.0 + float(event.value()))
                return True
            return False

        if kind == QEvent.Type.Gesture:
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None:
                self._emit(float(pinch.scaleFactor()))
                event.accept()
                return True

        return False


def enable_pinch_zoom(
    widget: Optional[QWidget], on_zoom: Callable[[float], None]
) -> Optional[QWidget]:
    """Let ``widget`` be zoomed by pinching, on a touch screen or a touchpad.

    ``on_zoom`` is called with an incremental multiplier.  Attach it to the
    widget that *owns* the zoom, not to whatever is nested inside it.
    """
    if widget is None:
        return None

    zoom_filter = _PinchZoomFilter(widget, on_zoom)
    widget.grabGesture(Qt.GestureType.PinchGesture)
    widget.installEventFilter(zoom_filter)
    # Wheel and native gestures land on the viewport of a scroll area, while
    # the grabbed QPinchGesture is delivered to the widget itself, so both
    # have to be watched.
    viewport = _scroll_target(widget)
    if viewport is not None and viewport is not widget:
        viewport.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        viewport.installEventFilter(zoom_filter)
    return widget


# ── Press and hold ────────────────────────────────────────────────────────────

class _HoldGestureWatcher(QObject):
    """One event filter, shared by every widget in a window, for press-and-hold.

    A finger has no right mouse button and no hover, so a stationary hold has
    to stand in for both: it opens a context menu where the widget defines
    one, and otherwise reads out the tooltip that a mouse user would get by
    hovering.

    Shared rather than per-widget on purpose.  A real window carries upwards
    of a thousand widgets with tooltips, and giving each its own filter object
    and its own ``QTimer`` would allocate a thousand of both during startup —
    a cost paid by every launch, touch screen or not, to serve a gesture that
    can only ever be in flight once.  One watcher, one timer, and the action
    is derived from the widget under the finger at the moment it fires.

    Only *synthesized* mouse events are considered.  A real mouse already has
    a right button, and reacting to a held left button there would fire a
    context menu in the middle of a rubber-band selection — a regression for
    the majority of users to serve the minority.
    """

    #: A context menu that arrives from the OS within this window of one this
    #: watcher synthesised is the *same* user gesture reported twice.  Whether
    #: Windows also turns press-and-hold into ``WM_CONTEXTMENU`` while Qt owns
    #: the touch stream is undocumented and varies by input stack, so it is
    #: treated as possible rather than guessed at in either direction.
    _MENU_DEDUPE_MS = 1500

    #: Movement (logical px) that cancels a pending hold.  Deliberately larger
    #: than ``startDragDistance()`` (4 px): a finger is never as still as a
    #: mouse, and cancelling because the skin rolled 5 px is experienced as
    #: "press and hold only works sometimes".
    _SLOP_PX = 24

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._target: Optional[QWidget] = None
        self._press_pos: Optional[QPoint] = None
        self._swallow_for: Optional[QWidget] = None
        self._last_native_menu_ms = float("-inf")
        self._synthesizing = False

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _is_touch(event) -> bool:
        source = getattr(event, "source", None)
        if source is not None and source() != Qt.MouseEventSource.MouseEventNotSynthesized:
            return True
        device = getattr(event, "pointingDevice", None)
        try:
            return (
                device is not None
                and device() is not None
                and device().type() == QInputDevice.DeviceType.TouchScreen
            )
        except (RuntimeError, AttributeError):
            return False

    @staticmethod
    def _hold_interval() -> int:
        app = QApplication.instance()
        hints = app.styleHints() if app is not None else None
        return hints.mousePressAndHoldInterval() if hints is not None else 650

    @staticmethod
    def _alive(widget: Optional[QWidget]) -> bool:
        return widget is not None and shiboken6.isValid(widget)

    def _cancel(self) -> None:
        self._timer.stop()
        self._target = None
        self._press_pos = None

    # -- gesture resolution ----------------------------------------------------

    @staticmethod
    def _menu_owner(target: QWidget) -> Optional[QWidget]:
        """The widget whose context-menu policy governs ``target``.

        For an item view the filter sits on the *viewport* — that is where the
        mouse events arrive and, as Qt requires, where a context-menu event
        must be delivered — while the policy itself is set on the view. So a
        viewport defers to its scroll area.
        """
        if target.contextMenuPolicy() in _EXPLICIT_MENU_POLICIES:
            return target
        parent = target.parentWidget()
        if parent is not None and parent.contextMenuPolicy() in _EXPLICIT_MENU_POLICIES:
            if isinstance(parent, QAbstractScrollArea) and parent.viewport() is target:
                return parent
        return None

    @staticmethod
    def _hook_owner(target: QWidget) -> QWidget:
        """The widget that may define ``touch_hold`` for ``target``.

        The filter sits on a scroll area's viewport, but the behaviour is
        implemented on the view, so a viewport defers to its scroll area.
        Resolved on its own rather than through the context-menu policy: tying
        the two together meant a widget without a declared menu silently never
        got asked.
        """
        if hasattr(target, "touch_hold"):
            return target
        parent = target.parentWidget()
        if (
            isinstance(parent, QAbstractScrollArea)
            and parent.viewport() is target
            and hasattr(parent, "touch_hold")
        ):
            return parent
        return target

    def _fire(self) -> None:
        target, pos = self._target, self._press_pos
        self._target = None
        self._press_pos = None
        if pos is None or not self._alive(target):
            return

        # A widget may claim the hold for itself. The Tag Editor's table does
        # while a marquee it already started is in progress, so the menu
        # cannot open on top of a selection the user is still drawing.
        hook = getattr(self._hook_owner(target), "touch_hold", None)
        if callable(hook):
            try:
                if hook(pos):
                    return
            except Exception:  # pragma: no cover - a hook must never break input
                logger.warning("[touch] touch_hold hook raised; ignoring", exc_info=True)

        if self._menu_owner(target) is not None:
            self._open_context_menu(target, pos)
            return
        self._show_tooltip(target, pos)

    def _open_context_menu(self, target: QWidget, pos: QPoint) -> None:
        if (time.monotonic() * 1000.0 - self._last_native_menu_ms) < self._MENU_DEDUPE_MS:
            return  # the OS already produced one for this same hold
        self._synthesizing = True
        try:
            # Synthesising the *event* rather than calling a menu builder is
            # what keeps this generic: QWidget already turns QEvent::ContextMenu
            # into customContextMenuRequested for a CustomContextMenu widget,
            # so every existing menu works untouched, and so will the next one.
            QApplication.sendEvent(
                target,
                QContextMenuEvent(
                    QContextMenuEvent.Reason.Other, pos, target.mapToGlobal(pos)
                ),
            )
        finally:
            self._synthesizing = False

    def _show_tooltip(self, target: QWidget, pos: QPoint) -> None:
        tip = (target.toolTip() or "").strip()
        if not tip:
            return
        QToolTip.showText(target.mapToGlobal(pos), tip, target)
        # Without this, holding a button to find out what it does would end by
        # doing it. Hold inspects, tap activates.
        #
        # Bound to the widget rather than kept as a bare flag: this watcher is
        # shared by the whole window, and a flag left set by a finger that
        # wandered off would swallow the next unrelated tap anywhere in it.
        self._swallow_for = target

    # -- QObject ---------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()

        if kind == QEvent.Type.ContextMenu:
            if not self._synthesizing:
                self._last_native_menu_ms = time.monotonic() * 1000.0
            return False

        if kind == QEvent.Type.MouseButtonPress:
            # A new gesture supersedes any unconsumed decision from the last.
            self._swallow_for = None
            if event.button() == Qt.MouseButton.LeftButton and self._is_touch(event):
                self._target = obj if isinstance(obj, QWidget) else None
                self._press_pos = event.position().toPoint()
                self._timer.start(self._hold_interval())
            else:
                self._cancel()
            return False

        if kind == QEvent.Type.MouseMove:
            if self._press_pos is not None:
                moved = (event.position().toPoint() - self._press_pos).manhattanLength()
                if moved > self._SLOP_PX:
                    self._cancel()
            return False

        if kind == QEvent.Type.MouseButtonRelease:
            self._cancel()
            if self._swallow_for is not None and self._swallow_for is obj:
                self._swallow_for = None
                if isinstance(obj, QAbstractButton):
                    # The press left it visually depressed, and consuming the
                    # release means the button never clears that itself.
                    obj.setDown(False)
                return True
            return False

        if kind in (QEvent.Type.TouchCancel, QEvent.Type.Leave):
            self._cancel()
            self._swallow_for = None

        return False


def _watcher_for(widget: QWidget) -> _HoldGestureWatcher:
    """The hold watcher owned by ``widget``'s window, created on first use.

    Parented to the window so it dies with it, and looked up rather than
    stored globally so a dialog cannot outlive or leak into the main window.
    """
    host = widget.window() or widget
    watcher = host.findChild(_HoldGestureWatcher, _WATCHER_NAME, Qt.FindDirectChildrenOnly)
    if watcher is None:
        watcher = _HoldGestureWatcher(host)
        watcher.setObjectName(_WATCHER_NAME)
    return watcher


_WATCHER_NAME = "_bananaflow_hold_watcher"


def enable_long_press(
    widget: Optional[QWidget], callback: Callable[[QPoint], None]
) -> Optional[QWidget]:
    """Call ``callback(pos)`` when a finger rests on ``widget`` without moving.

    For a caller that needs its own reaction to a hold.  The window-wide sweep
    does not use this — it relies on the shared watcher deriving the action
    from the widget — so this stays available without costing anything when
    unused.

    ``pos`` is in the coordinates of the widget the filter watches: the
    viewport for a scroll area, which is what ``indexAt``/``itemAt`` expect.
    """
    if widget is None:
        return None
    target = _scroll_target(widget)
    if target is None:
        return widget
    target.installEventFilter(_SingleLongPressFilter(target, callback))
    return widget


class _SingleLongPressFilter(QObject):
    """A dedicated hold filter for one widget with a bespoke callback."""

    def __init__(self, widget: QWidget, callback: Callable[[QPoint], None]) -> None:
        super().__init__(widget)
        self._callback = callback
        self._press_pos: Optional[QPoint] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)

    def _fire(self) -> None:
        pos, self._press_pos = self._press_pos, None
        if pos is not None:
            self._callback(pos)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            if (
                event.button() == Qt.MouseButton.LeftButton
                and _HoldGestureWatcher._is_touch(event)
            ):
                self._press_pos = event.position().toPoint()
                self._timer.start(_HoldGestureWatcher._hold_interval())
            else:
                self._timer.stop()
                self._press_pos = None
        elif kind == QEvent.Type.MouseMove and self._press_pos is not None:
            moved = (event.position().toPoint() - self._press_pos).manhattanLength()
            if moved > _HoldGestureWatcher._SLOP_PX:
                self._timer.stop()
                self._press_pos = None
        elif kind in (
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.TouchCancel,
            QEvent.Type.Leave,
        ):
            self._timer.stop()
            self._press_pos = None
        return False


def enable_hold_gesture(widget: Optional[QWidget]) -> Optional[QWidget]:
    """Attach the shared press-and-hold watcher to ``widget``.

    What the hold does is decided when it fires: a context menu if the widget
    declares one, otherwise its tooltip.
    """
    if widget is None:
        return None
    target = _scroll_target(widget)
    if target is None:
        return widget
    target.installEventFilter(_watcher_for(target))
    return widget


#: Kept as a name because a widget built after the window-wide sweep (a queue
#: card, say) wires itself and reads better saying what it wants.
enable_long_press_context_menu = enable_hold_gesture
enable_long_press_tooltip = enable_hold_gesture


# ── Whole-window sweep ────────────────────────────────────────────────────────

#: Marks a widget this module has already handled, so a second sweep over the
#: same window (a panel rebuilt in place, a dialog reused) cannot stack a
#: second event filter or a second scroller on it.
_APPLIED_PROPERTY = "_bananaflow_touch_applied"

#: Context-menu policies the *application* opted into deliberately.  Every
#: widget carries ``DefaultContextMenu``, so reacting to that would attach a
#: filter to the entire widget tree for no benefit.
_EXPLICIT_MENU_POLICIES = (
    Qt.ContextMenuPolicy.CustomContextMenu,
    Qt.ContextMenuPolicy.ActionsContextMenu,
)


def _already_applied(widget: QWidget) -> bool:
    if bool(widget.property(_APPLIED_PROPERTY)):
        return True
    widget.setProperty(_APPLIED_PROPERTY, True)
    return False


#: Remembers a control's designed height so turning density off restores the
#: original value rather than a guess.
_ORIGINAL_FIXED_HEIGHT = "_bananaflow_touch_original_h"

#: Only controls the user actually operates are grown.  Frames, separators and
#: header strips also carry fixed heights, and enlarging those would inflate
#: the layout without making a single target easier to hit.
_INTERACTIVE_TYPES: tuple[type, ...] = ()


def _interactive_types() -> tuple[type, ...]:
    global _INTERACTIVE_TYPES
    if not _INTERACTIVE_TYPES:
        from PySide6.QtWidgets import (
            QAbstractButton,
            QAbstractSpinBox,
            QComboBox,
            QLineEdit,
        )

        _INTERACTIVE_TYPES = (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit)
    return _INTERACTIVE_TYPES


def apply_touch_density_sizes(root: Optional[QWidget]) -> None:
    """Grow (or restore) controls whose height is fixed in Python.

    ``setFixedHeight`` pins minimum and maximum together, which no stylesheet
    ``min-height`` can override — so the density stylesheet alone leaves a
    26 px button at 26 px.  Rather than edit every call site that hard-codes a
    height, the designed value is captured once per control and used as the
    baseline, which is also what makes turning the setting back off exact
    instead of approximate.

    Idempotent, and safe to call on every density change.
    """
    if root is None:
        return
    dense = is_touch_density()
    for widget in root.findChildren(QWidget):
        if not isinstance(widget, _interactive_types()):
            continue
        original = widget.property(_ORIGINAL_FIXED_HEIGHT)
        if original is None:
            # Equal bounds are Qt's signature for setFixedHeight. A control
            # that was never pinned is left to the stylesheet.
            if widget.minimumHeight() != widget.maximumHeight():
                continue
            original = widget.minimumHeight()
            widget.setProperty(_ORIGINAL_FIXED_HEIGHT, original)
        widget.setFixedHeight(max(int(original), TOUCH_TARGET_PX) if dense else int(original))


def apply_touch_support(root: Optional[QWidget]) -> None:
    """Make every scrollable and every context menu under ``root`` finger-usable.

    Called once per window rather than once per widget.  The alternative — an
    application-wide event filter that catches widgets as they are polished —
    was rejected on cost: in PySide6 every filtered event crosses into Python,
    and an always-on global filter would tax mouse moves and paints across the
    whole application to serve widget construction, which happens seldom and
    at predictable moments.  A sweep is O(widgets) once, then free forever.

    Idempotent, so a panel that rebuilds itself can call it again safely.
    """
    if root is None:
        return

    for area in root.findChildren(QAbstractScrollArea):
        if not _already_applied(area):
            enable_touch_scroll(area)
    if isinstance(root, QAbstractScrollArea) and not _already_applied(root):
        enable_touch_scroll(root)

    for widget in root.findChildren(QWidget):
        # The scroll pass above may already own this widget; the gesture
        # marker is tracked separately so both can apply to one view.
        if bool(widget.property(_APPLIED_PROPERTY + "_hold")):
            continue
        # One hold, one meaning. A widget that declares a context menu gets
        # the menu; a widget that only has a tooltip gets the tooltip. The
        # shared watcher makes that choice when the gesture fires, so both
        # cases attach the same single filter here.
        wants_hold = (
            widget.contextMenuPolicy() in _EXPLICIT_MENU_POLICIES
            or bool((widget.toolTip() or "").strip())
        )
        if wants_hold:
            widget.setProperty(_APPLIED_PROPERTY + "_hold", True)
            enable_hold_gesture(widget)

    apply_touch_density_sizes(root)
