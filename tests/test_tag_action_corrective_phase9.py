"""Independent-review blocker regressions for Phase 9 production paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from core.metadata_models import AudioTrackItem, OriginalTags
from core.change_drafts import DraftStore
from ui.controllers.metadata_controller import MetadataController
from ui.i18n import apply_language, current_language, t
from ui.panels.metadata_editor import MetadataEditorPanel
from ui.panels.metadata_editor.action_diagnostics import format_action_diagnostic


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _production_stack(tmp_path: Path):
    controller = MetadataController()
    controller._draft_store = DraftStore(tmp_path / "tag_editor_pending.json")  # noqa: SLF001
    panel = MetadataEditorPanel()
    panel.set_workspace_state(controller.workspace_state)
    panel.set_tag_action_preview_acceptor(controller.accept_tag_action_preview)
    item = AudioTrackItem(
        tmp_path / "old.mp3", tmp_path, ".mp3",
        original=OriginalTags(title="hello world", artist="Artist"),
    )
    controller.workspace_state.set_tracks([item])
    controller.workspace_state.set_selected_items([item])
    controller.workspace_state.set_visible_items([item])
    return controller, panel, item


def _select_template(dialog, action_id: str, template: str) -> None:
    dialog._tabs.setCurrentIndex(1)
    dialog._template_combo.setCurrentIndex(dialog._template_combo.findData(action_id))
    editor = dialog._parameter_widgets["template"]
    assert isinstance(editor, QLineEdit)
    editor.setText(template)
    dialog.refresh_preview()


@pytest.mark.parametrize("kind", ["action", "template", "preset"])
def test_production_dialog_acceptance_uses_controller_draft_lifecycle(app, tmp_path, monkeypatch, kind):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    controller, panel, item = _production_stack(tmp_path)
    notifications = []
    controller.tags_modified.connect(lambda: notifications.append(True))
    try:
        dialog = panel._create_action_engine_dialog()
        if kind == "action":
            dialog._action_combo.setCurrentIndex(dialog._action_combo.findData("tag.change_case.v1"))
        elif kind == "template":
            _select_template(dialog, "template.tags_to_filename.v1", "{artist} - {title}")
        else:
            dialog._tabs.setCurrentIndex(2)
        dialog.refresh_preview()
        assert dialog._preview is not None and dialog._preview.changed_count == 1
        dialog.accept_preview()

        assert dialog.result() == dialog.DialogCode.Accepted
        assert notifications == [True]
        assert controller._draft_timer.isActive()  # noqa: SLF001 - production debounce contract
        assert len(controller.workspace_state.change_set.records()) >= 1
        assert controller.workspace_state.can_undo_proposals()
        assert controller.workspace_state.undo_proposals()
        assert not controller.workspace_state.undo_proposals()
        assert not item.path.exists()  # Phase 9 never writes media or renames files.

        # Recreate acceptance because Undo intentionally removes the pending proposal;
        # this verifies the real debounce persists the controller-owned snapshot.
        dialog = panel._create_action_engine_dialog()
        if kind == "action":
            dialog._action_combo.setCurrentIndex(dialog._action_combo.findData("tag.change_case.v1"))
        elif kind == "template":
            _select_template(dialog, "template.tags_to_filename.v1", "{artist} - {title}")
        else:
            dialog._tabs.setCurrentIndex(2)
        dialog.refresh_preview()
        dialog.accept_preview()
        QTest.qWait(650)
        metadata, snapshot = controller._draft_store.load()  # noqa: SLF001 - production draft store
        assert metadata["session_id"] == controller._draft_session_id  # noqa: SLF001
        assert snapshot.records
    finally:
        panel.deleteLater()
        controller.deleteLater()


def test_production_dialog_cancel_does_not_schedule_a_draft(app, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    controller, panel, _item = _production_stack(tmp_path)
    notifications = []
    controller.tags_modified.connect(lambda: notifications.append(True))
    try:
        dialog = panel._create_action_engine_dialog()
        dialog._action_combo.setCurrentIndex(dialog._action_combo.findData("tag.change_case.v1"))
        dialog.refresh_preview()
        dialog.reject()
        QTest.qWait(550)
        assert notifications == []
        assert not controller.workspace_state.change_set.records()
        assert not controller._draft_store.path.exists()  # noqa: SLF001
    finally:
        panel.deleteLater()
        controller.deleteLater()


def _detail_values(dialog) -> list[str]:
    return [dialog._preview_table.item(row, 5).text() for row in range(dialog._preview_table.rowCount())]


def test_dialog_details_localize_template_missing_invalid_unsupported_and_collision(app, tmp_path):
    controller, panel, item = _production_stack(tmp_path)
    try:
        item.original.artist = ""
        dialog = panel._create_action_engine_dialog()
        _select_template(dialog, "template.tags_to_filename.v1", "{artist} - {title}")
        details = _detail_values(dialog)
        assert t("meta_action_diag_missing_value", field=t("meta_field_artist").rstrip(":")) in details
        assert all("missing_value:artist" not in value for value in details)
        dialog.deleteLater()

        dialog = panel._create_action_engine_dialog()
        _select_template(dialog, "template.filename_to_tags.v1", "{artist}{title}")
        details = _detail_values(dialog)
        assert t("meta_action_diag_adjacent_field") in details
        assert all("adjacent_fields_are_ambiguous" not in value for value in details)
        dialog.deleteLater()

        item.metadata_editable = False
        dialog = panel._create_action_engine_dialog()
        dialog._action_combo.setCurrentIndex(dialog._action_combo.findData("tag.change_case.v1"))
        dialog.refresh_preview()
        assert t("meta_action_diag_unsupported_item") in _detail_values(dialog)
        dialog.deleteLater()

        item.metadata_editable = True
        other = AudioTrackItem(tmp_path / "taken.mp3", tmp_path, ".mp3", original=OriginalTags(title="taken"))
        other.path.write_bytes(b"fixture")
        item.original.title = "taken"
        controller.workspace_state.set_tracks([item, other])
        controller.workspace_state.set_selected_items([item])
        controller.workspace_state.set_visible_items([item])
        dialog = panel._create_action_engine_dialog()
        dialog._action_combo.setCurrentIndex(dialog._action_combo.findData("file.from_title.v1"))
        dialog._scope_combo.setCurrentIndex(dialog._scope_combo.findData("selected"))
        dialog.refresh_preview()
        assert dialog._preview is not None and dialog._preview.blocked
        details = _detail_values(dialog)
        assert t("meta_action_diag_rename_collision") in details
        assert all("rename_collision" not in value for value in details)
        dialog.deleteLater()
    finally:
        panel.deleteLater()
        controller.deleteLater()


def test_diagnostic_formatter_localizes_tokens_unknown_codes_and_hebrew(app):
    previous = current_language()
    try:
        apply_language(app, "en")
        token_detail = format_action_diagnostic("unknown_field:unsafe")
        assert "{unsafe}" in token_detail and "unknown_field:unsafe" not in token_detail
        unknown = format_action_diagnostic("future_engine_code:artist")
        assert unknown == t("meta_action_diag_unknown")
        assert "future_engine_code" not in unknown

        apply_language(app, "he")
        detail = format_action_diagnostic("missing_value:artist")
        assert detail == t("meta_action_diag_missing_value", field=t("meta_field_artist").rstrip(":"))
        assert "missing_value:artist" not in detail
        assert "artist" not in detail.casefold()
    finally:
        apply_language(app, previous)
