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
    from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, QPointF, Qt
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


# ── drag must scroll, not select ──────────────────────────────────────────────

class TestTableDragScrolls:
    """The Tag Editor table draws its own marquee on left-press.

    On a touch screen that won the race against the kinetic scroller: dragging
    the table selected rows and there was no way to scroll it at all. Reported
    from real hardware, so these pin the resolution.
    """

    def _table(self):
        from ui.panels.metadata_editor.explorer_view import ExplorerDetailsView

        table = ExplorerDetailsView()
        model = _Grid()
        table.setModel(model)
        table._grid_model = model
        table.resize(400, 300)
        touch.enable_touch_scroll(table)
        touch.enable_hold_gesture(table)
        return table

    def test_a_finger_drag_draws_no_marquee(self, app):
        table = self._table()
        try:
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), QPoint(50, 50)))
            QApplication.sendEvent(table.viewport(), _move(table.viewport(), QPoint(50, 200)))
            # This is the reported bug: the drag selected rows and the table
            # could not be scrolled at all.
            assert table._rubber_dragging is False
            assert table._rubber_rect.isEmpty()
        finally:
            table.deleteLater()

    def test_a_finger_drag_from_the_checkbox_column_also_scrolls(self, app):
        table = self._table()
        try:
            # The checkbox strip is a separate press path, and it is exactly
            # where a thumb lands on the leading edge while scrolling.
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), QPoint(6, 50)))
            QApplication.sendEvent(table.viewport(), _move(table.viewport(), QPoint(6, 200)))
            assert table._rubber_dragging is False
            assert table._rubber_rect.isEmpty()
        finally:
            table.deleteLater()

    def test_a_mouse_drag_still_draws_a_marquee(self, app):
        table = self._table()
        try:
            table._is_empty_viewport_area = lambda pos: True
            QApplication.sendEvent(
                table.viewport(), _press(table.viewport(), QPoint(50, 50), finger=False)
            )
            assert table._rubber_active is True
            QApplication.sendEvent(
                table.viewport(),
                QMouseEvent(
                    QEvent.Type.MouseMove,
                    QPointF(QPoint(50, 200)),
                    QPointF(table.viewport().mapToGlobal(QPoint(50, 200))),
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                    _mouse(),
                ),
            )
            # The mouse behaviour must be untouched.
            assert table._rubber_dragging is True
        finally:
            table.deleteLater()

