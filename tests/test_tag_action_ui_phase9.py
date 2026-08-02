"""Offscreen production UI coverage for the Phase 9 action engine."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from core.metadata_models import AudioTrackItem, OriginalTags, ScanResult
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.i18n import apply_language, current_language, set_language, t
from ui.panels.metadata_editor.action_dialog import TagActionDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path: Path, name: str = "old.mp3", *, title: str = "שיר Song"):
    item = AudioTrackItem(tmp_path / name, tmp_path, ".mp3", original=OriginalTags(title=title))
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([item])
    workspace.set_selected_items([item])
    workspace.set_visible_items([item])
    return workspace, item


def _select_template(dialog: TagActionDialog, action_id: str, template: str) -> None:
    dialog._tabs.setCurrentIndex(1)
    dialog._template_combo.setCurrentIndex(dialog._template_combo.findData(action_id))
    editor = dialog._parameter_widgets["template"]
    assert isinstance(editor, QLineEdit)
    editor.setText(template)
    dialog.refresh_preview()


def test_template_preview_is_live_immutable_and_accepts_to_change_set(app, tmp_path):
    workspace, item = _workspace(tmp_path)
    dialog = TagActionDialog(workspace, active_folder=tmp_path, preset_path=tmp_path / "presets.json")
    try:
        _select_template(dialog, "template.tags_to_filename.v1", "{title}")
        assert item.proposed_filename is None
        assert not workspace.change_set.records()
        assert dialog._preview is not None and dialog._preview.changed_count == 1
        assert dialog._preview_table.rowCount() == 1
        assert dialog._preview_table.item(0, 3).text() == "שיר Song.mp3"
        assert dialog._preview_table.item(0, 0).textAlignment() & int(Qt.AlignmentFlag.AlignLeft)
        assert dialog._accept_btn.text() == t("meta_action_add_pending")

        dialog.accept_preview()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert item.proposed_filename == "שיר Song.mp3"
        records = workspace.change_set.records()
        assert len(records) == 1 and records[0].field == "filename"
        assert not (tmp_path / "שיר Song.mp3").exists()
    finally:
        dialog.deleteLater()


def test_cancel_after_preview_leaves_proposals_untouched(app, tmp_path):
    workspace, item = _workspace(tmp_path)
    dialog = TagActionDialog(workspace, active_folder=tmp_path, preset_path=tmp_path / "presets.json")
    try:
        _select_template(dialog, "template.tags_to_filename.v1", "{title}")
        dialog.reject()
        assert item.proposed_filename is None
        assert not workspace.change_set.records()
    finally:
        dialog.deleteLater()


def test_preview_surfaces_collision_noop_and_unsupported_states(app, tmp_path):
    first = AudioTrackItem(tmp_path / "a.mp3", tmp_path, ".mp3", original=OriginalTags(title="same"))
    second = AudioTrackItem(tmp_path / "b.mp3", tmp_path, ".mp3", original=OriginalTags(title="same"))
    noop = AudioTrackItem(tmp_path / "same.mp3", tmp_path, ".mp3", original=OriginalTags(title="same"))
    unsupported = AudioTrackItem(
        tmp_path / "unsupported.mp3", tmp_path, ".mp3", original=OriginalTags(title="unsupported"),
        metadata_editable=False,
    )
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([first, second, noop, unsupported])
    workspace.set_visible_items([first, second, noop, unsupported])
    dialog = TagActionDialog(workspace, active_folder=tmp_path, preset_path=tmp_path / "presets.json")
    try:
        dialog._action_combo.setCurrentIndex(dialog._action_combo.findData("file.from_title.v1"))
        dialog._scope_combo.setCurrentIndex(dialog._scope_combo.findData("visible"))
        dialog.refresh_preview()
        statuses = {
            dialog._preview_table.item(row, 4).text()
            for row in range(dialog._preview_table.rowCount())
        }
        assert t("meta_action_status_collision") in statuses
        assert t("meta_action_status_no_op") in statuses
        assert t("meta_action_status_unsupported") in statuses
        assert not dialog._accept_btn.isEnabled()
        assert not workspace.change_set.records()
    finally:
        dialog.deleteLater()


def test_hebrew_dialog_is_rtl_but_templates_and_paths_remain_ltr(app, tmp_path):
    previous = current_language()
    try:
        apply_language(app, "he")
        workspace, _item = _workspace(tmp_path, title="שלום Mixed Title")
        dialog = TagActionDialog(workspace, active_folder=tmp_path, preset_path=tmp_path / "presets.json")
        try:
            _select_template(dialog, "template.tags_to_filename.v1", "{artist}[ - {album}] - {title}")
            editor = dialog._parameter_widgets["template"]
            assert dialog.layoutDirection() == Qt.LayoutDirection.RightToLeft
            assert editor.layoutDirection() == Qt.LayoutDirection.LeftToRight
            assert editor.alignment() & Qt.AlignmentFlag.AlignLeft
            assert dialog._preview_table.item(0, 0).textAlignment() & int(Qt.AlignmentFlag.AlignLeft)
            visible_labels = [dialog._action_combo.itemText(index) for index in range(dialog._action_combo.count())]
            assert all(not label.endswith(".v1") for label in visible_labels)
            assert dialog._accept_btn.text() == t("meta_action_add_pending")
        finally:
            dialog.deleteLater()
    finally:
        apply_language(app, previous)


def test_custom_preset_save_uses_schema_and_stable_action_id(app, tmp_path, monkeypatch):
    workspace, _item = _workspace(tmp_path)
    path = tmp_path / "presets.json"
    dialog = TagActionDialog(workspace, active_folder=tmp_path, preset_path=path)
    try:
        monkeypatch.setattr("ui.panels.metadata_editor.action_dialog.get_text", lambda *_a, **_k: ("My Workflow", True))
        dialog._save_as_preset()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == 2
        assert payload["presets"][0]["name"] == "My Workflow"
        assert payload["presets"][0]["action_id"].endswith(".v1")
        assert ".v1" not in dialog._preset_combo.itemText(0)
    finally:
        dialog.deleteLater()


def test_panel_exposes_production_action_engine_page(app, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from ui.panels.metadata_editor import MetadataEditorPanel

    panel = MetadataEditorPanel()
    try:
        item = AudioTrackItem(tmp_path / "song.mp3", tmp_path, ".mp3", original=OriginalTags(title="Song"))
        panel._root_folder = tmp_path
        panel.on_scan_complete(ScanResult(root=tmp_path, tracks=[item], folder_set={tmp_path}))
        assert panel._action_engine_btn.isEnabled()
        assert panel._templates_btn.isEnabled()
        assert panel._saved_workflows_btn.isEnabled()
        assert panel._action_engine_btn.accessibleName() == t("meta_all_actions_engine_open")
        assert any(button.toolTip() == t("meta_action_engine_title") for button in panel._inspector_tool_buttons)
    finally:
        panel.deleteLater()
