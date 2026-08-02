"""
tests/test_touch_accessibility.py  –  finger-driven input guards
=====================================================================
Pins the behaviour that makes the UI usable without a mouse:

* every scrollable surface carries a kinetic scroller,
* a stationary hold reaches the context menu a finger has no button for,
* a hold on a tooltip-only control reads the tooltip instead of activating it,
* the touch-density setting is exactly reversible,
* capabilities that a finger cannot drag to are reachable another way.

These are all regressions that are invisible on a development machine with a
mouse — nothing raises, the gesture simply does nothing — so they are worth
pinning rather than eyeballing.

Headless (QT_QPA_PLATFORM=offscreen); skips when PySide6 is missing.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QContextMenuEvent, QInputDevice, QMouseEvent, QPointingDevice
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QPushButton,
        QScrollArea,
        QScroller,
        QTreeWidget,
        QTreeWidgetItem,
        QWidget,
    )

    from ui import touch
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 / qfluentwidgets not available", allow_module_level=True)


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    touch.configure_application(instance)
    return instance


@pytest.fixture
def as_touch_device():
    """Report a touch screen for the duration of one test."""
    touch.set_touch_override(True)
    yield
    touch.set_touch_override(None)


@pytest.fixture
def dense():
    touch.set_touch_density(True)
    yield
    touch.set_touch_density(False)


# ── helpers ───────────────────────────────────────────────────────────────────

_FINGER = None
_MOUSE = None


def _finger() -> QPointingDevice:
    """A device that reports itself as a touch screen.

    The production filter only reacts to input that did not come from a real
    mouse, so a test that presses with the default device is testing nothing.
    """
    global _FINGER
    if _FINGER is None:
        _FINGER = QPointingDevice(
            "test finger",
            4242,
            QInputDevice.DeviceType.TouchScreen,
            QPointingDevice.PointerType.Finger,
            QInputDevice.Capability.Position,
            5,
            0,
        )
    return _FINGER


def _mouse() -> QPointingDevice:
    """A genuine mouse. Must be a real device — Qt dereferences it."""
    global _MOUSE
    if _MOUSE is None:
        _MOUSE = QPointingDevice(
            "test mouse",
            4243,
            QInputDevice.DeviceType.Mouse,
            QPointingDevice.PointerType.Generic,
            QInputDevice.Capability.Position,
            1,
            3,
        )
    return _MOUSE


def _press(widget: QWidget, pos: QPoint, *, finger: bool = True) -> QMouseEvent:
    device = _finger() if finger else _mouse()
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        device,
    )


def _move(widget: QWidget, pos: QPoint) -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _finger(),
    )


def _release(widget: QWidget, pos: QPoint) -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        _finger(),
    )


def _watcher_of(widget: QWidget) -> touch._HoldGestureWatcher:
    host = widget.window() or widget
    return host.findChild(touch._HoldGestureWatcher)


def _hold(widget: QWidget, target: QWidget, pos: QPoint, *, finger: bool = True) -> None:
    """Press and let the hold mature, without waiting out a real timer."""
    watcher = _watcher_of(widget)
    assert watcher is not None, "widget was never given the hold gesture"
    QApplication.sendEvent(target, _press(target, pos, finger=finger))
    watcher._timer.stop()
    watcher._fire()


# ── scrolling ─────────────────────────────────────────────────────────────────

class TestTouchScroll:
    def test_scroll_area_gets_a_scroller_on_its_viewport(self, app):
        area = QScrollArea()
        touch.enable_touch_scroll(area)
        # The scroller must sit on the viewport: attached to the frame it
        # silently scrolls nothing.
        assert QScroller.hasScroller(area.viewport())

    def test_item_view_scrolls_per_pixel_on_a_touch_machine(self, app, as_touch_device):
        tree = QTreeWidget()
        touch.enable_touch_scroll(tree)
        assert tree.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel

    def test_item_view_scroll_mode_untouched_without_a_touch_screen(self, app):
        touch.set_touch_override(False)
        try:
            tree = QTreeWidget()
            before = tree.verticalScrollMode()
            touch.enable_touch_scroll(tree)
            # A mouse-only machine must not have its wheel behaviour changed.
            assert tree.verticalScrollMode() == before
        finally:
            touch.set_touch_override(None)

    def test_enable_is_none_safe_and_idempotent(self, app):
        assert touch.enable_touch_scroll(None) is None
        area = QScrollArea()
        touch.enable_touch_scroll(area)
        touch.enable_touch_scroll(area)
        assert QScroller.hasScroller(area.viewport())


# ── press and hold → context menu ─────────────────────────────────────────────

def _tree_with_menu() -> tuple[QTreeWidget, list]:
    tree = QTreeWidget()
    tree.resize(300, 300)
    tree.addTopLevelItem(QTreeWidgetItem(["one"]))
    tree.addTopLevelItem(QTreeWidgetItem(["two"]))
    tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    received: list = []
    tree.customContextMenuRequested.connect(received.append)
    touch.enable_hold_gesture(tree)
    return tree, received


class TestHoldOpensContextMenu:
    def test_hold_reaches_the_existing_custom_menu(self, app):
        tree, received = _tree_with_menu()
        _hold(tree, tree.viewport(), QPoint(30, 30))
        assert received == [QPoint(30, 30)]

    def test_position_is_in_viewport_coordinates(self, app):
        """The menu handlers call indexAt(pos) — viewport coordinates or bust."""
        tree, received = _tree_with_menu()
        _hold(tree, tree.viewport(), QPoint(10, 12))
        assert tree.indexAt(received[0]).isValid()

    def test_a_real_mouse_press_is_ignored(self, app):
        tree, received = _tree_with_menu()
        watcher = _watcher_of(tree)
        QApplication.sendEvent(tree.viewport(), _press(tree.viewport(), QPoint(30, 30), finger=False))
        # A mouse already has a right button; holding its left one must not
        # pop a menu in the middle of a rubber-band selection.
        assert not watcher._timer.isActive()

    def test_movement_beyond_the_slop_cancels_the_hold(self, app):
        tree, received = _tree_with_menu()
        watcher = _watcher_of(tree)
        QApplication.sendEvent(tree.viewport(), _press(tree.viewport(), QPoint(30, 30)))
        QApplication.sendEvent(tree.viewport(), _move(tree.viewport(), QPoint(30, 120)))
        assert not watcher._timer.isActive()
        watcher._fire()
        assert received == [], "a scroll flick must not open a menu"

    def test_small_wobble_does_not_cancel(self, app):
        tree, received = _tree_with_menu()
        QApplication.sendEvent(tree.viewport(), _press(tree.viewport(), QPoint(30, 30)))
        QApplication.sendEvent(tree.viewport(), _move(tree.viewport(), QPoint(33, 32)))
        _watcher_of(tree)._fire()
        # A finger is never perfectly still; a few pixels must still count.
        assert received == [QPoint(30, 30)]

    def test_a_native_menu_suppresses_the_synthesized_one(self, app):
        tree, received = _tree_with_menu()
        viewport = tree.viewport()
        # Windows may or may not turn press-and-hold into WM_CONTEXTMENU while
        # Qt owns the touch stream. If it does, the user must still get one
        # menu, not two.
        QApplication.sendEvent(
            viewport,
            QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, QPoint(30, 30), viewport.mapToGlobal(QPoint(30, 30))
            ),
        )
        native_count = len(received)
        _hold(tree, viewport, QPoint(30, 30))
        assert len(received) == native_count, "the same hold produced two menus"


# ── press and hold → tooltip ──────────────────────────────────────────────────

class TestHoldReadsTooltip:
    def test_hold_shows_the_tooltip_of_an_icon_only_control(self, app):
        button = QPushButton()
        button.setToolTip("Remove from queue")
        touch.enable_hold_gesture(button)
        _hold(button, button, QPoint(5, 5))
        from PySide6.QtWidgets import QToolTip

        assert QToolTip.text() == "Remove from queue"

    def test_hold_swallows_the_activation_that_would_follow(self, app):
        button = QPushButton()
        button.setToolTip("Delete everything")
        clicks: list = []
        button.clicked.connect(lambda: clicks.append(1))
        touch.enable_hold_gesture(button)

        _hold(button, button, QPoint(5, 5))
        QApplication.sendEvent(button, _release(button, QPoint(5, 5)))
        # Holding a button to find out what it does must not end by doing it.
        assert clicks == []

    def test_a_stale_hold_does_not_swallow_an_unrelated_tap(self, app):
        window = QWidget()
        held = QPushButton(window)
        held.setToolTip("held")
        other = QPushButton(window)
        other.setToolTip("other")
        clicks: list = []
        other.clicked.connect(lambda: clicks.append(1))
        touch.apply_touch_support(window)

        # Hold one control, then wander off it without releasing. The watcher
        # is shared by the whole window, so a decision left behind here would
        # eat the next tap anywhere in it.
        _hold(held, held, QPoint(5, 5))
        QApplication.sendEvent(held, QEvent(QEvent.Type.Leave))

        QApplication.sendEvent(other, _press(other, QPoint(5, 5)))
        QApplication.sendEvent(other, _release(other, QPoint(5, 5)))
        assert clicks == [1]

    def test_a_plain_tap_still_activates(self, app):
        button = QPushButton()
        button.setToolTip("Go")
        clicks: list = []
        button.clicked.connect(lambda: clicks.append(1))
        touch.enable_hold_gesture(button)

        # No hold in between: press, release, done.
        QApplication.sendEvent(button, _press(button, QPoint(5, 5)))
        QApplication.sendEvent(button, _release(button, QPoint(5, 5)))
        assert clicks == [1], "a tap must still reach the button"


# ── the window-wide sweep ─────────────────────────────────────────────────────

class TestSweep:
    def _window(self) -> QWidget:
        window = QWidget()
        area = QScrollArea(window)
        tree = QTreeWidget(window)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button = QPushButton(window)
        button.setToolTip("something")
        plain = QPushButton(window)
        return window

    def test_sweep_reaches_scroll_areas_and_hold_targets(self, app):
        window = self._window()
        touch.apply_touch_support(window)
        for area in window.findChildren(QScrollArea):
            assert QScroller.hasScroller(area.viewport())
        held = [
            w for w in window.findChildren(QWidget)
            if w.property("_bananaflow_touch_applied_hold")
        ]
        assert len(held) == 2  # the tree (menu) and the tooltip button

    def test_a_control_with_neither_is_left_alone(self, app):
        window = self._window()
        touch.apply_touch_support(window)
        plain = [
            b for b in window.findChildren(QPushButton)
            if not (b.toolTip() or "").strip()
        ]
        assert plain and not any(
            b.property("_bananaflow_touch_applied_hold") for b in plain
        )

    def test_sweep_is_idempotent(self, app):
        window = self._window()
        touch.apply_touch_support(window)
        first = len(window.findChildren(touch._HoldGestureWatcher))
        touch.apply_touch_support(window)
        # Re-sweeping a rebuilt panel must not stack a second watcher.
        assert len(window.findChildren(touch._HoldGestureWatcher)) == first

    def test_one_watcher_serves_the_whole_window(self, app):
        window = self._window()
        touch.apply_touch_support(window)
        # The alternative — a filter and a timer per widget — allocated one
        # thousand of each during startup in the real window.
        assert len(window.findChildren(touch._HoldGestureWatcher)) == 1

    def test_none_root_is_safe(self, app):
        touch.apply_touch_support(None)
        touch.apply_touch_density_sizes(None)


# ── touch density ─────────────────────────────────────────────────────────────

class TestTouchDensity:
    def test_touch_size_grows_only_when_dense(self, app):
        assert touch.touch_size(26) == 26
        touch.set_touch_density(True)
        try:
            assert touch.touch_size(26) == touch.TOUCH_TARGET_PX
            # A control already above the target is not inflated further.
            assert touch.touch_size(64) == 64
        finally:
            touch.set_touch_density(False)

    def test_fixed_heights_grow_and_restore_exactly(self, app):
        window = QWidget()
        button = QPushButton(window)
        button.setFixedHeight(26)

        touch.set_touch_density(True)
        try:
            touch.apply_touch_density_sizes(window)
            assert button.height() == touch.TOUCH_TARGET_PX
        finally:
            touch.set_touch_density(False)

        touch.apply_touch_density_sizes(window)
        # Restoring from the captured original, not from a guess.
        assert button.minimumHeight() == 26
        assert button.maximumHeight() == 26

    def test_unpinned_controls_are_left_to_the_stylesheet(self, app):
        window = QWidget()
        button = QPushButton(window)
        before_min, before_max = button.minimumHeight(), button.maximumHeight()
        touch.set_touch_density(True)
        try:
            touch.apply_touch_density_sizes(window)
            assert button.minimumHeight() == before_min
            assert button.maximumHeight() == before_max
        finally:
            touch.set_touch_density(False)

    def test_non_interactive_widgets_are_not_inflated(self, app):
        window = QWidget()
        separator = QWidget(window)
        separator.setFixedHeight(1)
        touch.set_touch_density(True)
        try:
            touch.apply_touch_density_sizes(window)
            # Growing a 1 px divider would inflate the layout without making a
            # single target easier to hit.
            assert separator.height() == 1
        finally:
            touch.set_touch_density(False)


# ── controls that were unreachable without a pointer ──────────────────────────

class TestTrackCardRemoveButton:
    """The queue's remove button was hover-only, i.e. absent on a touch screen."""

    def _card(self):
        from ui.components.track_card import TrackCard

        return TrackCard(title="Song", artist="Artist")

    def test_visible_without_hover_on_a_touch_machine(self, app, as_touch_device):
        card = self._card()
        try:
            # A finger produces no enterEvent, so waiting for hover made the
            # entry permanently unremovable.
            assert card._remove_btn.isVisible() or card._remove_btn_should_show()
        finally:
            card.deleteLater()

    def test_hidden_once_the_download_leaves_the_queue(self, app, as_touch_device):
        card = self._card()
        try:
            card.set_status("downloading")
            assert not card._remove_btn_should_show()
        finally:
            card.deleteLater()

    def test_still_hover_gated_without_a_touch_screen(self, app):
        touch.set_touch_override(False)
        card = self._card()
        try:
            assert not card._remove_btn_should_show()
        finally:
            card.deleteLater()
            touch.set_touch_override(None)

    def test_card_offers_a_context_menu(self, app):
        card = self._card()
        try:
            # Reordering is drag-only; a finger cannot drag, so the menu is
            # the path to it.
            assert card.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
        finally:
            card.deleteLater()


