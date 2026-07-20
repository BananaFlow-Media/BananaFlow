import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QListWidget,
    QPushButton, QTableWidget,
)

from core.change_sets import ChangeOrigin, FileIdentity
from core.metadata_csv import (
    CANONICAL_FIELDS, BlankValuePolicy, ImportResultState,
    app_generated_mapping, import_mapping_identity, parse_csv_file,
)
from core.metadata_models import AudioTrackItem, OriginalTags
from core.preset_transfer import build_transfer_package, export_transfer_package
from core.tag_action_presets import PresetStep, PresetStore, TagActionPreset
from core.tag_actions import builtin_registry
from ui.controllers.metadata_controller import MetadataController
from ui.i18n import set_language, t
from ui.panels.metadata_editor.action_dialog import TagActionDialog
from ui.panels.metadata_editor.io_dialog import MetadataIODialog


def app():
    return QApplication.instance() or QApplication([])


def controller_with_track(tmp_path: Path):
    root = tmp_path / "music"; root.mkdir(); path = root / "song.mp3"; path.write_bytes(b"media")
    item = AudioTrackItem(path, root, ".mp3", original=OriginalTags(title="Old", artist="Artist"),
        format_id="mp3", baseline_identity=FileIdentity(str(path), path.stat().st_size, path.stat().st_mtime_ns))
    controller = MetadataController(); controller.workspace_state.set_tracks([item])
    from core.metadata_models import ScanResult
    controller._session.scan_result = ScanResult(root, [item], folder_set={root})
    return controller, item


def callbacks(controller):
    return {
        "create_csv_export_plan": controller.create_metadata_csv_export_plan,
        "start_csv_export": controller.start_metadata_csv_export,
        "start_csv_header_preview": controller.start_metadata_csv_header_preview,
        "start_csv_import_preview": controller.start_metadata_csv_import_preview,
        "accept_csv_import": controller.accept_metadata_csv_import,
        "create_change_report": controller.create_change_report_snapshot,
        "create_problems_report": controller.create_problems_report_snapshot,
        "start_report_export": controller.start_report_export,
        "create_playlist": controller.create_playlist_export_plan,
        "start_playlist_export": controller.start_playlist_export,
        "cancel_io": controller.cancel_metadata_io,
    }


def test_hub_has_all_explicit_operations_accessible_controls_and_no_automatic_start(tmp_path):
    app(); set_language("en"); controller, item = controller_with_track(tmp_path)
    started = []; controller.metadata_io_started.connect(started.append)
    dialog = MetadataIODialog(controller.workspace_state, callbacks=callbacks(controller),
        root=controller.io_root(), ordered_item_ids=(controller.workspace_state.item_id(item),))
    assert dialog.operation_list.count() == 7
    assert dialog.operation_list.accessibleName() == t("meta_io_operation_accessible")
    assert dialog.mapping_table.accessibleName() == t("meta_io_field_mapping")
    assert dialog.import_results.accessibleName() == t("meta_io_dry_run_results")
    assert dialog.export_fields.count() == len(CANONICAL_FIELDS)
    assert not started
    buttons = dialog.findChildren(QPushButton)
    assert any(button.objectName() == "metadataIODryRun" for button in buttons)
    assert any(button.objectName() == "metadataIOAcceptImport" for button in buttons)
    assert any(button.objectName() == "metadataIOPreviewCsv" for button in buttons)
    assert any(button.objectName() == "metadataIOPreviewPlaylist" for button in buttons)
    assert sum(button.objectName().startswith("metadataIOPreviewReport_") for button in buttons) == 2
    dialog.reject(); controller.deleteLater()


def test_export_plan_uses_explicit_scope_source_encoding_delimiter_and_privacy(tmp_path):
    app(); controller, item = controller_with_track(tmp_path)
    plan = controller.create_metadata_csv_export_plan({
        "scope": "all_loaded", "value_source": "effective", "encoding": "utf_8_bom",
        "delimiter": ";", "include_absolute_paths": False,
        "ordered_item_ids": (controller.workspace_state.item_id(item),),
    })
    assert plan.identity.item_ids == (controller.workspace_state.item_id(item),)
    assert plan.value_source.value == "effective" and plan.dialect.delimiter.value == ";"
    assert "absolute_path" not in plan.headers
    controller.deleteLater()


