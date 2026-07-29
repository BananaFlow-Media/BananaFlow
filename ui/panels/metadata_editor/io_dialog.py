"""Production Phase 12 Import / Export hub for the Tag Editor."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from core.metadata_csv import (
    CANONICAL_FIELDS, BlankValuePolicy, CsvColumnMapping, CsvIdentityMapping,
    CsvIdentityRole, ImportResultState, ParsedCsv, app_generated_mapping,
)
from core.metadata_io import IOScope, MetadataIOError
from core.preset_transfer import (
    PresetConflictDecision, PresetConflictPolicy, accept_preset_import, build_transfer_package,
    export_transfer_package, preview_preset_import,
)
from core.tag_action_presets import PresetStore
from core.tag_actions import builtin_registry
from qfluentwidgets import FluentIcon
from ui.dialogs.styled_dialog import add_header, make_footer
from ui.i18n import current_language, t
from utils.paths import get_tag_action_presets_path
from .shared import mark_tag_editor_dialog


_OPERATIONS = (
    ("metadata_export", "meta_io_export_metadata"),
    ("metadata_import", "meta_io_import_metadata"),
    ("change_report", "meta_io_export_change_report"),
    ("problems_report", "meta_io_export_problems_report"),
    ("playlist", "meta_io_export_playlist"),
    ("preset_export", "meta_io_export_presets"),
    ("preset_import", "meta_io_import_presets"),
)


class MetadataIODialog(QDialog):
    """One coherent hub; each operation exposes its complete explicit steps."""

    def __init__(self, workspace, *, callbacks: dict[str, object], root: Path | None,
                 ordered_item_ids=(), problem_issue_ids=(), parent=None,
                 preset_path: Path | None = None) -> None:
        super().__init__(parent)
        mark_tag_editor_dialog(self)
        self.setWindowTitle(t("meta_io_title"))
        self.setAccessibleName(t("meta_io_title"))
        self.resize(980, 680)
        self.setMinimumSize(820, 560)
        self._workspace = workspace
        self._callbacks = callbacks
        self._root = root
        self._ordered_item_ids = tuple(ordered_item_ids)
        self._problem_issue_ids = tuple(problem_issue_ids)
        self._parsed: ParsedCsv | None = None
        self._import_preview = None
        self._import_filter_connected = False
        self._export_plan = None
        self._export_plan_options = None
        self._playlist_plan = None
        self._playlist_plan_options = None
        self._preset_import_preview = None
        self._active_request_id = ""
        self._preset_store = PresetStore(
            preset_path or get_tag_action_presets_path(),
            builtin_registry(),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 10)
        layout.setSpacing(10)
        add_header(layout, t("meta_io_title"), t("meta_io_subtitle"),
                   icon=FluentIcon.DOCUMENT.icon())
        splitter = QSplitter(Qt.Horizontal, self)
        self.operation_list = QListWidget(splitter)
        self.operation_list.setObjectName("metadataIOOperations")
        self.operation_list.setAccessibleName(t("meta_io_operation_accessible"))
        self.operation_list.setFixedWidth(240)
        self.pages = QStackedWidget(splitter)
        splitter.addWidget(self.operation_list); splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        builders = {
            "metadata_export": self._metadata_export_page,
            "metadata_import": self._metadata_import_page,
            "change_report": lambda: self._report_page("change"),
            "problems_report": lambda: self._report_page("problems"),
            "playlist": self._playlist_page,
            "preset_export": lambda: self._preset_page("export"),
            "preset_import": lambda: self._preset_page("import"),
        }
        for operation, key in _OPERATIONS:
            self.operation_list.addItem(t(key))
            page = builders[operation]()
            page.setObjectName(f"metadataIOPage_{operation}")
            self.pages.addWidget(page)
        self.operation_list.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.operation_list.setCurrentRow(0)

        self.status_label = QLabel(t("meta_io_state_initial"))
        self.status_label.setObjectName("metadataIOStatus")
        self.status_label.setWordWrap(True)
        close = QPushButton(t("close")); close.setAccessibleName(t("close")); close.clicked.connect(self.reject)
        layout.addWidget(make_footer(close, leading=(self.status_label,)))

    @staticmethod
    def _scope_combo(*, playlist: bool = False) -> QComboBox:
        combo = QComboBox()
        combo.setAccessibleName(t("meta_io_scope"))
        combo.addItem(t("meta_io_scope_selected"), IOScope.SELECTED.value)
        combo.addItem(t("meta_io_scope_visible"), IOScope.VISIBLE.value)
        if not playlist:
            combo.addItem(t("meta_io_scope_changed"), IOScope.CHANGED.value)
        combo.addItem(t("meta_io_scope_all"), IOScope.ALL_LOADED.value)
        return combo

    @staticmethod
    def _destination_row(parent, *, suffix: str, save: bool = True) -> tuple[QWidget, QLineEdit]:
        container = QWidget(parent); row = QHBoxLayout(container); row.setContentsMargins(0, 0, 0, 0)
        line = QLineEdit(container); line.setAccessibleName(t("meta_io_destination"))
        browse = QPushButton(t("browse"), container); browse.setAccessibleName(t("meta_io_choose_destination"))
        def choose():
            if save:
                value, _ = QFileDialog.getSaveFileName(parent, t("meta_io_choose_destination"), "", suffix)
            else:
                value, _ = QFileDialog.getOpenFileName(parent, t("meta_io_choose_source"), "", suffix)
            if value: line.setText(value)
        browse.clicked.connect(choose); row.addWidget(line, 1); row.addWidget(browse)
        return container, line

    @staticmethod
    def _numbered_group(number: int, title_key: str, parent) -> tuple[QGroupBox, QFormLayout]:
        group = QGroupBox(f"{number}. {t(title_key)}", parent)
        form = QFormLayout(group); form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return group, form

    def _metadata_export_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        group, form = self._numbered_group(1, "meta_io_export_options", page)
        self.export_scope = self._scope_combo(); form.addRow(t("meta_io_scope"), self.export_scope)
        self.export_source = QComboBox(); self.export_source.setAccessibleName(t("meta_io_value_source"))
        self.export_source.addItem(t("meta_io_effective_values"), "effective")
        self.export_source.addItem(t("meta_io_original_values"), "original")
        form.addRow(t("meta_io_value_source"), self.export_source)
        self.export_fields = QListWidget(); self.export_fields.setAccessibleName(t("meta_io_fields"))
        self.export_fields.setMaximumHeight(150)
        for field_name in CANONICAL_FIELDS:
            field_item = QListWidgetItem(field_name)
            field_item.setData(Qt.UserRole, field_name)
            field_item.setFlags(field_item.flags() | Qt.ItemIsUserCheckable)
            field_item.setCheckState(Qt.Checked)
            self.export_fields.addItem(field_item)
        form.addRow(t("meta_io_fields"), self.export_fields)
        self.export_encoding = QComboBox(); self.export_encoding.setAccessibleName(t("meta_io_encoding"))
        for label, value in (("UTF-8 BOM", "utf_8_bom"), ("UTF-8", "utf_8"),
                             ("UTF-16 LE", "utf_16_le")):
            self.export_encoding.addItem(label, value)
        form.addRow(t("meta_io_encoding"), self.export_encoding)
        self.export_delimiter = QComboBox(); self.export_delimiter.setAccessibleName(t("meta_io_delimiter"))
        for key, value in (("meta_io_delimiter_comma", ","), ("meta_io_delimiter_semicolon", ";"),
                           ("meta_io_delimiter_tab", "\t")):
            self.export_delimiter.addItem(t(key), value)
        form.addRow(t("meta_io_delimiter"), self.export_delimiter)
        self.export_absolute = QCheckBox(t("meta_io_include_absolute_paths"))
        self.export_absolute.setAccessibleName(t("meta_io_include_absolute_paths")); form.addRow(self.export_absolute)
        layout.addWidget(group)
        destination_group, destination_form = self._numbered_group(2, "meta_io_preview_destination", page)
        row, self.export_destination = self._destination_row(page, suffix="CSV (*.csv)")
        destination_form.addRow(t("meta_io_destination"), row)
        self.export_overwrite = QCheckBox(t("meta_io_overwrite")); destination_form.addRow(self.export_overwrite)
        self.export_preview = QLabel(t("meta_io_preview_not_ready")); self.export_preview.setWordWrap(True)
        destination_form.addRow(t("meta_io_preview"), self.export_preview)
        preview = QPushButton(t("meta_io_preview")); preview.setObjectName("metadataIOPreviewCsv")
        preview.setAccessibleName(t("meta_io_preview")); preview.clicked.connect(self._preview_metadata_export)
        destination_form.addRow(preview)
        start = QPushButton(t("meta_io_export")); start.setObjectName("metadataIOExportCsv")
        start.setAccessibleName(t("meta_io_export_metadata")); start.clicked.connect(self._start_metadata_export)
        destination_form.addRow(start); layout.addWidget(destination_group); layout.addStretch()
        return page

    def _metadata_export_options(self) -> dict:
        fields = tuple(self.export_fields.item(index).data(Qt.UserRole)
                       for index in range(self.export_fields.count())
                       if self.export_fields.item(index).checkState() == Qt.Checked)
        return {
            "scope": self.export_scope.currentData(), "value_source": self.export_source.currentData(),
            "encoding": self.export_encoding.currentData(), "delimiter": self.export_delimiter.currentData(),
            "include_absolute_paths": self.export_absolute.isChecked(),
            "fields": fields, "ordered_item_ids": self._ordered_item_ids,
        }

    def _preview_metadata_export(self) -> None:
        options = self._metadata_export_options()
        plan = self._callbacks["create_csv_export_plan"](options)
        if plan is None or not plan.rows:
            self._export_plan = None; self.status_label.setText(t("meta_io_empty_scope")); return
        self._export_plan = plan; self._export_plan_options = options
        self.export_preview.setText(t("meta_io_preview_rows", n=len(plan.rows), source=t(
            "meta_io_effective_values" if plan.value_source.value == "effective" else "meta_io_original_values")))
        self.status_label.setText(t("meta_io_preview_ready"))

    def _start_metadata_export(self) -> None:
        if self._export_plan is None or self._export_plan_options != self._metadata_export_options():
            self.status_label.setText(t("meta_io_preview_first")); return
        destination = self._validated_destination(self.export_destination.text(), self.export_overwrite.isChecked())
        if destination is None: return
        worker = self._callbacks["start_csv_export"](self._export_plan, destination,
                                                      overwrite=self.export_overwrite.isChecked())
        self._active_request_id = self._export_plan.identity.request_id
        self.status_label.setText(t("meta_io_state_loading"))

    def _metadata_import_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        source_group, form = self._numbered_group(1, "meta_io_source_format", page)
        row, self.import_source = self._destination_row(page, suffix="CSV (*.csv);;All files (*)", save=False)
        form.addRow(t("meta_io_source_file"), row)
        self.import_encoding = QComboBox(); self.import_encoding.setAccessibleName(t("meta_io_encoding"))
        self.import_encoding.addItem(t("meta_io_auto_detect"), None)
        for label, value in (("UTF-8", "utf_8"), ("UTF-8 BOM", "utf_8_bom"),
                             ("Windows-1255", "windows_1255"), ("Windows-1252", "windows_1252"),
                             ("UTF-16 LE", "utf_16_le"), ("UTF-16 BE", "utf_16_be")):
            self.import_encoding.addItem(label, value)
        form.addRow(t("meta_io_encoding"), self.import_encoding)
        self.import_delimiter = QComboBox(); self.import_delimiter.setAccessibleName(t("meta_io_delimiter"))
        self.import_delimiter.addItem(t("meta_io_auto_detect"), None)
        for key, value in (("meta_io_delimiter_comma", ","), ("meta_io_delimiter_semicolon", ";"),
                           ("meta_io_delimiter_tab", "\t")):
            self.import_delimiter.addItem(t(key), value)
        form.addRow(t("meta_io_delimiter"), self.import_delimiter)
        load = QPushButton(t("meta_io_load_headers")); load.setObjectName("metadataIOLoadHeaders")
        load.clicked.connect(self._load_import_headers); form.addRow(load); layout.addWidget(source_group)

        mapping_group, mapping_form = self._numbered_group(2, "meta_io_mapping", page)
        self.import_scope = self._scope_combo(); mapping_form.addRow(t("meta_io_scope"), self.import_scope)
        self.mapping_table = QTableWidget(0, 2); self.mapping_table.setObjectName("metadataIOMappingTable")
        self.mapping_table.setAccessibleName(t("meta_io_field_mapping"))
        self.mapping_table.setHorizontalHeaderLabels([t("meta_io_csv_column"), t("meta_io_target")])
        mapping_form.addRow(self.mapping_table)
        self.blank_clear = QCheckBox(t("meta_io_blank_clears")); self.blank_clear.setAccessibleName(t("meta_io_blank_clears"))
        mapping_form.addRow(self.blank_clear)
        dry_run = QPushButton(t("meta_io_dry_run")); dry_run.setObjectName("metadataIODryRun")
        dry_run.clicked.connect(self._start_import_dry_run); mapping_form.addRow(dry_run)
        layout.addWidget(mapping_group)

        result_group, result_form = self._numbered_group(3, "meta_io_results", page)
        buttons = QHBoxLayout(); self.import_filter = QComboBox()
        self.import_filter.setAccessibleName(t("meta_io_result_filter"))
        self.import_filter.addItem(t("meta_io_filter_all"), "all")
        for state in (ImportResultState.CHANGE, ImportResultState.NO_OP, ImportResultState.UNMATCHED,
                      ImportResultState.AMBIGUOUS, ImportResultState.INVALID, ImportResultState.UNSUPPORTED,
                      ImportResultState.BLOCKED, ImportResultState.STALE_IDENTITY):
            self.import_filter.addItem(t(f"meta_io_state_{state.value}"), state.value)
        self.import_filter.addItem(t("meta_io_filter_selected"), "selected")
        self.import_filter.currentIndexChanged.connect(self._apply_import_filter)
        safe = QPushButton(t("meta_io_select_safe")); clear = QPushButton(t("meta_io_clear_selection"))
        safe.clicked.connect(self._select_safe_import); clear.clicked.connect(lambda: self._set_import_selection(False))
        buttons.addWidget(self.import_filter); buttons.addWidget(safe); buttons.addWidget(clear); buttons.addStretch(); result_form.addRow(buttons)
        self.import_results = QTableWidget(0, 6); self.import_results.setObjectName("metadataIOImportResults")
        self.import_results.setAccessibleName(t("meta_io_dry_run_results"))
        self.import_results.setHorizontalHeaderLabels([
            t("meta_io_selected"), t("meta_io_row"), t("meta_io_target"),
            t("meta_io_field"), t("meta_io_imported"), t("meta_io_state"),
        ])
        result_form.addRow(self.import_results)
        accept = QPushButton(t("meta_io_add_pending")); accept.setObjectName("metadataIOAcceptImport")
        accept.clicked.connect(self._accept_import); result_form.addRow(accept)
        layout.addWidget(result_group, 1)
        return page

    def _import_options(self) -> dict:
        return {"path": self.import_source.text().strip(), "encoding": self.import_encoding.currentData(),
                "delimiter": self.import_delimiter.currentData(), "scope": self.import_scope.currentData(),
                "source_identity": self._parsed.source if self._parsed is not None else None,
                "blank_policy": (BlankValuePolicy.CLEAR.value if self.blank_clear.isChecked()
                                  else BlankValuePolicy.NO_CHANGE.value),
                "ordered_item_ids": self._ordered_item_ids}

    def _load_import_headers(self) -> None:
        if not Path(self.import_source.text().strip()).is_file():
            self.status_label.setText(t("meta_io_source_missing")); return
        worker = self._callbacks["start_csv_header_preview"](self._import_options())
        self._active_request_id = worker.request_identity.request_id
        self.status_label.setText(t("meta_io_state_loading"))

    def _populate_mapping(self, parsed: ParsedCsv) -> None:
        self._parsed = parsed
        mapping, identity = app_generated_mapping(parsed.headers)
        self.mapping_table.setRowCount(len(parsed.headers))
        for row, header in enumerate(parsed.headers):
            item = QTableWidgetItem(header); item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.mapping_table.setItem(row, 0, item)
            combo = QComboBox(); combo.setAccessibleName(t("meta_io_mapping_for", column=header))
            combo.addItem(t("meta_io_ignore"), ("ignore", ""))
            combo.addItem(t("meta_io_identity_relative"), ("identity", CsvIdentityRole.RELATIVE_PATH.value))
            combo.addItem(t("meta_io_identity_absolute"), ("identity", CsvIdentityRole.ABSOLUTE_PATH.value))
            combo.addItem(t("meta_io_identity_filename"), ("identity", CsvIdentityRole.FILENAME.value))
            for field_name in CANONICAL_FIELDS:
                combo.addItem(field_name, ("field", field_name))
            auto = next(entry for entry in mapping if entry.source_column == header)
            if auto.identity_role:
                target = combo.findData(("identity", auto.identity_role.value))
            elif auto.target_field:
                target = combo.findData(("field", auto.target_field))
            else:
                target = 0
            combo.setCurrentIndex(max(0, target)); self.mapping_table.setCellWidget(row, 1, combo)
        self.status_label.setText(t("meta_io_headers_loaded", n=len(parsed.headers)))

    def _mapping_values(self):
        mappings = []; identity = None
        for row in range(self.mapping_table.rowCount()):
            header = self.mapping_table.item(row, 0).text()
            kind, value = self.mapping_table.cellWidget(row, 1).currentData()
            if kind == "identity":
                role = CsvIdentityRole(value); mappings.append(CsvColumnMapping(header, identity_role=role))
                identity = CsvIdentityMapping(header, role)
            elif kind == "field": mappings.append(CsvColumnMapping(header, target_field=value))
            else: mappings.append(CsvColumnMapping(header, ignored=True))
        return tuple(mappings), identity

    def _start_import_dry_run(self) -> None:
        if self._parsed is None:
            self.status_label.setText(t("meta_io_load_headers_first")); return
        mapping, identity = self._mapping_values()
        if identity is None:
            self.status_label.setText(t("meta_io_identity_required")); return
        options = self._import_options(); options.update({"mapping": mapping, "identity_mapping": identity})
        worker = self._callbacks["start_csv_import_preview"](options)
        self._active_request_id = worker.request_identity.request_id
        self.status_label.setText(t("meta_io_state_loading"))

    def _populate_import_results(self, preview) -> None:
        self._import_preview = preview
        display = [(row_result, change) for row_result in preview.rows
                   for change in (row_result.changes or (None,))]
        self.import_results.blockSignals(True)
        self.import_results.setRowCount(len(display))
        for row, (row_result, change) in enumerate(display):
            selected = QTableWidgetItem(); selected.setFlags(selected.flags() | Qt.ItemIsUserCheckable)
            selectable = bool(change and change.selectable)
            selected.setCheckState(Qt.Checked if selectable else Qt.Unchecked)
            selected.setData(Qt.UserRole, change.id if change else None)
            state = change.state if change else row_result.state
            selected.setData(int(Qt.UserRole) + 1, state.value)
            selected.setFlags(selected.flags() if selectable else selected.flags() & ~Qt.ItemIsEnabled)
            values = [selected, QTableWidgetItem(str(row_result.row_number)),
                      QTableWidgetItem(str(row_result.item_id or "")), QTableWidgetItem(change.field if change else ""),
                      QTableWidgetItem(str(change.imported_value if change and change.imported_value is not None else "")),
                      QTableWidgetItem(t(f"meta_io_state_{state.value}"))]
            for column, item in enumerate(values): self.import_results.setItem(row, column, item)
        self.import_results.blockSignals(False)
        if not self._import_filter_connected:
            self.import_results.itemChanged.connect(self._apply_import_filter)
            self._import_filter_connected = True
        self._apply_import_filter()
        self.status_label.setText(t("meta_io_dry_run_ready", changes=len(preview.safe_change_ids), rows=len(preview.rows)))

    def _apply_import_filter(self, *_args) -> None:
        if not hasattr(self, "import_filter"): return
        value = self.import_filter.currentData() or "all"
        for row in range(self.import_results.rowCount()):
            selected = self.import_results.item(row, 0)
            visible = (value == "all" or
                       (value == "selected" and selected.checkState() == Qt.Checked) or
                       selected.data(int(Qt.UserRole) + 1) == value)
            self.import_results.setRowHidden(row, not visible)

    def _set_import_selection(self, checked: bool) -> None:
        for row in range(self.import_results.rowCount()):
            item = self.import_results.item(row, 0)
            if item.flags() & Qt.ItemIsEnabled: item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _select_safe_import(self) -> None: self._set_import_selection(True)

    def _accept_import(self) -> None:
        if self._import_preview is None:
            self.status_label.setText(t("meta_io_dry_run_first")); return
        selected = [self.import_results.item(row, 0).data(Qt.UserRole)
                    for row in range(self.import_results.rowCount())
                    if self.import_results.item(row, 0).checkState() == Qt.Checked]
        result = self._callbacks["accept_csv_import"](self._import_preview, selected)
        self.status_label.setText(t("meta_io_import_success", fields=result.selected_cells,
                                    files=result.changed_items) if result.accepted
                                  else t("meta_io_import_failed"))

    def _report_page(self, report_type: str) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); group, form = self._numbered_group(1, "meta_io_report_options", page)
        scope = self._scope_combo() if report_type == "change" else QComboBox()
        if report_type == "problems":
            scope.addItem(t("meta_report_scope_all_issues"), "all")
            scope.addItem(t("meta_report_scope_filtered_issues"), "filtered")
        scope.setAccessibleName(t("meta_io_scope")); form.addRow(t("meta_io_scope"), scope)
        format_combo = QComboBox(); format_combo.addItem("HTML", "html"); format_combo.addItem("CSV", "csv")
        format_combo.setAccessibleName(t("meta_io_report_format")); form.addRow(t("meta_io_report_format"), format_combo)
        language = QComboBox(); language.addItem("English", "en"); language.addItem("עברית", "he")
        language.setCurrentIndex(1 if current_language() == "he" else 0); form.addRow(t("meta_io_language"), language)
        technical = QCheckBox(t("meta_io_include_technical_ids")); form.addRow(technical)
        absolute = QCheckBox(t("meta_io_include_absolute_paths"))
        absolute.setAccessibleName(t("meta_io_include_absolute_paths")); form.addRow(absolute)
        privacy_warning = QLabel(t("meta_io_absolute_paths_warning")); privacy_warning.setWordWrap(True)
        form.addRow(privacy_warning)
        spreadsheet = QCheckBox(t("meta_io_spreadsheet_safe")); form.addRow(spreadsheet)
        row, destination = self._destination_row(page, suffix="HTML (*.html);;CSV (*.csv)")
        form.addRow(t("meta_io_destination"), row)
        overwrite = QCheckBox(t("meta_io_overwrite")); form.addRow(overwrite)
        preview_label = QLabel(t("meta_io_preview_not_ready")); preview_label.setWordWrap(True)
        form.addRow(t("meta_io_preview"), preview_label)
        state = {"snapshot": None, "scope": None, "absolute": None}
        preview_button = QPushButton(t("meta_io_preview"))
        preview_button.setObjectName(f"metadataIOPreviewReport_{report_type}")
        start = QPushButton(t("meta_io_export")); start.setAccessibleName(t(
            "meta_io_export_change_report" if report_type == "change" else "meta_io_export_problems_report"))
        start.setObjectName(f"metadataIOExportReport_{report_type}")

        def make_preview():
            snapshot = (self._callbacks["create_change_report"](scope.currentData(),
                        ordered_item_ids=self._ordered_item_ids,
                        include_absolute_paths=absolute.isChecked()) if report_type == "change"
                        else self._callbacks["create_problems_report"](
                            issue_ids=(self._problem_issue_ids if scope.currentData() == "filtered" else None),
                            include_absolute_paths=absolute.isChecked()))
            if snapshot is None or (not snapshot.entries and report_type == "change"):
                state["snapshot"] = None; self.status_label.setText(t("meta_io_no_rows")); return
            state.update(snapshot=snapshot, scope=scope.currentData(), absolute=absolute.isChecked())
            preview_label.setText(
                t("meta_io_report_preview_ready", n=len(snapshot.entries)) + " " +
                t("meta_report_preview_absolute_on" if absolute.isChecked()
                  else "meta_report_preview_absolute_off"))
            self.status_label.setText(t("meta_io_preview_ready"))

        def run():
            snapshot = state["snapshot"]
            if (snapshot is None or state["scope"] != scope.currentData()
                    or state["absolute"] != absolute.isChecked()):
                self.status_label.setText(t("meta_io_preview_first")); return
            target = self._validated_destination(destination.text(), overwrite.isChecked())
            if target is None: return
            worker = self._callbacks["start_report_export"](
                snapshot, target, report_type=report_type, format=format_combo.currentData(),
                language=language.currentData(), include_technical_ids=technical.isChecked(),
                spreadsheet_safe=spreadsheet.isChecked(), overwrite=overwrite.isChecked())
            self._active_request_id = worker.request_identity.request_id
            self.status_label.setText(t("meta_io_state_loading"))
        preview_button.clicked.connect(make_preview); start.clicked.connect(run)
        controls = QHBoxLayout(); controls.addWidget(preview_button); controls.addWidget(start)
        form.addRow(controls); layout.addWidget(group); layout.addStretch(); return page

    def _playlist_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); group, form = self._numbered_group(1, "meta_io_playlist_options", page)
        scope = self._scope_combo(playlist=True); form.addRow(t("meta_io_scope"), scope)
        order = QComboBox();
        for key, value in (("meta_io_order_current", "current_view"), ("meta_io_order_natural", "natural_path"),
                           ("meta_io_order_track", "track_disc")): order.addItem(t(key), value)
        form.addRow(t("meta_io_order"), order)
        path_mode = QComboBox();
        for key, value in (("meta_io_path_auto", "auto"), ("meta_io_path_relative", "relative"),
                           ("meta_io_path_absolute", "absolute")): path_mode.addItem(t(key), value)
        form.addRow(t("meta_io_path_mode"), path_mode)
        source = QComboBox(); source.addItem(t("meta_io_effective_values"), "effective"); source.addItem(t("meta_io_original_values"), "original")
        form.addRow(t("meta_io_value_source"), source)
        fmt = QComboBox(); fmt.addItem("M3U8", "m3u8"); fmt.addItem("M3U", "m3u"); form.addRow(t("meta_io_playlist_format"), fmt)
        row, destination = self._destination_row(page, suffix="M3U8 (*.m3u8);;M3U (*.m3u)")
        form.addRow(t("meta_io_destination"), row); overwrite = QCheckBox(t("meta_io_overwrite")); form.addRow(overwrite)
        preview_label = QLabel(t("meta_io_preview_not_ready")); preview_label.setWordWrap(True)
        form.addRow(t("meta_io_preview"), preview_label)
        preview_button = QPushButton(t("meta_io_preview")); preview_button.setObjectName("metadataIOPreviewPlaylist")
        start = QPushButton(t("meta_io_export")); start.setObjectName("metadataIOExportPlaylist")

        def options():
            return {"scope": scope.currentData(), "order": order.currentData(),
                "path_mode": path_mode.currentData(), "value_source": source.currentData(), "format": fmt.currentData(),
                "ordered_item_ids": self._ordered_item_ids}

        def make_preview():
            plan = self._callbacks["create_playlist"](options())
            if not plan.entries:
                self._playlist_plan = None; self.status_label.setText(t("meta_io_empty_scope")); return
            self._playlist_plan = plan; self._playlist_plan_options = options()
            preview_label.setText(t("meta_io_playlist_preview_ready", n=len(plan.entries), warnings=len(plan.warnings)))
            self.status_label.setText(t("meta_io_preview_ready"))

        def run():
            if self._playlist_plan is None or self._playlist_plan_options != options():
                self.status_label.setText(t("meta_io_preview_first")); return
            target = self._validated_destination(destination.text(), overwrite.isChecked())
            if target is None: return
            worker = self._callbacks["start_playlist_export"](self._playlist_plan, target, overwrite=overwrite.isChecked())
            self._active_request_id = worker.request_identity.request_id; self.status_label.setText(t("meta_io_state_loading"))
        preview_button.clicked.connect(make_preview); start.clicked.connect(run)
        controls = QHBoxLayout(); controls.addWidget(preview_button); controls.addWidget(start)
        form.addRow(controls); layout.addWidget(group); layout.addStretch(); return page

    def _preset_page(self, mode: str) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page); group, form = self._numbered_group(1, "meta_io_preset_package", page)
        row, source = self._destination_row(page, suffix="BananaFlow presets (*.bananaflow-presets.json);;JSON (*.json)", save=mode == "export")
        form.addRow(t("meta_io_destination" if mode == "export" else "meta_io_source_file"), row)
        overwrite = QCheckBox(t("meta_io_overwrite"));
        if mode == "export": form.addRow(overwrite)
        preset_selection = None
        if mode == "export":
            preset_selection = QListWidget(); preset_selection.setAccessibleName(t("meta_io_preset_selection"))
            custom, _ = self._preset_store.load()
            for preset in custom:
                item = QListWidgetItem(preset.name); item.setData(Qt.UserRole, preset)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(Qt.Checked)
                preset_selection.addItem(item)
            form.addRow(t("meta_io_preset_selection"), preset_selection)
        preview = QTableWidget(0, 5); preview.setHorizontalHeaderLabels([
            t("meta_io_preset_name"), t("meta_io_state"), t("meta_io_conflict_policy"),
            t("meta_io_rename_to"), t("meta_io_details")])
        preview.setAccessibleName(t("meta_io_preset_preview")); form.addRow(preview)
        start = QPushButton(t("meta_io_preview" if mode == "export" else "meta_io_validate_package"))
        export_commit = QPushButton(t("meta_io_export")); export_commit.setEnabled(False)
        accept = QPushButton(t("meta_io_accept_preset_import")); accept.setEnabled(False)
        export_state = {"package": None, "selection": ()}

        def selected_presets():
            return tuple(preset_selection.item(index).data(Qt.UserRole)
                         for index in range(preset_selection.count())
                         if preset_selection.item(index).checkState() == Qt.Checked)

        def preview_export_package():
            selected = selected_presets()
            if not selected:
                export_state["package"] = None; export_commit.setEnabled(False)
                self.status_label.setText(t("meta_io_no_rows")); return
            try:
                package = build_transfer_package(selected, builtin_registry())
            except (KeyError, ValueError):
                self.status_label.setText(t("meta_io_error_invalid_format")); return
            export_state.update(package=package, selection=tuple(value.id for value in selected))
            preview.setRowCount(len(package.presets))
            for row_index, preset in enumerate(package.presets):
                preview.setItem(row_index, 0, QTableWidgetItem(preset.name))
                preview.setItem(row_index, 1, QTableWidgetItem(t("meta_io_preset_state_valid")))
            export_commit.setEnabled(True)
            self.status_label.setText(t("meta_io_preset_preview_ready", n=len(package.presets)))

        def export_package():
            package = export_state["package"]
            if package is None or export_state["selection"] != tuple(value.id for value in selected_presets()):
                self.status_label.setText(t("meta_io_preview_first")); return
            target = self._validated_destination(source.text(), overwrite.isChecked())
            if target is None: return
            try:
                export_transfer_package(package, target, overwrite=overwrite.isChecked())
            except MetadataIOError as error:
                self.status_label.setText(t(error.info.message_key)); return
            self.status_label.setText(t("meta_io_export_success", n=len(package.presets)))

        def validate_package():
            path = Path(source.text().strip())
            if not path.is_file(): self.status_label.setText(t("meta_io_source_missing")); return
            try:
                result_preview = preview_preset_import(
                    path, registry=builtin_registry(), store=self._preset_store)
            except MetadataIOError as error:
                self.status_label.setText(t(error.info.message_key)); return
            self._preset_import_preview = result_preview
            preview.setRowCount(len(result_preview.items))
            for row_index, item in enumerate(result_preview.items):
                preview.setItem(row_index, 0, QTableWidgetItem(item.preset.name if item.preset else ""))
                preview.setItem(row_index, 1, QTableWidgetItem(t(f"meta_io_preset_state_{item.state.value}")))
                policy = QComboBox(); policy.setAccessibleName(t("meta_io_conflict_policy"))
                for key, value in (("meta_io_conflict_skip", "skip"), ("meta_io_conflict_keep_both", "keep_both"),
                                   ("meta_io_conflict_replace", "replace_custom"), ("meta_io_conflict_rename", "rename")):
                    policy.addItem(t(key), value)
                policy.setEnabled(item.state.value == "existing_custom_conflict")
                policy.setProperty("preset_id", item.preset.id if item.preset else "")
                policy.setProperty("package_index", item.index)
                preview.setCellWidget(row_index, 2, policy)
                rename = QLineEdit(); rename.setAccessibleName(t("meta_io_rename_to"))
                rename.setProperty("preset_id", item.preset.id if item.preset else "")
                rename.setProperty("package_index", item.index)
                rename.setEnabled(item.state.value == "existing_custom_conflict")
                preview.setCellWidget(row_index, 3, rename)
                preview.setItem(row_index, 4, QTableWidgetItem(item.diagnostic))
            accept.setEnabled(True)
            self.status_label.setText(t("meta_io_preset_preview_ready", n=len(result_preview.items)))

        def accept_package():
            result_preview = self._preset_import_preview
            if result_preview is None: return
            decisions = []
            for row_index in range(preview.rowCount()):
                policy = preview.cellWidget(row_index, 2); rename = preview.cellWidget(row_index, 3)
                preset_id = str(policy.property("preset_id") or "")
                package_index = policy.property("package_index")
                if preset_id and policy.isEnabled():
                    decisions.append(PresetConflictDecision(
                        int(package_index), preset_id,
                        PresetConflictPolicy(policy.currentData()), rename.text()))
            result = accept_preset_import(result_preview, store=self._preset_store,
                                          decisions=decisions)
            if not result.accepted:
                self.status_label.setText(t(result.error.message_key)); return
            self.status_label.setText(t("meta_io_preset_import_success", n=result.imported, skipped=result.skipped))
            accept.setEnabled(False)

        if mode == "export":
            start.clicked.connect(preview_export_package); export_commit.clicked.connect(export_package)
        else:
            start.clicked.connect(validate_package); accept.clicked.connect(accept_package)
        buttons = QHBoxLayout(); buttons.addWidget(start)
        if mode == "import": buttons.addWidget(accept)
        else: buttons.addWidget(export_commit)
        form.addRow(buttons); layout.addWidget(group); layout.addStretch(); return page

    def _validated_destination(self, value: str, overwrite: bool) -> Path | None:
        path = Path(value.strip()) if value.strip() else None
        if path is None or not path.parent.is_dir():
            self.status_label.setText(t("meta_io_destination_invalid")); return None
        if path.exists() and not overwrite:
            self.status_label.setText(t("meta_io_destination_exists")); return None
        return path

    def on_io_started(self, identity) -> None:
        if identity.request_id == self._active_request_id:
            self.status_label.setText(t("meta_io_state_loading"))

    def on_io_finished(self, identity, result) -> None:
        if identity.request_id != self._active_request_id: return
        if isinstance(result, ParsedCsv): self._populate_mapping(result)
        elif hasattr(result, "safe_change_ids") and hasattr(result, "rows"): self._populate_import_results(result)
        else: self.status_label.setText(t("meta_io_export_success", n=getattr(result, "bytes_written", 0)))

    def on_io_error(self, identity, error) -> None:
        if identity.request_id == self._active_request_id:
            self.status_label.setText(t(error.message_key))

    def reject(self) -> None:
        cancel = self._callbacks.get("cancel_io")
        if cancel: cancel()
        super().reject()
