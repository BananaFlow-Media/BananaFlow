"""Guarded product UI for managing only BananaFlow tag-operation backups."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.backup_manager import BackupManager, BackupManagerError
from core.metadata_processor import load_tag_backup
from core.operation_manifest import ManifestError, read_manifest
from ui.dialogs.styled_dialog import confirm, show_info, show_warning
from ui.dialogs.styled_dialog import add_header, make_footer
from ui.i18n import t
from ui.panels.metadata_editor.shared import mark_tag_editor_dialog
from ui.touch import apply_touch_support
from qfluentwidgets import FluentIcon


class BackupManagerDialog(QDialog):
    """Inspect/export/delete in-root backups and route reviewed disk actions."""

    def __init__(self, root: Path, *, restore_callback, undo_callback, parent=None) -> None:
        super().__init__(parent)
        mark_tag_editor_dialog(self)
        self._manager = BackupManager(root)
        self._restore_callback = restore_callback
        self._undo_callback = undo_callback
        self.setWindowTitle(t("meta_backup_manager"))
        self.setMinimumSize(900, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 10)
        layout.setSpacing(10)
        add_header(layout, t("meta_backup_manager"), t("meta_backup_manager_note"),
                   icon=FluentIcon.FOLDER.icon())
        self._note = QLabel(t("meta_backup_manager_note"), self)
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        self._table = QTableWidget(0, 10, self)
        self._table.setHorizontalHeaderLabels([
            t("meta_backup_created"), t("meta_backup_operation"), t("meta_backup_files"),
            t("meta_backup_schema"), t("meta_backup_app_version"), t("meta_backup_root"),
            t("meta_backup_status"), t("meta_backup_size"), t("meta_backup_validity"),
            t("meta_backup_location"),
        ])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self._table, 1)
        buttons = QHBoxLayout()
        self._preview = QPushButton(t("meta_backup_preview_restore"), self)
        self._restore = QPushButton(t("meta_backup_restore"), self)
        self._undo = QPushButton(t("meta_backup_undo_batch"), self)
        self._details = QPushButton(t("meta_backup_details"), self)
        self._export = QPushButton(t("meta_backup_export"), self)
        self._delete = QPushButton(t("meta_backup_delete"), self)
        self._refresh = QPushButton(t("meta_backup_refresh"), self)
        for button in (self._preview, self._restore, self._undo, self._details,
                       self._export, self._delete, self._refresh):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        close = QPushButton(t("meta_cancel"), self)
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self._preview.clicked.connect(self._preview_restore)
        self._restore.clicked.connect(self._restore_selected)
        self._undo.clicked.connect(self._undo_selected)
        self._details.clicked.connect(self._show_details)
        self._export.clicked.connect(self._export_selected)
        self._delete.clicked.connect(self._delete_selected)
        self._refresh.clicked.connect(self.refresh)
        self.refresh()
        # Not a StyledDialog, so the shared touch sweep does not reach it.
        apply_touch_support(self)

    def refresh(self) -> None:
        self._infos = self._manager.list_backups()
        self._table.setRowCount(len(self._infos))
        for row, info in enumerate(self._infos):
            validity = t("meta_backup_valid") if info.valid else t("meta_backup_invalid")
            if info.interrupted:
                validity = t("meta_backup_journal_referenced")
            values = [info.created, info.operation_type, str(info.affected_files),
                      str(info.schema or ""), info.app_version, info.root or "", info.status or info.operation_id,
                      self._format_size(info.size_bytes),
                      validity, str(info.path)]
            for col, value in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem(value))
        self._table.resizeColumnsToContents()

    @staticmethod
    def _format_size(value: int) -> str:
        if value < 1024:
            return f"{value} B"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KB"
        return f"{value / (1024 * 1024):.1f} MB"

    def _selected(self):
        rows = self._table.selectionModel().selectedRows()
        return self._infos[rows[0].row()] if rows else None

    def _records(self):
        info = self._selected()
        if info is None or not info.valid:
            return None
        try:
            return info, load_tag_backup(info.path)
        except Exception as exc:
            show_warning(self, t("meta_backup_invalid"), str(exc))
            return None

    def _preview_restore(self) -> None:
        result = self._records()
        if result is None:
            return
        _info, records = result
        names = "\n".join(str(path) for path, _tags in records[:12])
        if len(records) > 12:
            names += "\n" + t("meta_backup_more_files", n=len(records) - 12)
        show_info(self, t("meta_backup_preview_restore"), t("meta_backup_preview_message", n=len(records)) + "\n\n" + names)

    def _restore_selected(self) -> None:
        result = self._records()
        if result is None:
            return
        _info, records = result
        if confirm(self, t("meta_backup_restore"), t("meta_backup_restore_confirm", n=len(records)),
                   accept_text=t("meta_backup_restore"), danger=True):
            self._restore_callback({"records": records, "backup_path": info.path})
            self.accept()

    def _undo_selected(self) -> None:
        info = self._selected()
        if info is None or not info.valid:
            return
        try:
            manifest = read_manifest(info.path)
            if manifest.get("status") != "completed":
                raise ManifestError("operation is not completed")
        except Exception as exc:
            show_warning(self, t("meta_backup_undo_batch"), str(exc))
            return
        if confirm(self, t("meta_backup_undo_batch"), t("meta_backup_undo_confirm"),
                   accept_text=t("meta_backup_undo_batch"), danger=True):
            self._undo_callback(info.path, False)
            self.accept()

    def _show_details(self) -> None:
        info = self._selected()
        if info is None:
            return
        detail = "\n".join((
            f"{t('meta_backup_location')}: {info.path}",
            f"{t('meta_backup_operation')}: {info.operation_type}",
            f"{t('meta_backup_files')}: {info.affected_files}",
            f"{t('meta_backup_schema')}: {info.schema or ''}",
            f"{t('meta_backup_size')}: {self._format_size(info.size_bytes)}",
            f"{t('meta_backup_validity')}: {t('meta_backup_valid') if info.valid else info.error}",
        ))
        show_info(self, t("meta_backup_details"), detail)

    def _export_selected(self) -> None:
        info = self._selected()
        if info is None:
            return
        target, _ = QFileDialog.getSaveFileName(self, t("meta_backup_export"), info.path.name,
                                                 "JSON (*.json)")
        if not target:
            return
        try:
            self._manager.export(info.path, Path(target))
        except BackupManagerError as exc:
            show_warning(self, t("meta_backup_export"), str(exc))

    def _delete_selected(self) -> None:
        info = self._selected()
        if info is None:
            return
        if info.protected or info.interrupted:
            show_warning(self, t("meta_backup_delete"), t("meta_backup_delete_protected"))
            return
        if not confirm(self, t("meta_backup_delete"), t("meta_backup_delete_confirm"),
                       accept_text=t("meta_backup_delete"), danger=True):
            return
        try:
            self._manager.delete(info.path)
        except BackupManagerError as exc:
            show_warning(self, t("meta_backup_delete"), str(exc))
            return
        self.refresh()
