"""
ui/panels/metadata_editor/tree_ops.py  –  Tag Editor
==============================================================================
Folder-tree file operations, its context menu, and the path
rebasing that keeps loaded tracks correct after a move or rename.

Extracted from panel.py unchanged; MetadataEditorPanel mixes this in,
so every attribute reference resolves exactly as before.
"""

from __future__ import annotations

from . import prompts

from pathlib import Path
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QFileInfo,
    QItemSelection,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QComboBox,
    QFileDialog,
    QFileIconProvider,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSplitterHandle,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon
from ui.direction import isolate_number
from utils.time_format import display_timestamp
from ui.i18n import t
from ui.services.file_operation_service import FileOperationError, FileOperationService


class TreeOpsMixin:
    """Folder-tree file operations, its context menu, and the path"""

    def _on_tree_item_moved(self, src: Path, dest: Path) -> None:
        """Physically moves a file or folder on the disk, and updates UI.

        The controller owns the lifecycle guard, the evidence and the
        reconciliation; the panel only rebases what it displays afterwards.
        """
        result = self._run_file_operation("move_paths", [src], Path(dest).parent)
        if result is not None and result.succeeded:
            self._rebase_loaded_paths(src, result.succeeded[0].destination)

    def _on_tree_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return

        path_str = item.data(0, Qt.UserRole)
        if not path_str:
            return
        path = Path(path_str)
        is_file = item.data(0, self._ROLE_IS_FILE)
        add_folder_action = None

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        open_action = menu.addAction(t("meta_open_file")) if is_file else None
        reveal_action = menu.addAction(t("meta_reveal_in_explorer"))
        copy_action = menu.addAction(t("meta_copy_path"))
        properties_action = menu.addAction(t("meta_properties"))
        menu.addSeparator()
        if not is_file:
            add_folder_action = menu.addAction(FluentIcon.FOLDER_ADD.icon(), t("meta_add_folder"))
            menu.addSeparator()

        rename_action = menu.addAction(FluentIcon.EDIT.icon(), t("meta_rename_menu"))
        move_action = menu.addAction(t("meta_move_menu"))
        delete_action = menu.addAction(FluentIcon.DELETE.icon(), t("meta_delete_menu"))

        action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if open_action is not None and action == open_action:
            self._perform_path_operation([path], self._file_operations.open_file)
        elif action == reveal_action:
            self._perform_path_operation([path], self._file_operations.reveal_in_explorer)
        elif action == copy_action:
            self._copy_tree_path(path)
        elif action == properties_action:
            self._show_path_properties(path)
        elif add_folder_action is not None and action == add_folder_action:
            self._on_tree_add_folder(path)
        elif action == rename_action:
            self._on_tree_rename(path, is_file)
        elif action == move_action:
            self._move_tree_path(path)
        elif action == delete_action:
            self._on_tree_delete(path, is_file)

    def _on_tree_add_folder(self, parent_path: Path) -> None:
        new_name, ok = prompts.get_text(
            self,
            t("meta_new_folder_dialog_title"),
            t("meta_new_folder_prompt"),
            text=t("meta_new_folder_default"),
        )
        if not ok or not new_name.strip():
            return

        result = self._run_file_operation("create_folder", parent_path, new_name)
        if result is not None and result.succeeded:
            self._rebuild_tree_from_loaded_tracks()
            self._get_or_create_folder_item(result.succeeded[0].destination)

    def _on_tree_rename(self, path: Path, is_file: bool) -> None:
        new_name, ok = prompts.get_text(
            self,
            t("meta_rename_dialog_title"),
            t("meta_rename_prompt"),
            text=path.name,
        )
        if not ok or not new_name.strip():
            return

        result = self._run_file_operation("rename_path", path, new_name)
        if result is not None and result.succeeded:
            self._rebase_loaded_paths(path, result.succeeded[0].destination)

    def _on_tree_delete(self, path: Path, is_file: bool) -> None:
        title = t("meta_delete_file_title") if is_file else t("meta_delete_folder_title")
        text = t("meta_delete_confirm", name=path.name)
        if not is_file:
            text += t("meta_delete_recursive_note")

        if not prompts.confirm(self, title, text, accept_text=t("meta_delete_menu"),
                       cancel_text=t("cancel_btn"), danger=True):
            return
        result = self._run_file_operation("recycle_paths", [path])
        if result is None or not result.succeeded:
            return
        removed = [track.path for track in self._model.get_all_tracks()
                   if track.path == path or self._is_path_within(track.path, path)]
        self._model.remove_paths(removed)
        self._navigation.reconcile_after_delete(path)
        self._rebuild_tree_from_loaded_tracks()
        self._apply_navigation_filter()
        self._update_summary()
        self._refresh_checked_scope_state()

    def _move_tree_path(self, path: Path) -> None:
        if not self._root_folder:
            return
        from .dialogs import MovePathDialog
        destinations = []
        for folder in sorted(self._folder_items, key=lambda value: str(value).casefold()):
            if folder == path or folder == path.parent:
                continue
            try:
                folder.relative_to(path)
                continue
            except ValueError:
                pass
            destinations.append(folder)
        dialog = MovePathDialog(path, destinations, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.destination is None:
            return
        destination = Path(dialog.destination) / path.name
        self._on_tree_item_moved(path, destination)

    def _copy_tree_path(self, path: Path) -> None:
        try:
            QApplication.clipboard().setText(self._file_operations.copy_path(path))
        except FileOperationError as exc:
            prompts.show_warning(self, t("meta_error_title"), str(exc))

    def _show_path_properties(self, path: Path) -> None:
        try:
            props = self._file_operations.properties(path)
        except FileOperationError as exc:
            prompts.show_warning(self, t("meta_error_title"), str(exc))
            return
        from .dialogs import PropertiesDialog
        rows = [
            (t("meta_property_path"), str(props.path)),
            (t("meta_property_format"), t("meta_property_folder") if props.is_directory else path.suffix.lstrip(".").upper()),
            (t("meta_property_size"), isolate_number(f"{props.size_bytes:,}")),
            (t("meta_property_modified"), isolate_number(display_timestamp(props.modified_at))),
        ]
        PropertiesDialog(
            [(props.path.name, rows)],
            self,
            open_callback=(
                None if props.is_directory
                else lambda: self._perform_path_operation(
                    [path], self._file_operations.open_file)),
            reveal_callback=lambda: self._perform_path_operation(
                [path], self._file_operations.reveal_in_explorer),
            copy_callback=lambda: self._copy_tree_path(path),
        ).exec()

    def _perform_path_operation(self, paths: list[Path], operation) -> None:
        errors: list[str] = []
        for path in paths:
            try:
                operation(path)
            except FileOperationError as exc:
                errors.append(str(exc))
        if errors:
            prompts.show_warning(self, t("meta_error_title"), "\n".join(errors))

    @staticmethod
    def _is_path_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _rebase_loaded_paths(self, source: Path, destination: Path) -> None:
        self._navigation.remap_folder(source, destination)
        for track in self._model.get_all_tracks():
            try:
                relative = track.path.relative_to(source)
            except ValueError:
                continue
            new_path = destination / relative
            if track.proposed_filename == new_path.name:
                track.proposed_filename = None
            self._model.update_file_path(track, new_path)
        self._rebuild_tree_from_loaded_tracks()
        self._apply_navigation_filter()

    def _rebuild_tree_from_loaded_tracks(self) -> None:
        if not self._root_folder:
            return
        was_blocked = self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        self._ignore_tree_changes = True
        try:
            self._tree.clear()
            self._folder_items.clear()
            self._file_items.clear()
            self._ensure_root_item()
            self._add_many_to_tree(self._model.get_all_tracks())
            current = self._navigation.current
            if current is not None and current.is_dir():
                self._get_or_create_folder_item(current)
        finally:
            self._ignore_tree_changes = False
            self._tree.blockSignals(was_blocked)
            self._tree.setUpdatesEnabled(True)
