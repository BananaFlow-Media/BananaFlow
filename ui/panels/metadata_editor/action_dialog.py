"""Phase 9 Actions, Templates and Presets production dialog.

The dialog evaluates immutable previews and only adds accepted results to the
Phase 8 Change Set.  It never writes tags or renames files.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon

from core.tag_action_presets import PresetStore, TagActionPreset
from core.tag_action_service import ActionPreview, TagActionService
from core.tag_actions import ActionParameter, ActionResultStatus, TagAction, builtin_registry
from ui.dialogs.styled_dialog import (
    StyledDialog,
    add_header,
    confirm,
    get_text,
    make_button,
    make_footer,
    make_root_layout,
    make_section,
    section_layout,
    show_warning,
)
from ui.i18n import t
from utils.paths import get_tag_action_presets_path

from .action_diagnostics import format_action_diagnostic
from .shared import mark_tag_editor_dialog


_FIELD_LABEL_KEYS = {
    "title": "meta_field_title",
    "artist": "meta_field_artist",
    "album": "meta_field_album",
    "album_artist": "meta_field_album_artist",
    "track_num": "meta_field_track_num",
    "disc_num": "meta_field_disc_num",
    "year": "meta_field_year",
    "genre": "meta_field_genre",
    "comment": "meta_field_comment",
    "composer": "meta_field_composer",
    "filename": "meta_field_filename",
}
_PARAMETER_LABEL_KEYS = {
    key: "meta_action_param_" + key for key in (
        "template", "overwrite", "sanitize", "strip_numbering", "field", "value",
        "find", "replace", "case_sensitive", "mode", "start", "step",
        "smart_brackets", "remove_domains", "remove_emojis", "fix_spaces",
        "remove_web_junk", "remove_hebrew", "fix_punctuation",
    )
}
_CHOICE_LABEL_KEYS = {
    key: "meta_action_choice_" + key for key in (
        "title", "artist", "album", "album_artist", "genre", "year", "comment",
        "upper", "lower", "sentence",
    )
}
_STATUS_LABEL_KEYS = {
    ActionResultStatus.CHANGED: "meta_action_status_changed",
    ActionResultStatus.NO_OP: "meta_action_status_no_op",
    ActionResultStatus.SKIPPED: "meta_action_status_skipped",
    ActionResultStatus.UNSUPPORTED: "meta_action_status_unsupported",
    ActionResultStatus.WARNING: "meta_action_status_warning",
    ActionResultStatus.BLOCKER: "meta_action_status_blocker",
}
_STORE_DIAGNOSTIC_KEYS = {
    "preset_store_corrupt": "meta_preset_store_corrupt",
    "preset_store_unsupported": "meta_preset_store_unsupported",
    "preset_store_migrated": "meta_preset_store_migrated",
}


class TagActionDialog(StyledDialog):
    """Preview and add a declarative action or saved workflow."""

    def __init__(self, workspace, *, active_folder: Path | None = None, parent=None,
                 preset_path: Path | None = None, accept_preview=None,
                 open_preset_transfer=None, initial_action_id: str = "") -> None:
        super().__init__(parent, minimum_size=(860, 620), resize_to=(1040, 720))
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("meta_action_engine_title"))
        self.setAccessibleName(t("meta_action_engine_title"))
        self._workspace = workspace
        self._active_folder = active_folder
        self._accept_preview = accept_preview
        self._open_preset_transfer = open_preset_transfer
        self._registry = builtin_registry()
        self._service = TagActionService(self._registry)
        self._store = PresetStore(
            preset_path or get_tag_action_presets_path(),
            self._registry,
        )
        custom, self._store_diagnostic = self._store.load()
        self._custom_presets = custom
        self._preview: ActionPreview | None = None
        self._preview_error = ""
        self._parameter_widgets: dict[str, QWidget] = {}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self.refresh_preview)

        root = make_root_layout(self, margins=(18, 16, 18, 14), spacing=10)
        add_header(
            root,
            t("meta_action_engine_title"),
            t("meta_action_engine_subtitle"),
            icon=FluentIcon.TAG.icon(),
        )

        self._tabs = QTabWidget()
        self._tabs.setAccessibleName(t("meta_action_kind_accessible"))
        self._action_combo = self._make_definition_tab("action")
        self._template_combo = self._make_definition_tab("template")
        self._preset_combo = self._make_preset_tab()
        self._tabs.currentChanged.connect(self._source_changed)
        root.addWidget(self._tabs)

        scope_row = QHBoxLayout()
        scope_label = QLabel(t("meta_action_scope"))
        self._scope_combo = QComboBox()
        self._scope_combo.setAccessibleName(t("meta_action_scope"))
        for value, key in (
            ("current", "meta_scope_current"),
            ("selected", "meta_scope_selected"),
            ("visible", "meta_scope_visible"),
            ("active_folder", "meta_scope_active_folder"),
        ):
            self._scope_combo.addItem(t(key), value)
        self._scope_combo.setCurrentIndex(1)
        self._scope_combo.currentIndexChanged.connect(self._schedule_preview)
        scope_row.addWidget(scope_label)
        scope_row.addWidget(self._scope_combo, 1)
        root.addLayout(scope_row)

        self._parameter_section = make_section(t("meta_action_parameters"))
        self._parameter_form = QFormLayout()
        self._parameter_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        section_layout(self._parameter_section).addLayout(self._parameter_form)
        root.addWidget(self._parameter_section)

        summary_row = QHBoxLayout()
        self._summary_labels: dict[str, QLabel] = {}
        for name, key in (
            ("targets", "meta_action_targets"),
            ("supported", "meta_action_supported"),
            ("changed", "meta_action_expected_changes"),
            ("skipped", "meta_action_skipped"),
            ("blockers", "meta_action_blockers"),
        ):
            label = QLabel(t(key, n=0))
            label.setAccessibleName(t(key, n=0))
            self._summary_labels[name] = label
            summary_row.addWidget(label)
        summary_row.addStretch()
        self._changed_only = QCheckBox(t("meta_action_changed_only"))
        self._changed_only.setAccessibleName(t("meta_action_changed_only"))
        self._changed_only.toggled.connect(self._populate_preview_table)
        summary_row.addWidget(self._changed_only)
        root.addLayout(summary_row)

        self._preview_table = QTableWidget(0, 6)
        self._preview_table.setHorizontalHeaderLabels([
            t("meta_action_col_file"), t("meta_action_col_field"),
            t("meta_action_col_old"), t("meta_action_col_new"),
            t("meta_action_col_status"), t("meta_action_col_details"),
        ])
        self._preview_table.setAccessibleName(t("meta_action_preview_accessible"))
        self._preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._preview_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.horizontalHeader().setStretchLastSection(True)
        self._preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._preview_table, 1)

        self._preview_btn = make_button(t("meta_action_preview"), "secondary", icon=FluentIcon.VIEW.icon())
        self._preview_btn.setAccessibleName(t("meta_action_preview"))
        self._preview_btn.clicked.connect(self.refresh_preview)
        self._edit_btn = make_button(t("meta_action_back_parameters"), "secondary", icon=FluentIcon.EDIT.icon())
        self._edit_btn.clicked.connect(self._focus_parameters)
        cancel_btn = make_button(t("cancel_btn"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        self._accept_btn = make_button(t("meta_action_add_pending"), "primary", icon=FluentIcon.ADD.icon())
        self._accept_btn.setAccessibleName(t("meta_action_add_pending"))
        self._accept_btn.clicked.connect(self.accept_preview)
        root.addWidget(make_footer(cancel_btn, self._edit_btn, self._accept_btn, leading=(self._preview_btn,)))

        self._select_initial_action(initial_action_id)
        self._source_changed()
        if self._store_diagnostic:
            self._store_status.setText(t(_STORE_DIAGNOSTIC_KEYS[self._store_diagnostic]))

    def _select_initial_action(self, action_id: str) -> None:
        """Land on a specific action when a Tools row asked for one.

        Both definition tabs are searched: the caller names an action, and
        which tab it lives on (plain action or template) is this dialog's
        business, not the caller's.
        """
        if not action_id:
            return
        for tab_index, combo in ((0, self._action_combo), (1, self._template_combo)):
            index = combo.findData(action_id)
            if index >= 0:
                self._tabs.setCurrentIndex(tab_index)
                combo.setCurrentIndex(index)
                return

    def _make_definition_tab(self, kind: str) -> QComboBox:
        page = QWidget()
        row = QHBoxLayout(page)
        row.setContentsMargins(8, 8, 8, 8)
        label = QLabel(t("meta_action_select" if kind == "action" else "meta_template_select"))
        combo = QComboBox()
        combo.setAccessibleName(label.text())
        actions = [action for action in self._registry.actions() if (action.category == "template") == (kind == "template")]
        for action in actions:
            combo.addItem(t(action.name_key), action.id)
            combo.setItemData(combo.count() - 1, t(action.description_key), Qt.ItemDataRole.ToolTipRole)
        combo.currentIndexChanged.connect(self._source_changed)
        row.addWidget(label)
        row.addWidget(combo, 1)
        self._tabs.addTab(page, t("meta_actions_tab" if kind == "action" else "meta_templates_tab"))
        return combo

    def _make_preset_tab(self) -> QComboBox:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        label = QLabel(t("meta_preset_select"))
        combo = QComboBox()
        self._preset_combo = combo
        combo.setAccessibleName(t("meta_preset_select"))
        combo.currentIndexChanged.connect(self._source_changed)
        row.addWidget(label)
        row.addWidget(combo, 1)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        self._preset_save_btn = make_button(t("meta_preset_save_as"), "secondary")
        self._preset_update_btn = make_button(t("meta_preset_update"), "secondary")
        self._preset_rename_btn = make_button(t("meta_preset_rename"), "secondary")
        self._preset_duplicate_btn = make_button(t("meta_preset_duplicate"), "secondary")
        self._preset_delete_btn = make_button(t("meta_preset_delete"), "secondary")
        self._preset_reset_btn = make_button(t("meta_preset_reset_builtins"), "secondary")
        self._preset_transfer_btn = make_button(t("meta_io_preset_transfer"), "secondary")
        self._preset_transfer_btn.setObjectName("metadataIOPresetTransfer")
        self._preset_save_btn.clicked.connect(self._save_as_preset)
        self._preset_update_btn.clicked.connect(self._update_preset)
        self._preset_rename_btn.clicked.connect(self._rename_preset)
        self._preset_duplicate_btn.clicked.connect(self._duplicate_preset)
        self._preset_delete_btn.clicked.connect(self._delete_preset)
        self._preset_reset_btn.clicked.connect(self._reload_presets)
        if self._open_preset_transfer is not None:
            self._preset_transfer_btn.clicked.connect(self._open_preset_transfer)
        for button in (self._preset_save_btn, self._preset_update_btn, self._preset_rename_btn, self._preset_duplicate_btn,
                       self._preset_delete_btn, self._preset_reset_btn):
            buttons.addWidget(button)
        if self._open_preset_transfer is not None:
            buttons.addWidget(self._preset_transfer_btn)
        buttons.addStretch()
        layout.addLayout(buttons)
        self._store_status = QLabel("")
        self._store_status.setWordWrap(True)
        layout.addWidget(self._store_status)
        self._tabs.addTab(page, t("meta_presets_tab"))
        self._reload_presets()
        return combo

    def _all_presets(self) -> list[TagActionPreset]:
        return [*PresetStore.builtins(), *self._custom_presets]

    def _preset_label(self, preset: TagActionPreset) -> str:
        builtin_keys = {
            "builtin.filename.artist-title.v1": "meta_preset_builtin_artist_title",
            "builtin.filename.track-artist-title.v1": "meta_preset_builtin_track_artist_title",
            "builtin.tags.artist-title.v1": "meta_preset_builtin_parse_artist_title",
        }
        return t(builtin_keys[preset.id]) if preset.id in builtin_keys else preset.name

    def _reload_presets(self) -> None:
        if not hasattr(self, "_preset_combo"):
            return
        combo = self._preset_combo
        current_id = combo.currentData() if combo.count() else None
        combo.blockSignals(True)
        combo.clear()
        for preset in self._all_presets():
            combo.addItem(self._preset_label(preset), preset.id)
        index = combo.findData(current_id)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
        self._source_changed()

    def _selected_preset(self) -> TagActionPreset | None:
        preset_id = self._preset_combo.currentData() if hasattr(self, "_preset_combo") else None
        return next((preset for preset in self._all_presets() if preset.id == preset_id), None)

    def _selected_action(self) -> TagAction | None:
        if self._tabs.currentIndex() == 0:
            action_id = self._action_combo.currentData()
        elif self._tabs.currentIndex() == 1:
            action_id = self._template_combo.currentData()
        else:
            preset = self._selected_preset()
            action_id = preset.action_id if preset and not preset.steps else None
        return self._registry.get(action_id) if action_id else None

    def _source_changed(self, *_args) -> None:
        action = self._selected_action() if hasattr(self, "_tabs") else None
        preset = self._selected_preset() if hasattr(self, "_preset_combo") else None
        defaults = dict(preset.parameters) if preset and not preset.steps else {}
        self._rebuild_parameter_form(action, defaults)
        if hasattr(self, "_preset_rename_btn"):
            editable = bool(preset and not preset.builtin)
            self._preset_update_btn.setEnabled(editable and action is not None)
            self._preset_rename_btn.setEnabled(editable)
            self._preset_delete_btn.setEnabled(editable)
            self._preset_duplicate_btn.setEnabled(bool(preset))
        self._schedule_preview()

    def _clear_form(self) -> None:
        while self._parameter_form.rowCount():
            self._parameter_form.removeRow(0)
        self._parameter_widgets.clear()

    def _rebuild_parameter_form(self, action: TagAction | None, defaults: dict[str, object]) -> None:
        if not hasattr(self, "_parameter_form"):
            return
        self._clear_form()
        if action is None:
            note = QLabel(t("meta_preset_sequence_note"))
            note.setWordWrap(True)
            self._parameter_form.addRow(note)
            return
        for parameter in action.parameters:
            widget = self._parameter_widget(parameter, defaults.get(parameter.id, parameter.default))
            widget.setAccessibleName(t(_PARAMETER_LABEL_KEYS[parameter.id]))
            self._parameter_widgets[parameter.id] = widget
            self._parameter_form.addRow(t(_PARAMETER_LABEL_KEYS[parameter.id]), widget)

    def _parameter_widget(self, parameter: ActionParameter, value: object) -> QWidget:
        if parameter.kind == "boolean":
            widget = QCheckBox()
            widget.setChecked(bool(value))
            widget.toggled.connect(self._schedule_preview)
            return widget
        if parameter.kind == "choice":
            widget = QComboBox()
            for choice in parameter.choices:
                widget.addItem(t(_CHOICE_LABEL_KEYS[choice]), choice)
            widget.setCurrentIndex(max(0, widget.findData(value)))
            widget.currentIndexChanged.connect(self._schedule_preview)
            return widget
        if parameter.kind == "integer":
            widget = QSpinBox()
            widget.setRange(-999999, 999999)
            widget.setValue(int(value or 0))
            widget.valueChanged.connect(self._schedule_preview)
            return widget
        widget = QLineEdit(str(value or ""))
        if parameter.kind == "template":
            widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            widget.setAlignment(Qt.AlignmentFlag.AlignLeft)
            widget.setPlaceholderText(t("meta_template_example"))
        widget.textChanged.connect(self._schedule_preview)
        return widget

    def _parameters(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key, widget in self._parameter_widgets.items():
            if isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                values[key] = widget.currentData()
            elif isinstance(widget, QSpinBox):
                values[key] = widget.value()
            elif isinstance(widget, QLineEdit):
                values[key] = widget.text()
        return values

    def _schedule_preview(self, *_args) -> None:
        if hasattr(self, "_preview_timer"):
            self._preview_timer.start()

    def _scope_arguments(self) -> dict[str, object]:
        current_tracks = self._workspace.selected_tracks() or self._workspace.visible_tracks() or list(self._workspace.tracks)
        current_id = self._workspace.item_id(current_tracks[0]) if current_tracks else None
        return {
            "scope": self._scope_combo.currentData(),
            "current_item_id": current_id,
            "active_folder": self._active_folder,
        }

    def refresh_preview(self) -> None:
        if not hasattr(self, "_scope_combo"):
            return
        self._preview_error = ""
        try:
            if self._tabs.currentIndex() == 2:
                preset = self._selected_preset()
                if preset is None:
                    self._preview = None
                elif preset.steps:
                    self._preview = self._service.preview_sequence(
                        self._workspace, preset.normalized_steps(), **self._scope_arguments()
                    )
                else:
                    self._preview = self._service.preview(
                        self._workspace, preset.action_id, parameters=preset.parameters,
                        **self._scope_arguments(),
                    )
            else:
                action = self._selected_action()
                self._preview = None if action is None else self._service.preview(
                    self._workspace, action.id, parameters=self._parameters(),
                    **self._scope_arguments(),
                )
        except (KeyError, ValueError) as exc:
            self._preview = None
            self._preview_error = str(exc)
        self._update_counts()
        self._populate_preview_table()

    def _update_counts(self) -> None:
        preview = self._preview
        deltas = preview.deltas if preview else ()
        blocked_ids = {identity for identity, _ in preview.blocked} if preview else set()
        supported = sum(delta.status is not ActionResultStatus.UNSUPPORTED for delta in deltas)
        changed = sum(delta.status is ActionResultStatus.CHANGED and delta.item_id not in blocked_ids for delta in deltas)
        skipped = sum(delta.status in {ActionResultStatus.SKIPPED, ActionResultStatus.UNSUPPORTED, ActionResultStatus.NO_OP} for delta in deltas)
        blockers = len(blocked_ids) + sum(delta.status in {ActionResultStatus.BLOCKER, ActionResultStatus.WARNING} for delta in deltas)
        for name, key, value in (
            ("targets", "meta_action_targets", len(deltas)),
            ("supported", "meta_action_supported", supported),
            ("changed", "meta_action_expected_changes", changed),
            ("skipped", "meta_action_skipped", skipped),
            ("blockers", "meta_action_blockers", blockers),
        ):
            self._summary_labels[name].setText(t(key, n=value))
        self._accept_btn.setEnabled(changed > 0 and blockers == 0)

    @staticmethod
    def _display_value(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (tuple, list)):
            return "; ".join(str(part) for part in value)
        return str(value)

    @staticmethod
    def _ltr_item(value: str) -> QTableWidgetItem:
        item = QTableWidgetItem(value)
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _status_text(self, status: ActionResultStatus, blocked: str = "") -> str:
        if blocked:
            return t("meta_action_status_collision" if "collision" in blocked.lower() else "meta_action_status_blocker")
        return t(_STATUS_LABEL_KEYS[status])

    def _populate_preview_table(self, *_args) -> None:
        preview = self._preview
        rows: list[tuple[str, str, str, str, str, str]] = []
        blocked = dict(preview.blocked) if preview else {}
        if preview:
            for delta in preview.deltas:
                if self._changed_only.isChecked() and delta.status is not ActionResultStatus.CHANGED:
                    continue
                item = self._workspace.track_for_id(delta.item_id)
                if item is None:
                    continue
                filename = item.proposed_filename or item.path.name
                status = self._status_text(delta.status, blocked.get(delta.item_id, ""))
                detail = self._detail_text(
                    blocked.get(delta.item_id, "") or delta.diagnostic,
                    delta.warnings,
                    filename,
                )
                effective = item.proposed.effective_tags(item.original)
                if delta.fields:
                    for field, new_value in delta.fields.items():
                        rows.append((filename, t(_FIELD_LABEL_KEYS.get(field, "meta_field_value")),
                                     self._display_value(effective.field_value(field)),
                                     self._display_value(new_value), status, detail))
                if delta.filename:
                    rows.append((filename, t("meta_field_filename"), filename, delta.filename, status, detail))
                if not delta.fields and not delta.filename:
                    rows.append((filename, "", "", "", status, detail))
        if not rows and self._preview_error:
            rows.append(("", "", "", "", t("meta_action_status_blocker"),
                         format_action_diagnostic(self._preview_error)))
        self._preview_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self._preview_table.setItem(row, column, self._ltr_item(value))

    @staticmethod
    def _detail_text(code: str, warnings: tuple[str, ...], filename: str) -> str:
        if code:
            return format_action_diagnostic(code, filename=filename)
        return " ".join(format_action_diagnostic(warning, filename=filename) for warning in warnings)

    def _focus_parameters(self) -> None:
        first = next(iter(self._parameter_widgets.values()), self._scope_combo)
        first.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def accept_preview(self) -> None:
        if self._preview is None:
            return
        accepted = (
            self._accept_preview(self._preview)
            if self._accept_preview is not None
            else self._service.accept(self._workspace, self._preview)
        )
        if not accepted:
            show_warning(self, t("meta_action_stale_title"), t("meta_action_stale_body"))
            self.refresh_preview()
            return
        self.accept()

    def _save_custom(self) -> None:
        self._store.save(self._custom_presets)
        self._store_status.setText(t("meta_preset_saved"))
        self._reload_presets()

    def _save_as_preset(self) -> None:
        action = self._selected_action()
        if action is None:
            return
        name, ok = get_text(self, t("meta_preset_save_as"), t("meta_preset_name_prompt"))
        if ok and name.strip():
            self._custom_presets.append(PresetStore.create(name, action.id, self._parameters()))
            self._save_custom()

    def _rename_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None or preset.builtin:
            return
        name, ok = get_text(self, t("meta_preset_rename"), t("meta_preset_name_prompt"), text=preset.name)
        if ok and name.strip():
            self._custom_presets = [PresetStore.rename(value, name) if value.id == preset.id else value
                                    for value in self._custom_presets]
            self._save_custom()

    def _update_preset(self) -> None:
        preset = self._selected_preset()
        action = self._selected_action()
        if preset is None or preset.builtin or action is None:
            return
        self._custom_presets = [
            PresetStore.update(value, action_id=action.id, parameters=self._parameters())
            if value.id == preset.id else value
            for value in self._custom_presets
        ]
        self._save_custom()

    def _duplicate_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        name, ok = get_text(self, t("meta_preset_duplicate"), t("meta_preset_name_prompt"),
                            text=t("meta_preset_copy_name", name=self._preset_label(preset)))
        if ok and name.strip():
            self._custom_presets.append(PresetStore.duplicate(preset, name))
            self._save_custom()

    def _delete_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None or preset.builtin:
            return
        if confirm(self, t("meta_preset_delete"), t("meta_preset_delete_confirm", name=preset.name),
                   accept_text=t("meta_preset_delete"), cancel_text=t("cancel_btn"), danger=True):
            self._custom_presets = PresetStore.delete(self._custom_presets, preset.id)
            self._save_custom()