def test_report_absolute_path_option_is_private_by_default_and_bound_to_preview(tmp_path):
    app(); set_language("en"); controller, item = controller_with_track(tmp_path)
    item.proposed.title = "Changed"
    controller.workspace_state.capture_proposals([item], ChangeOrigin.MANUAL)
    dialog = MetadataIODialog(
        controller.workspace_state, callbacks=callbacks(controller),
        root=controller.io_root(),
        ordered_item_ids=(controller.workspace_state.item_id(item),))
    dialog.operation_list.setCurrentRow(2)
    page = dialog.pages.currentWidget()
    scope = next(combo for combo in page.findChildren(QComboBox)
                 if combo.findData("changed") >= 0)
    scope.setCurrentIndex(scope.findData("changed"))
    absolute = next(box for box in page.findChildren(QCheckBox)
                    if box.text() == t("meta_io_include_absolute_paths"))
    assert not absolute.isChecked()
    assert any(t("meta_io_absolute_paths_warning") == label.text()
               for label in page.findChildren(QLabel))
    preview = page.findChild(QPushButton, "metadataIOPreviewReport_change")
    export = page.findChild(QPushButton, "metadataIOExportReport_change")
    preview.click()
    assert any(t("meta_report_preview_absolute_off") in label.text()
               for label in page.findChildren(QLabel))
    absolute.setChecked(True)
    export.click()
    assert dialog.status_label.text() == t("meta_io_preview_first")
    preview.click()
    assert any(t("meta_report_preview_absolute_on") in label.text()
               for label in page.findChildren(QLabel))
    dialog.reject(); controller.deleteLater()


def test_controller_import_acceptance_emits_once_preserves_exclusion_and_writes_no_media(tmp_path):
    app(); controller, item = controller_with_track(tmp_path)
    identity = controller.workspace_state.item_id(item)
    item.excluded_from_apply = True
    path = tmp_path / "import.csv"
    path.write_text("relative_path,title\nsong.mp3,New\n", encoding="utf-8-sig")
    preview = controller.create_metadata_csv_import_preview({"path": path, "scope": "all_loaded"})
    notifications = []; controller.tags_modified.connect(lambda: notifications.append(True))
    before = item.path.read_bytes()
    result = controller.accept_metadata_csv_import(preview, preview.safe_change_ids)
    assert result.accepted and len(notifications) == 1
    assert item.excluded_from_apply and item.path.read_bytes() == before and item.path.name == "song.mp3"
    assert controller.workspace_state.proposal_history.can_undo(controller.workspace_state.generation)
    controller.deleteLater()


def test_hebrew_hub_is_localized_while_technical_choices_remain_ltr(tmp_path):
    application = app(); set_language("he"); controller, item = controller_with_track(tmp_path)
    dialog = MetadataIODialog(controller.workspace_state, callbacks=callbacks(controller),
        root=controller.io_root(), ordered_item_ids=(controller.workspace_state.item_id(item),))
    assert dialog.windowTitle() == "ייבוא / ייצוא"
    technical = [combo.itemText(index) for combo in dialog.findChildren(QComboBox)
                 for index in range(combo.count())]
    assert "UTF-8 BOM" in technical and "M3U8" in technical
    dialog.reject(); controller.deleteLater(); set_language("en")


def test_preset_import_ui_is_preview_first_and_applies_per_conflict_policy(tmp_path):
    app(); set_language("en"); controller, item = controller_with_track(tmp_path)
    preset = TagActionPreset("custom.one", "Incoming", "tag.set_field.v1", {}, False, 1,
        (PresetStep("tag.set_field.v1", {"field": "title", "value": "New"}),))
    existing = TagActionPreset("custom.one", "Existing", "tag.set_field.v1", {}, False, 1,
        (PresetStep("tag.set_field.v1", {"field": "title", "value": "Old"}),))
    package_path = tmp_path / "presets.json"
    export_transfer_package(build_transfer_package([preset], builtin_registry()), package_path)
    store_path = tmp_path / "store.json"; store = PresetStore(store_path, builtin_registry()); store.save([existing])
    dialog = MetadataIODialog(controller.workspace_state, callbacks=callbacks(controller),
        root=controller.io_root(), ordered_item_ids=(controller.workspace_state.item_id(item),),
        preset_path=store_path)
    dialog.operation_list.setCurrentRow(6)
    page = dialog.pages.currentWidget(); source = page.findChildren(QLineEdit)[0]; source.setText(str(package_path))
    validate = next(button for button in page.findChildren(QPushButton)
                    if button.text() == t("meta_io_validate_package"))
    validate.click()
    assert [value.name for value in store.load()[0]] == ["Existing"]
    table = page.findChild(QTableWidget); assert table.rowCount() == 1
    policy = table.cellWidget(0, 2); policy.setCurrentIndex(policy.findData("keep_both"))
    accept = next(button for button in page.findChildren(QPushButton)
                  if button.text() == t("meta_io_accept_preset_import"))
    accept.click()
    assert len(store.load()[0]) == 2 and item.proposed.title is None
    dialog.reject(); controller.deleteLater()


