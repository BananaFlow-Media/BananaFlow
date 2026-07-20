from __future__ import annotations

import os
from dataclasses import dataclass

import pytest


@pytest.fixture()
def explorer_view_qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QStandardItem, QStandardItemModel
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QAbstractItemView
        from ui.panels.metadata_editor.explorer_view import ExplorerDetailsView
    except ImportError:
        pytest.skip("PySide6 not available in this environment")

    @dataclass
    class Item:
        name: str

    class TrackModel(QStandardItemModel):
        def __init__(self):
            super().__init__(3, 2)
            self.items = [Item(f"track-{i}") for i in range(3)]
            for row in range(3):
                for col in range(2):
                    self.setItem(row, col, QStandardItem(f"{row}:{col}"))

        def track_at_row(self, row: int):
            return self.items[row] if 0 <= row < len(self.items) else None

    app = QApplication.instance() or QApplication([])
    view = ExplorerDetailsView()
    view.setModel(TrackModel())
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection)
    view.resize(320, 160)
    view.show()
    app.processEvents()
    yield app, QTest, Qt, view
    view.close()
    view.deleteLater()


def test_ctrl_a_selects_all_visible_rows(explorer_view_qt):
    app, qtest, qt, view = explorer_view_qt

    view.setFocus()
    qtest.keyClick(view, qt.Key_A, qt.ControlModifier)
    app.processEvents()

    assert len(view.selectionModel().selectedRows()) == view.model().rowCount()


def test_escape_clears_selection_and_current_index(explorer_view_qt):
    app, qtest, qt, view = explorer_view_qt

    idx = view.model().index(1, 0)
    view.selectAll()
    view.setCurrentIndex(idx)
    assert view.selectionModel().selectedRows()

    view.setFocus()
    qtest.keyClick(view, qt.Key_Escape)
    app.processEvents()

    assert view.selectionModel().selectedRows() == []
    assert not view.currentIndex().isValid()


def test_selected_items_changed_emits_model_items(explorer_view_qt):
    app, _qtest, _qt, view = explorer_view_qt
    seen = []
    view.selectedItemsChanged.connect(seen.append)

    view.selectRow(2)
    app.processEvents()

    assert seen
    assert [item.name for item in seen[-1]] == ["track-2"]


def test_enter_emits_open_requested_for_selection(explorer_view_qt):
    app, qtest, qt, view = explorer_view_qt
    seen = []
    view.openRequested.connect(seen.append)

    view.selectRow(1)
    view.setFocus()
    qtest.keyClick(view, qt.Key_Return)
    app.processEvents()

    assert seen
    assert [item.name for item in seen[-1]] == ["track-1"]


def test_f2_emits_physical_rename_request(explorer_view_qt):
    app, qtest, qt, view = explorer_view_qt
    seen = []
    view.renameRequested.connect(seen.append)

    view.selectRow(1)
    view.setFocus()
    qtest.keyClick(view, qt.Key_F2)
    app.processEvents()

    assert seen
    assert [item.name for item in seen[-1]] == ["track-1"]


def test_space_toggles_current_row_selection(explorer_view_qt):
    app, _qtest, qt, view = explorer_view_qt
    from PySide6.QtCore import QEvent, QItemSelectionModel
    from PySide6.QtGui import QKeyEvent

    index = view.model().index(1, 0)
    view.selectionModel().setCurrentIndex(index, QItemSelectionModel.NoUpdate)
    view.clearSelection()
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, qt.Key_Space, qt.NoModifier))
    app.processEvents()

    assert [idx.row() for idx in view.selectionModel().selectedRows()] == [1]


def test_keyboard_context_menu_key_emits_position(explorer_view_qt):
    app, qtest, qt, view = explorer_view_qt
    seen = []
    view.keyboardContextMenuRequested.connect(seen.append)

    view.setCurrentIndex(view.model().index(1, 0))
    view.setFocus()
    qtest.keyClick(view, qt.Key_Menu)
    app.processEvents()

    assert seen


