"""
ui/panels/metadata_editor/file_actions.py  –  Tag Editor
==============================================================================
Track-level file operations and the table row context menu.

Every physical mutation is delegated to the controller-owned
operation owner; only read-only inspection runs locally.

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
from ui.direction import isolate_number
from utils.time_format import display_timestamp
from core.metadata_models import (
    ARTWORK_FIELD,
    AudioTrackItem,
    LyricsEntry,
    ScanResult,
    TrackStatus,
    LYRICS_FIELD,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_FIELDS,
    REPLAYGAIN_REFERENCE_LOUDNESS,
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
)
from ui.i18n import t
from ui.services.file_operation_service import FileOperationError, FileOperationService


class FileActionsMixin:
    """Track-level file operations and the table row context menu."""

    def _request_delete_files(self, paths: list[Path]) -> None:
        """Single-confirm Recycle Bin send for selected table rows.

        Called from `ExplorerFileListView.keyPressEvent` on Delete. Emits
        `delete_files_requested` only after the user confirms — the actual
        send2trash + rescan is owned by `MetadataController.delete_files`.
        """
        if not paths:
            return
        if prompts.confirm(
            self.window(),
            t("meta_delete_to_trash_title"),
            t("meta_delete_to_trash_body", n=len(paths)),
            accept_text=t("meta_delete_to_trash_confirm"),
            cancel_text=t("cancel_btn"),
            danger=True,
        ):
            result = self._run_file_operation("recycle_paths", list(paths))
            if result is None:
                return
            deleted = [outcome.source for outcome in result.succeeded]
            if deleted:
                self._model.remove_paths(deleted)
                self._rebuild_tree_from_loaded_tracks()
                self._update_summary()
                self._refresh_checked_scope_state()

    def _on_table_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if index.isValid() and not self._table.selectionModel().isSelected(index):
            self._table.selectRow(index.row())
            self._table.setCurrentIndex(index)
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        open_action = menu.addAction(t("meta_open_file"))
        reveal_action = menu.addAction(t("meta_reveal_in_explorer"))
        copy_action = menu.addAction(t("meta_copy_path"))
        menu.addSeparator()
        rename_action = menu.addAction(t("meta_rename_menu")) if len(tracks) == 1 else None
        move_action = menu.addAction(t("meta_move_menu"))
        properties_action = menu.addAction(t("meta_properties"))
        menu.addSeparator()
        delete_action = menu.addAction(t("meta_delete_menu"))
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == open_action:
            self._open_tracks(tracks)
        elif action == reveal_action:
            self._reveal_tracks(tracks)
        elif action == copy_action:
            self._copy_paths(tracks)
        elif rename_action is not None and action == rename_action:
            self._rename_tracks(tracks)
        elif action == move_action:
            self._move_tracks(tracks)
        elif action == properties_action:
            self._show_properties(tracks)
        elif action == delete_action:
            self._request_delete_files([track.path for track in tracks])

    def _open_tracks(self, tracks: list[AudioTrackItem]) -> None:
        self._perform_track_operation(tracks, self._file_operations.open_file)

    def _reveal_tracks(self, tracks: list[AudioTrackItem]) -> None:
        self._perform_track_operation(tracks, self._file_operations.reveal_in_explorer)

    def _copy_paths(self, tracks: list[AudioTrackItem]) -> None:
        try:
            paths = [self._file_operations.copy_path(track.path) for track in tracks]
        except FileOperationError as exc:
            prompts.show_warning(self, t("meta_error_title"), str(exc))
            return
        QApplication.clipboard().setText("\n".join(paths))

    def _rename_tracks(self, tracks: list[AudioTrackItem]) -> None:
        if len(tracks) != 1:
            return
        track = tracks[0]
        new_name, ok = prompts.get_text(self, t("meta_rename_dialog_title"), t("meta_rename_prompt"), text=track.path.name)
        if not ok or not new_name.strip():
            return
        result = self._run_file_operation("rename_path", track.path, new_name)
        if result is None or not result.succeeded:
            return
        destination = result.succeeded[0].destination
        if track.proposed_filename == destination.name:
            track.proposed_filename = None
        self._model.update_file_path(track, destination)
        self._rebuild_tree_from_loaded_tracks()
        self._refresh_checked_scope_state()

    def _move_tracks(self, tracks: list[AudioTrackItem]) -> None:
        if not self._root_folder:
            return
        folder = QFileDialog.getExistingDirectory(self, t("meta_move_choose_folder"), str(self._root_folder))
        if not folder:
            return
        by_path = {track.path: track for track in tracks}
        result = self._run_file_operation(
            "move_paths", list(by_path), Path(folder))
        if result is None:
            return
        for outcome in result.succeeded:
            track = by_path.get(outcome.source)
            if track is not None:
                self._model.update_file_path(track, outcome.destination)
        if result.succeeded:
            self._rebuild_tree_from_loaded_tracks()
            self._refresh_checked_scope_state()

    def _show_properties(self, tracks: list[AudioTrackItem]) -> None:
        lines: list[str] = []
        for track in tracks:
            try:
                props = self._file_operations.properties(track.path)
            except FileOperationError as exc:
                lines.append(str(exc))
                continue
            lines.append(t("meta_properties_item", name=props.path.name, path=str(props.path), size=isolate_number(f"{props.size_bytes:,}"), modified=isolate_number(display_timestamp(props.modified_at))))
        if lines:
            prompts.show_info(self, t("meta_properties"), "\n\n".join(lines))

    def _perform_track_operation(self, tracks: list[AudioTrackItem], operation) -> None:
        errors: list[str] = []
        for track in tracks:
            try:
                operation(track.path)
            except FileOperationError as exc:
                errors.append(str(exc))
        if errors:
            prompts.show_warning(self, t("meta_error_title"), "\n".join(errors))