def _ctrl_wheel(widget, notches: int):
    from PySide6.QtGui import QWheelEvent

    return QWheelEvent(
        QPointF(QPoint(20, 20)),
        QPointF(widget.mapToGlobal(QPoint(20, 20))),
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def _plain_wheel(widget, notches: int):
    from PySide6.QtGui import QWheelEvent

    return QWheelEvent(
        QPointF(QPoint(20, 20)),
        QPointF(widget.mapToGlobal(QPoint(20, 20))),
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


class TestPinchZoom:
    def _zoomable(self):
        from PySide6.QtWidgets import QTableView

        view = QTableView()
        view.resize(300, 300)
        factors: list = []
        touch.enable_pinch_zoom(view, factors.append)
        return view, factors

    def test_ctrl_wheel_up_zooms_in(self, app):
        view, factors = self._zoomable()
        # On Windows a precision-touchpad pinch reaches the app as Ctrl+wheel,
        # so this *is* the touchpad path, not a mouse-only convenience.
        QApplication.sendEvent(view.viewport(), _ctrl_wheel(view.viewport(), 1))
        assert factors and factors[0] > 1.0

    def test_ctrl_wheel_down_zooms_out(self, app):
        view, factors = self._zoomable()
        QApplication.sendEvent(view.viewport(), _ctrl_wheel(view.viewport(), -1))
        assert factors and 0.0 < factors[0] < 1.0

    def test_a_plain_wheel_still_scrolls(self, app):
        view, factors = self._zoomable()
        event = _plain_wheel(view.viewport(), 1)
        QApplication.sendEvent(view.viewport(), event)
        # Without Ctrl the wheel must reach the list unchanged.
        assert factors == []

    def test_native_zoom_gesture_is_honoured(self, app):
        from PySide6.QtGui import QNativeGestureEvent

        view, factors = self._zoomable()
        event = QNativeGestureEvent(
            Qt.NativeGestureType.ZoomNativeGesture,
            _finger(),
            2,
            QPointF(QPoint(20, 20)),
            QPointF(QPoint(20, 20)),
            QPointF(view.viewport().mapToGlobal(QPoint(20, 20))),
            0.25,
            QPointF(0, 0),
        )
        QApplication.sendEvent(view.viewport(), event)
        # value() is a delta around zero, not a scale factor.
        assert factors and abs(factors[0] - 1.25) < 1e-6

    def test_a_degenerate_factor_is_dropped(self, app):
        view, factors = self._zoomable()
        zoom_filter = view.findChild(touch._PinchZoomFilter)
        zoom_filter._emit(1.0)
        zoom_filter._emit(0.0)
        zoom_filter._emit(-2.0)
        # A zero or negative multiplier would collapse the caller's value.
        assert factors == []


class TestTagEditorPinchZoom:
    def _panel(self):
        from config import AppConfig
        from ui.theme_manager import ThemeManager
        from ui.panels.metadata_editor.panel import MetadataEditorPanel

        cfg = AppConfig()
        ThemeManager(cfg)
        return MetadataEditorPanel(config=cfg)

    def test_small_increments_accumulate(self, app):
        panel = self._panel()
        try:
            panel._set_zoom(100)
            # Rounding each step to the integer percent the control uses
            # would quantise every increment to zero, and the whole gesture
            # would do nothing at all.
            for _ in range(6):
                panel._on_pinch_zoom(1.02)
            assert panel._zoom_level > 100
        finally:
            panel.deleteLater()

    def test_clamped_to_the_control_range(self, app):
        panel = self._panel()
        try:
            panel._set_zoom(100)
            for _ in range(200):
                panel._on_pinch_zoom(1.05)
            assert panel._zoom_level == 200
            for _ in range(400):
                panel._on_pinch_zoom(1 / 1.05)
            assert panel._zoom_level == 50
        finally:
            panel.deleteLater()

    def test_the_config_write_is_deferred(self, app):
        panel = self._panel()
        try:
            panel._set_zoom(100)
            panel._on_pinch_zoom(1.3)
            # A pinch crosses many integer steps; each one writing config.json
            # would be a separate disk write mid-gesture.
            assert panel._zoom_persist_timer.isActive()
        finally:
            panel.deleteLater()


# ── a scroll must not also be a tap, and must not drift or invert ─────────────

class _Grid(__import__("PySide6.QtCore", fromlist=["QAbstractTableModel"]).QAbstractTableModel):
    """Enough rows and columns for both scrollbars to have a range."""

    def rowCount(self, parent=None):
        return 60

    def columnCount(self, parent=None):
        return 30

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return f"r{index.row()}c{index.column()}"
        return None


class TestScrollIsNotATap:
    """A drag down the file table also marked the row it started from."""

    def _table(self):
        from ui.panels.metadata_editor.explorer_view import ExplorerDetailsView

        table = ExplorerDetailsView()
        model = _Grid()
        table.setModel(model)
        table._grid_model = model  # keep it alive for the view's lifetime
        table.resize(400, 300)
        table.show()
        touch.enable_touch_scroll(table)
        return table

    def test_a_finger_press_selects_nothing_yet(self, app):
        table = self._table()
        try:
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), QPoint(120, 60)))
            # Selecting here marked whichever row the scroll started from.
            assert not table.selectionModel().selectedRows()
        finally:
            table.deleteLater()

    def test_a_tap_still_selects_on_release(self, app):
        table = self._table()
        try:
            spot = QPoint(120, 60)
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), spot))
            QApplication.sendEvent(table.viewport(), _release(table.viewport(), spot))
            rows = table.selectionModel().selectedRows()
            assert len(rows) == 1
            assert rows[0].row() == table.indexAt(spot).row()
        finally:
            table.deleteLater()

    def test_a_scroll_selects_nothing_at_all(self, app):
        table = self._table()
        try:
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), QPoint(120, 60)))
            QApplication.sendEvent(table.viewport(), _release(table.viewport(), QPoint(120, 230)))
            # The finger travelled: this was a scroll, and a scroll chooses
            # nothing it passed over.
            assert not table.selectionModel().selectedRows()
        finally:
            table.deleteLater()

    def test_a_scroll_from_the_checkbox_column_ticks_no_box(self, app):
        table = self._table()
        try:
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), QPoint(6, 60)))
            QApplication.sendEvent(table.viewport(), _release(table.viewport(), QPoint(6, 230)))
            # Same trap one column over: the deferred checkbox toggle has to
            # be dropped by the same travel test.
            assert table._pending_cb_row == -1
            assert not table.selectionModel().selectedRows()
        finally:
            table.deleteLater()

    def test_a_small_wobble_still_counts_as_a_tap(self, app):
        table = self._table()
        try:
            QApplication.sendEvent(table.viewport(), _press(table.viewport(), QPoint(120, 60)))
            QApplication.sendEvent(table.viewport(), _release(table.viewport(), QPoint(124, 63)))
            # No finger lifts on the pixel it landed on.
            assert len(table.selectionModel().selectedRows()) == 1
        finally:
            table.deleteLater()


