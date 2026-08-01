"""
ui/panels/metadata_editor/dialogs.py  –  Tag Editor dialogs
=============================================================
MoreColumnsDialog        — Explorer 'Choose details…' column picker
AutoArrangeSettingsDialog — which magic ops the auto-arrange button runs
CleanSettingsDialog      — aggressiveness of the cleaning operations
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon

from ui.dialogs.styled_dialog import (
    CheckMarkBox,
    StyledDialog,
    add_header,
    make_button,
    make_footer,
    make_root_layout,
    make_section,
    make_setting_row,
    section_layout,
    set_button_role,
    show_info,
)
from ui.i18n import t
from ui.models.metadata_table_model import (
    COLUMN_COUNT,
    COL_CHECK,
    COL_END_GUTTER,
    COL_GUTTER,
    COL_FILENAME,
    COL_STATUS,
    _HEADER_KEYS,
)

from .shared import MAGIC_OP_DEFS, mark_tag_editor_dialog


class MoreColumnsDialog(StyledDialog):
    """Scrollable, searchable list of all table columns — mirrors Windows
    Explorer's 'Choose details…' dialog that appears from 'More…' in the
    column header context menu."""

    def __init__(self, table_view: QTableView, parent=None) -> None:
        super().__init__(parent, minimum_size=(400, 460), resize_to=(440, 540))
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("mt_more_columns_title"))
        self._table = table_view

        layout = make_root_layout(self)
        add_header(
            layout,
            t("mt_more_columns_title"),
            t("mt_search_columns"),
            icon=FluentIcon.LAYOUT.icon(),
        )
        section = make_section()
        section_lay = section_layout(section)

        # Search bar
        self._search = QLineEdit()
        self._search.setPlaceholderText(t("mt_search_columns"))
        self._search.textChanged.connect(self._on_search)
        section_lay.addWidget(self._search)

        # Scrollable column list
        scroll = QScrollArea()
        scroll.setObjectName("dialogNoBorderScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        self._list_layout = QVBoxLayout(content)
        self._list_layout.setSpacing(6)
        self._list_layout.setContentsMargins(0, 8, 0, 0)

        # Always-visible columns that can't be hidden
        ALWAYS_VISIBLE = {COL_FILENAME, COL_STATUS}
        # Columns never offered in any menu
        NO_MENU = {COL_CHECK, COL_GUTTER, COL_END_GUTTER}

        self._rows: list[tuple[int, str, QCheckBox]] = []
        for col in range(COLUMN_COUNT):
            if col in NO_MENU:
                continue
            key = _HEADER_KEYS[col] if col < len(_HEADER_KEYS) else ""
            label = t(key) if key else ""
            if not label:
                continue
            cb = CheckMarkBox(label)
            cb.setChecked(not table_view.isColumnHidden(col))
            if col in ALWAYS_VISIBLE:
                cb.setEnabled(False)
            self._rows.append((col, label, cb))
            self._list_layout.addWidget(make_setting_row(cb))

        self._list_layout.addStretch()
        scroll.setWidget(content)
        section_lay.addWidget(scroll, stretch=1)
        layout.addWidget(section, stretch=1)

        # OK / Cancel
        cancel_btn = make_button(t("meta_cancel"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = make_button(t("meta_ok"), "primary")
        ok_btn.clicked.connect(self._accept)
        layout.addWidget(make_footer(cancel_btn, ok_btn))

    def _on_search(self, text: str) -> None:
        needle = text.casefold()
        for _, label, cb in self._rows:
            cb.setVisible(not needle or needle in label.casefold())

    def _accept(self) -> None:
        for col, _, cb in self._rows:
            self._table.setColumnHidden(col, not cb.isChecked())
        self.accept()


class AutoArrangeSettingsDialog(StyledDialog):
    """Choose which magic operations the 🪄 auto-arrange button runs."""

    def __init__(self, enabled: set[str], parent=None) -> None:
        super().__init__(parent, minimum_size=(420, 500), resize_to=(480, 580))
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("meta_auto_settings_title"))

        self._result = set(enabled)
        layout = make_root_layout(self)
        add_header(
            layout,
            t("meta_auto_settings_title"),
            t("meta_auto_header"),
        )

        note = QLabel(t("meta_auto_album_note"))
        note.setObjectName("dialogHint")
        note.setWordWrap(True)
        layout.addWidget(note)

        scroll = QScrollArea()
        scroll.setObjectName("dialogNoBorderScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(6)

        # Same four buckets as the inspector rail, so the settings dialog
        # reads as "which of these already-organised actions run automatically"
        # rather than a flat, unordered wall of checkboxes.
        op_defs_by_key = {key: (label_key, desc_key) for key, label_key, desc_key in MAGIC_OP_DEFS}
        op_sections = (
            ("meta_group_from_filename", ("title_strip", "title_full", "track_num", "split_at")),
            ("meta_section_text_cleanup", ("normalize_spaces", "strip_junk", "album_artist")),
            ("meta_section_clear_fields", (
                "clear_title", "clear_artist", "clear_album", "clear_album_artist",
                "clear_track_num", "clear_year", "clear_genre", "clear_comments",
            )),
            ("meta_rename_group", (
                "clean_filename", "strip_filename_numbering", "rename_from_title")),
        )
        # Parameterised operations are deliberately absent: Auto-Order runs
        # unattended, and there is nowhere here to supply the find text, the
        # case mode or the starting number they each require.

        self._cbs: dict[str, QCheckBox] = {}
        for i, (subheader_key, keys) in enumerate(op_sections):
            header = QLabel(t(subheader_key))
            header.setObjectName("dialogSectionTitle")
            if i:
                header.setStyleSheet("margin-top: 6px;")
            scroll_layout.addWidget(header)
            for key in keys:
                label_key, desc_key = op_defs_by_key[key]
                label = t(label_key)
                desc  = t(desc_key)
                row = QHBoxLayout()
                row.setSpacing(6)
                cb = CheckMarkBox(label)
                cb.setChecked(key in enabled)
                self._cbs[key] = cb
                row.addWidget(cb)

                info_btn = QPushButton("")
                info_btn.setObjectName("dialogInfoBtn")
                info_btn.setIcon(FluentIcon.INFO.icon())
                info_btn.setFixedSize(30, 30)
                info_btn.setToolTip(desc)
                set_button_role(info_btn, "icon")
                info_btn.clicked.connect(lambda _, l=label, d=desc: self._show_info(l, d))
                row.addWidget(info_btn)
                row.addStretch()
                scroll_layout.addLayout(row)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, stretch=1)

        cancel_btn = make_button(t("meta_cancel"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = make_button(t("meta_ok"), "primary")
        ok_btn.clicked.connect(self._accept)
        layout.addWidget(make_footer(cancel_btn, ok_btn))

    def _accept(self) -> None:
        self._result = {k for k, cb in self._cbs.items() if cb.isChecked()}
        self.accept()

    def _show_info(self, title: str, desc: str) -> None:
        show_info(self, title, desc)

    @property
    def result_ops(self) -> set[str]:
        return self._result


class CleanSettingsDialog(StyledDialog):
    """Choose how aggressive the cleaning features should be."""

    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent, minimum_size=(430, 440), resize_to=(480, 520))
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("meta_clean_settings_title"))
        self._cfg = cfg

        layout = make_root_layout(self)
        add_header(
            layout,
            t("meta_clean_settings_title"),
            "",
            icon=FluentIcon.ERASE_TOOL.icon(),
        )

        # Title clean settings
        title_grp = make_section(t("meta_clean_title_group"))
        title_lay = section_layout(title_grp)

        self.cb_title_brackets = CheckMarkBox(t("meta_clean_brackets"))
        self.cb_title_brackets.setChecked(getattr(self._cfg, "tag_clean_title_remove_brackets", True))

        self.cb_title_english = CheckMarkBox(t("meta_clean_english_junk"))
        self.cb_title_english.setChecked(getattr(self._cfg, "tag_clean_title_remove_web_junk", True))

        self.cb_title_hebrew = CheckMarkBox(t("meta_clean_hebrew_junk"))
        self.cb_title_hebrew.setChecked(getattr(self._cfg, "tag_clean_title_remove_hebrew", True))

        self.cb_title_punc = CheckMarkBox(t("meta_clean_punctuation"))
        self.cb_title_punc.setChecked(getattr(self._cfg, "tag_clean_title_fix_punctuation", True))

        title_lay.addWidget(make_setting_row(self.cb_title_brackets))
        title_lay.addWidget(make_setting_row(self.cb_title_english))
        title_lay.addWidget(make_setting_row(self.cb_title_hebrew))
        title_lay.addWidget(make_setting_row(self.cb_title_punc))
        layout.addWidget(title_grp)

        # Filename clean settings
        fn_grp = make_section(t("meta_clean_filename_group"))
        fn_lay = section_layout(fn_grp)

        self.cb_fn_brackets = CheckMarkBox(t("meta_clean_filename_brackets"))
        self.cb_fn_brackets.setChecked(getattr(self._cfg, "tag_clean_filename_smart_brackets", True))
        self.cb_fn_brackets.setToolTip(t("meta_clean_filename_brackets_tooltip"))

        self.cb_fn_domains = CheckMarkBox(t("meta_clean_filename_domains"))
        self.cb_fn_domains.setChecked(getattr(self._cfg, "tag_clean_filename_remove_domains", True))

        self.cb_fn_emojis = CheckMarkBox(t("meta_clean_filename_emojis"))
        self.cb_fn_emojis.setChecked(getattr(self._cfg, "tag_clean_filename_remove_emojis", True))

        self.cb_fn_spaces = CheckMarkBox(t("meta_clean_filename_spaces"))
        self.cb_fn_spaces.setChecked(getattr(self._cfg, "tag_clean_filename_fix_spaces", True))

        fn_lay.addWidget(make_setting_row(self.cb_fn_brackets))
        fn_lay.addWidget(make_setting_row(self.cb_fn_domains))
        fn_lay.addWidget(make_setting_row(self.cb_fn_emojis))
        fn_lay.addWidget(make_setting_row(self.cb_fn_spaces))
        layout.addWidget(fn_grp)

        layout.addStretch()

        cancel_btn = make_button(t("meta_cancel"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = make_button(t("meta_save_ok"), "primary")
        ok_btn.clicked.connect(self._accept)
        layout.addWidget(make_footer(cancel_btn, ok_btn))

    def _accept(self) -> None:
        if self._cfg:
            self._cfg.tag_clean_title_remove_brackets = self.cb_title_brackets.isChecked()
            self._cfg.tag_clean_title_remove_web_junk = self.cb_title_english.isChecked()
            self._cfg.tag_clean_title_remove_hebrew = self.cb_title_hebrew.isChecked()
            self._cfg.tag_clean_title_fix_punctuation = self.cb_title_punc.isChecked()
            
            self._cfg.tag_clean_filename_smart_brackets = self.cb_fn_brackets.isChecked()
            self._cfg.tag_clean_filename_remove_domains = self.cb_fn_domains.isChecked()
            self._cfg.tag_clean_filename_remove_emojis = self.cb_fn_emojis.isChecked()
            self._cfg.tag_clean_filename_fix_spaces = self.cb_fn_spaces.isChecked()
            self._cfg.save()
        self.accept()


class ApplyConfirmationDialog(StyledDialog):
    """Reference Apply review: authoritative scope, blockers and backup."""

    def __init__(self, summary, *, candidate_count: int, blocker_count: int, parent=None) -> None:
        super().__init__(parent, minimum_size=(480, 360), resize_to=(540, 430))
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("meta_apply_confirm_title"))
        root = make_root_layout(self, margins=(16, 14, 16, 10), spacing=10)
        add_header(
            root, t("meta_apply_confirm_title"), t("meta_apply_dialog_subtitle"),
            icon=FluentIcon.SAVE.icon(),
        )

        if blocker_count:
            warning = QLabel(t("meta_apply_dialog_blocked", n=blocker_count))
            warning.setObjectName("tagDialogResultWarning")
            warning.setWordWrap(True)
            root.addWidget(warning)

        section = make_section(t("meta_apply_dialog_writes_heading"))
        section_lay = section_layout(section)
        for label_key, value in (
            ("meta_apply_dialog_files", candidate_count),
            ("meta_apply_dialog_tag_changes", summary.changed_fields - summary.filename_changes),
            ("meta_apply_dialog_filename_changes", summary.filename_changes),
            ("meta_apply_dialog_excluded", summary.excluded_files),
            ("meta_apply_dialog_blockers", blocker_count),
            ("meta_apply_dialog_backup", t("meta_apply_dialog_backup_value")),
        ):
            row = QHBoxLayout()
            label = QLabel(t(label_key))
            label.setObjectName("tagDialogFormLabel")
            value_label = QLabel(str(value))
            value_label.setObjectName("tagDialogFormValue")
            row.addWidget(label)
            row.addStretch()
            row.addWidget(value_label)
            section_lay.addLayout(row)
        root.addWidget(section)

        note = QLabel(t("meta_apply_dialog_scope_note"))
        note.setObjectName("dialogHint")
        note.setWordWrap(True)
        root.addWidget(note)

        cancel = make_button(t("cancel_btn"), "cancel")
        cancel.clicked.connect(self.reject)
        apply = make_button(t("meta_apply_backup_and_apply"), "primary")
        apply.clicked.connect(self.accept)
        root.addWidget(make_footer(cancel, apply))


class ApplyResultDialog(StyledDialog):
    """Full success/partial/failure result instead of a transient toast only."""

    def __init__(self, result=None, *, error_message: str = "", parent=None) -> None:
        super().__init__(parent, minimum_size=(620, 390), resize_to=(760, 500))
        mark_tag_editor_dialog(self)
        failed = int(getattr(result, "failed_count", 0)) if result is not None else 1
        partial = int(getattr(result, "partial_count", 0)) if result is not None else 0
        success = int(getattr(result, "success_count", 0)) if result is not None else 0
        kind = "failure" if error_message or (failed and not success and not partial) else "partial" if failed or partial else "success"
        title_key = f"meta_apply_result_{kind}_title"
        body_key = f"meta_apply_result_{kind}_body"
        self.setWindowTitle(t(title_key))

        root = make_root_layout(self, margins=(16, 14, 16, 10), spacing=10)
        add_header(root, t(title_key), t("meta_apply_result_subtitle"),
                   icon=(FluentIcon.ACCEPT if kind == "success" else FluentIcon.INFO).icon())
        banner = QLabel(error_message or t(
            body_key, success=success, partial=partial, failed=failed))
        banner.setObjectName(
            "tagDialogResultSuccess" if kind == "success"
            else "tagDialogResultWarning" if kind == "partial"
            else "tagDialogResultError")
        banner.setWordWrap(True)
        root.addWidget(banner)

        outcomes = list(getattr(result, "outcomes", ())) if result is not None else []
        self._details_text = error_message or banner.text()
        table = QTableWidget(0, 4, self)
        table.setHorizontalHeaderLabels([
            t("meta_apply_result_file"), t("meta_apply_result_tags"),
            t("meta_apply_result_rename"), t("meta_apply_result_verify"),
        ])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for outcome in outcomes:
            row = table.rowCount()
            table.insertRow(row)
            values = (
                outcome.final_path.name,
                t(f"meta_apply_result_status_{outcome.status}"),
                t("meta_apply_result_rename_pending") if outcome.rename_pending
                else t("meta_apply_result_not_required"),
                outcome.detail or t(f"meta_apply_result_status_{outcome.status}"),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))
            self._details_text += "\n" + " | ".join(str(value) for value in values)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setVisible(bool(outcomes))
        root.addWidget(table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        copy = make_button(t("meta_apply_result_copy"))
        copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self._details_text))
        backups = make_button(t("meta_apply_result_open_backups"))
        backups.clicked.connect(self._open_backup_manager)
        actions.addWidget(copy)
        actions.addWidget(backups)
        if kind == "partial":
            blockers = make_button(t("meta_apply_result_resolve_blocker"))
            blockers.clicked.connect(self._open_external_blockers)
            actions.addWidget(blockers)
        actions.addStretch()
        root.addLayout(actions)

        close = make_button(t("close"), "primary")
        close.clicked.connect(self.accept)
        root.addWidget(make_footer(close))

    def _open_backup_manager(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        from ui.dialogs.backup_manager_dialog import BackupManagerDialog
        from utils.paths import get_tag_backup_dir
        BackupManagerDialog(
            get_tag_backup_dir(),
            restore_callback=parent.restore_requested.emit,
            undo_callback=parent.undo_applied_requested.emit,
            parent=parent,
        ).exec()

    def _open_external_blockers(self) -> None:
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "_select_inspector_tool"):
            parent._select_inspector_tool(14)
        self.accept()


class MovePathDialog(StyledDialog):
    """In-app destination chooser matching the prototype's Move modal."""

    def __init__(self, path, destinations, parent=None, *, item_count: int = 1) -> None:
        super().__init__(parent, minimum_size=(430, 250), resize_to=(520, 300))
        mark_tag_editor_dialog(self)
        self.destination = None
        self.setWindowTitle(t("meta_move_dialog_title"))
        root = make_root_layout(self, margins=(16, 14, 16, 10), spacing=10)
        add_header(root, t("meta_move_dialog_title"), t("meta_move_dialog_subtitle"),
                   icon=FluentIcon.FOLDER.icon())
        section = make_section(t("meta_move_destination_heading"))
        section_lay = section_layout(section)
        current = QLabel(
            path.name if item_count == 1 else t("meta_move_items_count", n=item_count))
        current.setObjectName("tagDialogFormValue")
        current.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        section_lay.addWidget(current)
        self._destinations = list(destinations)
        self._combo = QComboBox()
        for destination in self._destinations:
            self._combo.addItem(str(destination), destination)
        self._combo.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        section_lay.addWidget(self._combo)
        root.addWidget(section)
        note = QLabel(t("meta_move_safety_note"))
        note.setObjectName("dialogHint")
        note.setWordWrap(True)
        root.addWidget(note)
        cancel = make_button(t("cancel_btn"), "cancel")
        cancel.clicked.connect(self.reject)
        move = make_button(t("meta_move_menu"), "primary")
        move.clicked.connect(self._accept_destination)
        move.setEnabled(bool(self._destinations))
        root.addWidget(make_footer(cancel, move))

    def _accept_destination(self) -> None:
        self.destination = self._combo.currentData()
        if self.destination is not None:
            self.accept()


