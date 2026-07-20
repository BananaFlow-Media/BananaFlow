from __future__ import annotations

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from ui.panels.metadata_editor.tree import ExplorerTreeWidget, ROLE_IS_FILE


@pytest.fixture()
def tree_qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    tree = ExplorerTreeWidget()
    yield app, tree
    tree.close()
    tree.deleteLater()


def _folder_item(name: str, path) -> QTreeWidgetItem:
    item = QTreeWidgetItem([name])
    item.setData(0, Qt.UserRole, path)
    item.setData(0, ROLE_IS_FILE, False)
    return item


def _file_item(name: str, path) -> QTreeWidgetItem:
    item = QTreeWidgetItem([name])
    item.setData(0, Qt.UserRole, path)
    item.setData(0, ROLE_IS_FILE, True)
    return item


def test_drop_on_file_targets_parent_directory(tree_qt, tmp_path):
    _app, tree = tree_qt
    source_path = tmp_path / "a" / "song.mp3"
    target_path = tmp_path / "b" / "other.mp3"
    source_path.parent.mkdir()
    target_path.parent.mkdir()

    root = _folder_item("root", tmp_path)
    source = _file_item("song.mp3", source_path)
    target = _file_item("other.mp3", target_path)
    tree.addTopLevelItem(root)
    root.addChild(source)
    root.addChild(target)

    assert tree._drop_destination_for_items(source, target) == tmp_path / "b" / "song.mp3"


def test_drop_rejects_existing_destination(tree_qt, tmp_path):
    _app, tree = tree_qt
    source_path = tmp_path / "a" / "song.mp3"
    source_path.parent.mkdir()
    dest_dir = tmp_path / "b"
    dest_dir.mkdir()
    (dest_dir / "song.mp3").write_text("", encoding="utf-8")

    root = _folder_item("root", tmp_path)
    source = _file_item("song.mp3", source_path)
    target = _folder_item("b", dest_dir)
    tree.addTopLevelItem(root)
    root.addChild(source)
    root.addChild(target)

    assert tree._drop_destination_for_items(source, target) is None


def test_drop_rejects_folder_into_own_descendant(tree_qt, tmp_path):
    _app, tree = tree_qt
    folder = tmp_path / "album"
    child = folder / "disc1"
    child.mkdir(parents=True)

    root = _folder_item("root", tmp_path)
    folder_item = _folder_item("album", folder)
    child_item = _folder_item("disc1", child)
    tree.addTopLevelItem(root)
    root.addChild(folder_item)
    folder_item.addChild(child_item)

    assert tree._drop_destination_for_items(folder_item, child_item) is None


def test_internal_move_rejects_copy_or_shortcut_modifiers(tree_qt):
    _app, tree = tree_qt

    class Event:
        def __init__(self, modifiers) -> None:
            self._modifiers = modifiers

        def source(self):
            return tree

        def possibleActions(self):
            return Qt.DropAction.MoveAction

        def keyboardModifiers(self):
            return self._modifiers

    assert tree._is_internal_move_event(Event(Qt.KeyboardModifier.ShiftModifier))
    assert not tree._is_internal_move_event(
        Event(Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    )
    assert not tree._is_internal_move_event(Event(Qt.KeyboardModifier.AltModifier))