def test_export_preview_gate_import_filters_and_preset_surface_shortcut(tmp_path):
    app(); set_language("en"); controller, item = controller_with_track(tmp_path)
    dialog = MetadataIODialog(controller.workspace_state, callbacks=callbacks(controller),
        root=controller.io_root(), ordered_item_ids=(controller.workspace_state.item_id(item),),
        preset_path=tmp_path / "store.json")
    dialog._start_metadata_export()
    assert dialog.status_label.text() == t("meta_io_preview_first")
    dialog.export_scope.setCurrentIndex(dialog.export_scope.findData("all_loaded"))
    title_row = next(index for index in range(dialog.export_fields.count())
                     if dialog.export_fields.item(index).data(Qt.UserRole) == "title")
    dialog.export_fields.item(title_row).setCheckState(Qt.Unchecked)
    dialog._preview_metadata_export()
    assert dialog._export_plan is not None and "title" not in dialog._export_plan.fields
    assert t("meta_io_preview_ready") == dialog.status_label.text()

    source = tmp_path / "filter.csv"
    source.write_text("relative_path,title\nsong.mp3,Changed\nmissing.mp3,Missing\n", encoding="utf-8-sig")
    parsed = parse_csv_file(source); mapping, identity_mapping = app_generated_mapping(parsed.headers)
    worker = controller.start_metadata_csv_import_preview({
        "path": source, "scope": "all_loaded", "mapping": mapping,
        "identity_mapping": identity_mapping, "source_identity": parsed.source,
        "blank_policy": BlankValuePolicy.NO_CHANGE.value,
    })
    assert worker.request_identity.source_identity == parsed.source
    assert worker.request_identity.mapping_identity == import_mapping_identity(
        mapping, identity_mapping, BlankValuePolicy.NO_CHANGE)
    assert worker.wait(5000); QApplication.processEvents()
    preview = controller.create_metadata_csv_import_preview({"path": source, "scope": "all_loaded"})
    dialog._populate_import_results(preview)
    dialog.import_filter.setCurrentIndex(dialog.import_filter.findData("unmatched"))
    visible_states = [dialog.import_results.item(row, 0).data(int(Qt.UserRole) + 1)
                      for row in range(dialog.import_results.rowCount())
                      if not dialog.import_results.isRowHidden(row)]
    assert visible_states == [ImportResultState.UNMATCHED.value]

    opened = []
    action_dialog = TagActionDialog(controller.workspace_state, preset_path=tmp_path / "actions.json",
                                    open_preset_transfer=lambda: opened.append(True))
    action_dialog.findChild(QPushButton, "metadataIOPresetTransfer").click()
    assert opened == [True]
    action_dialog.reject(); dialog.reject(); controller.deleteLater()


def test_preset_export_supports_explicit_selection_and_preview_before_write(tmp_path):
    app(); controller, item = controller_with_track(tmp_path)
    presets = [TagActionPreset(f"custom.{index}", f"Preset {index}", "tag.set_field.v1", {}, False, 1,
        (PresetStep("tag.set_field.v1", {"field": "title", "value": str(index)}),)) for index in (1, 2)]
    store_path = tmp_path / "store.json"; PresetStore(store_path, builtin_registry()).save(presets)
    dialog = MetadataIODialog(controller.workspace_state, callbacks=callbacks(controller),
        root=controller.io_root(), preset_path=store_path)
    dialog.operation_list.setCurrentRow(5); page = dialog.pages.currentWidget()
    selection = page.findChild(QListWidget); selection.item(1).setCheckState(Qt.Unchecked)
    preview_button = next(button for button in page.findChildren(QPushButton)
                          if button.text() == t("meta_io_preview"))
    export_button = next(button for button in page.findChildren(QPushButton)
                         if button.text() == t("meta_io_export"))
    assert not export_button.isEnabled()
    preview_button.click()
    assert export_button.isEnabled() and page.findChild(QTableWidget).rowCount() == 1
    assert not list(tmp_path.glob("*.bananaflow-presets.json"))
    dialog.reject(); controller.deleteLater()
