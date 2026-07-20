"""
ui/panels/metadata_editor/tree.py  –  folder/file tree with physical move DnD
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget

ROLE_IS_FILE = Qt.UserRole + 1


class ExplorerTreeWidget(QTreeWidget):
    """Tree view with physical drag and drop to move files/folders, and custom drop handling."""
    item_moved = Signal(Path, Path)  # src, dest
    #: The Menu key and Shift+F10 must reach the same context menu the mouse
    #: reaches. Qt's own keyboard-triggered contextMenuEvent does not reach
    #: this view reliably, so it is handled explicitly here, matching the
    #: table's ExplorerDetailsView.keyboardContextMenuRequested.
    keyboardContextMenuRequested = Signal(QPoint)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() == Qt.Key_Menu
            or (event.key() == Qt.Key_F10 and event.modifiers() == Qt.ShiftModifier)
        ):
            self.keyboardContextMenuRequested.emit(self._current_context_menu_pos())
            event.accept()
            return
        super().keyPressEvent(event)

    def _current_context_menu_pos(self) -> QPoint:
        item = self.currentItem()
        if item is not None:
            rect = self.visualItemRect(item)
            if rect.isValid():
                return rect.center()
        return self.viewport().rect().center()

    def dragEnterEvent(self, event) -> None:
        if self._is_internal_move_event(event):
            super().dragEnterEvent(event)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._is_internal_move_event(event) and self._drop_destination_for_event(event) is not None:
            super().dragMoveEvent(event)
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        destination = self._drop_destination_for_event(event)
        if destination is None:
            event.ignore()
            return

        source_item = self._single_selected_item()
        src_path = Path(source_item.data(0, Qt.UserRole))
        self.item_moved.emit(src_path, destination)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def _drop_destination_for_event(self, event) -> Path | None:
        if not self._is_internal_move_event(event):
            return None
        target_item = self.itemAt(self._event_pos(event))
        if not target_item and self.topLevelItemCount() > 0:
            target_item = self.topLevelItem(0)
        return self._drop_destination_for_items(self._single_selected_item(), target_item)

    def _drop_destination_for_items(self, source_item, target_item) -> Path | None:
        if source_item is None or target_item is None or source_item == target_item:
            return None
        if self._is_ancestor(source_item, target_item):
            return None

        src_path = source_item.data(0, Qt.UserRole)
        target_path = target_item.data(0, Qt.UserRole)
        if not src_path or not target_path:
            return None

        src_path = Path(src_path)
        target_path = Path(target_path)
        dest_dir = target_path.parent if bool(target_item.data(0, ROLE_IS_FILE)) else target_path
        dest_path = dest_dir / src_path.name

        if src_path == dest_path or src_path.parent == dest_dir:
            return None
        if dest_path.exists():
            return None

        root_path = self._root_path()
        if root_path is not None:
            if not self._is_relative_to(dest_dir, root_path):
                return None
            if src_path.is_dir() and self._is_relative_to(dest_dir, src_path):
                return None

        return dest_path

    def _single_selected_item(self):
        selected = self.selectedItems()
        return selected[0] if len(selected) == 1 else None

    def _root_path(self) -> Path | None:
        if self.topLevelItemCount() <= 0:
            return None
        root_path = self.topLevelItem(0).data(0, Qt.UserRole)
        return Path(root_path) if root_path else None

    @staticmethod
    def _is_ancestor(parent_item, child_item) -> bool:
        current = child_item
        while current is not None:
            if current == parent_item:
                return True
            current = current.parent()
        return False

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            return False

    @staticmethod
    def _event_pos(event):
        if hasattr(event, "position"):
            return event.position().toPoint()
        return event.pos()

    def _is_internal_move_event(self, event) -> bool:
        if event.source() != self:
            return False
        if not (event.possibleActions() & Qt.DropAction.MoveAction):
            return False
        disallowed = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        return not (event.keyboardModifiers() & disallowed)