class TestQueueStepReorder:
    """Stepping a card, for input that cannot drag."""

    def _panel_with_three(self):
        from ui.panels.queue_panel import QueuePanel

        panel = QueuePanel()
        for i in range(3):
            panel.add_card(index=i, title=f"track-{i}")
        return panel

    def _order(self, panel):
        return [c.queue_index for c in panel._cards]

    def test_move_up(self, app):
        panel = self._panel_with_three()
        try:
            panel._on_card_move(1, -1)
            assert self._order(panel) == [1, 0, 2]
        finally:
            panel.deleteLater()

    def test_move_down_actually_moves(self, app):
        panel = self._panel_with_three()
        try:
            panel._on_card_move(1, 1)
            # The underlying reorder only inserts *before* its target, so the
            # obvious call is a no-op. This pins the direction handling.
            assert self._order(panel) == [0, 2, 1]
        finally:
            panel.deleteLater()

    def test_edges_are_no_ops(self, app):
        panel = self._panel_with_three()
        try:
            panel._on_card_move(0, -1)
            panel._on_card_move(2, 1)
            assert self._order(panel) == [0, 1, 2]
        finally:
            panel.deleteLater()

    def test_unknown_card_is_ignored(self, app):
        panel = self._panel_with_three()
        try:
            panel._on_card_move(99, 1)
            assert self._order(panel) == [0, 1, 2]
        finally:
            panel.deleteLater()


class TestDensityStylesheet:
    def test_density_qss_is_geometry_only(self, app):
        from ui.theme_manager import _build_touch_density_qss

        qss = _build_touch_density_qss()
        # It has to stay valid for dark, light and high-contrast alike, which
        # it only does by carrying no colour.
        assert "color:" not in qss
        assert "background" not in qss
        assert f"min-height: {touch.TOUCH_TARGET_PX}px" in qss
        assert f"width: {touch.TOUCH_SCROLLBAR_PX}px" in qss