class PropertiesDialog(StyledDialog):
    """Read-only file facts in the prototype's properties-modal structure."""

    def __init__(
        self,
        files,
        parent=None,
        *,
        open_callback=None,
        reveal_callback=None,
        copy_callback=None,
    ) -> None:
        super().__init__(parent, minimum_size=(560, 360), resize_to=(680, 500))
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("meta_properties"))
        root = make_root_layout(self, margins=(16, 14, 16, 10), spacing=10)
        add_header(
            root, t("meta_properties"), t("meta_properties_dialog_subtitle"),
            icon=FluentIcon.INFO.icon(),
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        for filename, rows in files:
            section = make_section(filename)
            section_lay = section_layout(section)
            for label_text, value_text in rows:
                row = QHBoxLayout()
                row.setSpacing(12)
                label = QLabel(label_text)
                label.setObjectName("tagDialogFormLabel")
                value = QLabel(str(value_text))
                value.setObjectName("tagDialogFormValue")
                value.setWordWrap(True)
                value.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                    | Qt.TextInteractionFlag.TextSelectableByKeyboard)
                if label_text == t("meta_property_path"):
                    value.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
                    value.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                row.addWidget(label, 0)
                row.addWidget(value, 1)
                section_lay.addLayout(row)
            content_layout.addWidget(section)
        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        for text_key, callback in (
            ("meta_property_open", open_callback),
            ("meta_property_reveal", reveal_callback),
            ("meta_property_copy_path", copy_callback),
        ):
            if callback is None:
                continue
            button = make_button(t(text_key))
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        root.addLayout(actions)

        close = make_button(t("close"), "primary")
        close.clicked.connect(self.accept)
        root.addWidget(make_footer(close))