def test_filename_double_click_emits_open_requested(explorer_view_qt):
    app, _qtest, _qt, view = explorer_view_qt
    from PySide6.QtCore import QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    seen = []
    view.openRequested.connect(seen.append)
    index = view.model().index(1, 1)
    point = view.visualRect(index).center()
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(point), QPointF(point),
        _qt.MouseButton.LeftButton, _qt.MouseButton.LeftButton, _qt.KeyboardModifier.NoModifier,
    )
    view.mouseDoubleClickEvent(event)
    app.processEvents()

    assert seen
    assert [item.name for item in seen[-1]] == ["track-1"]


def test_header_resize_handle_double_click_requests_auto_size(explorer_view_qt):
    app, _qtest, qt, view = explorer_view_qt
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtWidgets import QHeaderView
    from ui.models.metadata_table_model import COL_CHECK, COL_FILENAME
    from ui.panels.metadata_editor.explorer_view import MetadataHeaderView

    header = MetadataHeaderView(view)
    view.setHorizontalHeader(header)
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
    view.resize(360, 180)
    header.resize(view.width(), 24)
    header.show()
    view.show()
    app.processEvents()

    seen = []
    header.sectionAutoSizeRequested.connect(seen.append)
    x = header.sectionViewportPosition(COL_FILENAME) + header.sectionSize(COL_FILENAME) - 1

    class ResizeDoubleClick:
        def __init__(self, pos: QPointF) -> None:
            self._pos = pos
            self.accepted = False

        def position(self) -> QPointF:
            return self._pos

        def accept(self) -> None:
            self.accepted = True

    assert header._section_resize_handle_at(QPoint(x, header.height() // 2)) == COL_FILENAME
    event = ResizeDoubleClick(QPointF(x, header.height() // 2))
    header.mouseDoubleClickEvent(event)
    app.processEvents()

    assert seen == [COL_FILENAME]
    assert event.accepted
    gutter_x = header.sectionViewportPosition(COL_CHECK) + header.sectionSize(COL_CHECK) - 1
    assert header._section_resize_handle_at(QPoint(gutter_x, header.height() // 2)) == -1


def test_rubber_band_selection_only_checks_visible_rows(explorer_view_qt):
    app, _qtest, qt, _view = explorer_view_qt
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QStandardItem, QStandardItemModel
    from PySide6.QtWidgets import QAbstractItemView
    from ui.panels.metadata_editor.explorer_view import ExplorerDetailsView

    seen_rows = []

    class CountingView(ExplorerDetailsView):
        def rowViewportPosition(self, row: int) -> int:
            seen_rows.append(row)
            return super().rowViewportPosition(row)

    model = QStandardItemModel(200, 2)
    for row in range(200):
        model.setItem(row, 0, QStandardItem(str(row)))
        model.setItem(row, 1, QStandardItem(str(row)))

    view = CountingView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection)
    view.resize(320, 160)
    view.show()
    app.processEvents()

    try:
        view._rubber_rect = QRect(0, 0, view.viewport().width(), view.viewport().height())
        view._rubber_modifiers = qt.NoModifier
        view._select_rows_in_rubber_band()

        assert seen_rows
        assert max(seen_rows) < 20
    finally:
        view.close()
        view.deleteLater()


def test_filename_delegate_keeps_filename_text_ltr_in_rtl_app(explorer_view_qt):
    app, _qtest, qt, view = explorer_view_qt
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QLineEdit, QStyleOptionViewItem
    from ui.panels.metadata_editor.explorer_view import FilenameDelegate

    old_direction = app.layoutDirection()
    app.setLayoutDirection(qt.LayoutDirection.RightToLeft)
    try:
        delegate = FilenameDelegate(view, show_checkbox=True)
        option = QStyleOptionViewItem()
        delegate.initStyleOption(option, view.model().index(0, 0))

        assert option.direction == qt.LayoutDirection.LeftToRight
        assert option.displayAlignment & qt.AlignmentFlag.AlignRight

        checkbox = delegate.checkbox_hit_rect(QRect(0, 0, 240, 40))
        assert checkbox.center().x() > 120

        editor = QLineEdit("Mixed עברית 01.mp3")
        delegate.setEditorData(editor, view.model().index(0, 0))

        assert editor.layoutDirection() == qt.LayoutDirection.LeftToRight
        assert editor.alignment() & qt.AlignmentFlag.AlignLeft
    finally:
        app.setLayoutDirection(old_direction)