class TestRtlScrollDirection:
    """Under RTL a finger dragged left scrolled the content right."""

    def _table(self, direction):
        from PySide6.QtWidgets import QTableView

        table = QTableView()
        table.setLayoutDirection(direction)
        model = _Grid()
        table.setModel(model)
        table._grid_model = model
        table.resize(400, 300)
        table.show()
        touch.enable_touch_scroll(table)
        return table

    def _drag_left_by(self, table, amount):
        """Return column 0's x before and after QScroller advances the content."""
        from PySide6.QtGui import QScrollEvent, QScrollPrepareEvent

        bar = table.horizontalScrollBar()
        bar.setValue(bar.maximum() // 2)
        before = table.columnViewportPosition(0)

        prepare = QScrollPrepareEvent(QPointF(0, 0))
        QApplication.sendEvent(table.viewport(), prepare)
        content = prepare.contentPos()
        QApplication.sendEvent(
            table.viewport(),
            QScrollEvent(
                QPointF(content.x() + amount, content.y()),
                QPointF(0, 0),
                QScrollEvent.ScrollState.ScrollUpdated,
            ),
        )
        return before, table.columnViewportPosition(0)

    def test_ltr_content_follows_the_finger(self, app):
        table = self._table(Qt.LayoutDirection.LeftToRight)
        try:
            before, after = self._drag_left_by(table, 5)
            assert after < before
        finally:
            table.deleteLater()

    def test_rtl_content_follows_the_finger(self, app):
        table = self._table(Qt.LayoutDirection.RightToLeft)
        try:
            before, after = self._drag_left_by(table, 5)
            # This is the regression: RTL used to move the content right.
            assert after < before
        finally:
            table.deleteLater()

    def test_rtl_vertical_is_left_alone(self, app):
        from PySide6.QtGui import QScrollEvent, QScrollPrepareEvent

        table = self._table(Qt.LayoutDirection.RightToLeft)
        try:
            vbar = table.verticalScrollBar()
            vbar.setValue(0)
            prepare = QScrollPrepareEvent(QPointF(0, 0))
            QApplication.sendEvent(table.viewport(), prepare)
            content = prepare.contentPos()
            QApplication.sendEvent(
                table.viewport(),
                QScrollEvent(
                    QPointF(content.x(), content.y() + 7),
                    QPointF(0, 0),
                    QScrollEvent.ScrollState.ScrollUpdated,
                ),
            )
            # Only x is mirrored; y must pass through untouched.
            assert vbar.value() == 7
        finally:
            table.deleteLater()


class TestAxisLock:
    def test_the_scroller_locks_to_one_axis(self, app):
        from PySide6.QtWidgets import QScroller, QScrollerProperties

        area = QScrollArea()
        touch.enable_touch_scroll(area)
        props = QScroller.scroller(area.viewport()).scrollerProperties()
        threshold = props.scrollMetric(
            QScrollerProperties.ScrollMetric.AxisLockThreshold
        )
        # At 0 (Qt's default) a vertical flick carries every degree of
        # sideways drift with it and the content slides about while scrolling.
        assert threshold > 0.5


# ── selecting several rows with a finger ──────────────────────────────────────

class TestCrossSlideSelection:
    """Windows' own gesture for picking several items with a finger.

    Three earlier attempts here invented a gesture and hung it on a small
    target — a 24 px checkbox column, a 17 px gutter — so missing by a
    millimetre silently did something else. Microsoft's guidance for a list
    that pans in one direction is a short sideways flick, with the whole row
    as the target:

        "A vertically panning one-dimensional list. Drag horizontally to
         select or move an item."
        "a small threshold of 2.7mm (approximately 10 pixels at target
         resolution) must be crossed before either a select or drag
         interaction is activated."
        "cross-slide selection is constrained to a 90 degree threshold area"
    """

    def _table(self):
        from ui.panels.metadata_editor.explorer_view import ExplorerDetailsView

        table = ExplorerDetailsView()
        model = _Grid()
        table.setModel(model)
        table._grid_model = model
        table.resize(500, 400)
        table.show()
        touch.enable_touch_scroll(table)
        return table

    def _row_pos(self, table, row, x=200):
        return QPoint(x, table.rowViewportPosition(row) + 4)

    def _flick(self, table, row, dx, dy=0):
        start = self._row_pos(table, row)
        end = QPoint(start.x() + dx, start.y() + dy)
        QApplication.sendEvent(table.viewport(), _press(table.viewport(), start))
        QApplication.sendEvent(table.viewport(), _release(table.viewport(), end))

    def _rows(self, table):
        return sorted(i.row() for i in table.selectionModel().selectedRows())

    def test_a_sideways_flick_selects_the_row(self, app):
        table = self._table()
        try:
            self._flick(table, 3, dx=40)
            assert self._rows(table) == [3]
        finally:
            table.deleteLater()

    def test_flicking_several_rows_selects_all_of_them(self, app):
        table = self._table()
        try:
            # The whole point: no mode to enter, no strip to find. Flick each
            # row you want.
            for row in (2, 5, 9):
                self._flick(table, row, dx=40)
            assert self._rows(table) == [2, 5, 9]
        finally:
            table.deleteLater()

    def test_it_works_in_either_direction(self, app):
        table = self._table()
        try:
            self._flick(table, 4, dx=-40)
            assert self._rows(table) == [4]
        finally:
            table.deleteLater()

    def test_flicking_a_selected_row_again_deselects_it(self, app):
        table = self._table()
        try:
            self._flick(table, 3, dx=40)
            self._flick(table, 3, dx=40)
            assert self._rows(table) == []
        finally:
            table.deleteLater()

    def test_below_the_activation_threshold_it_is_a_tap(self, app):
        table = self._table()
        try:
            # Under Microsoft's 10 px the gesture must stay a tap, or the
            # list could not be tapped at all without wobble selecting rows.
            self._flick(table, 3, dx=6)
            assert self._rows(table) == [3]  # tap selects, it does not toggle
            self._flick(table, 3, dx=6)
            assert self._rows(table) == [3]  # ...and taps do not deselect
        finally:
            table.deleteLater()

    def test_a_vertical_drag_selects_nothing(self, app):
        table = self._table()
        try:
            # Outside the 90 degree cone: this is a scroll.
            self._flick(table, 3, dx=0, dy=120)
            assert self._rows(table) == []
        finally:
            table.deleteLater()

    def test_a_diagonal_drag_that_is_mostly_vertical_selects_nothing(self, app):
        table = self._table()
        try:
            self._flick(table, 3, dx=30, dy=90)
            assert self._rows(table) == []
        finally:
            table.deleteLater()

    def test_a_long_sideways_drag_is_horizontal_panning(self, app):
        table = self._table()
        try:
            # Microsoft assumes a list that pans one way only; this table
            # also pans sideways, so distance separates the two.
            self._flick(table, 3, dx=300)
            assert self._rows(table) == []
        finally:
            table.deleteLater()

    def test_a_mouse_drag_is_not_a_cross_slide(self, app):
        table = self._table()
        try:
            start = self._row_pos(table, 3)
            QApplication.sendEvent(
                table.viewport(), _press(table.viewport(), start, finger=False)
            )
            assert table._apply_cross_slide(
                _release(table.viewport(), QPoint(start.x() + 40, start.y()))
            ) is False
        finally:
            table.deleteLater()


