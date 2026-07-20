from core.tag_actions import TagActionContext, builtin_registry
from core.tag_templates import TemplateError, compile_template, safe_filename
from core.tag_action_presets import PresetStore
from core.tag_action_service import TagActionService
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def test_filename_to_tag_template_round_trip_and_numeric_track():
    action = builtin_registry().get("filename_to_tags")
    result = action.evaluate(TagActionContext(7, "02 - Artist - Song.mp3", ".mp3", "mp3", {}), {"template": "{track_num} - {artist} - {title}"})
    assert result.fields == {"track_num": 2, "artist": "Artist", "title": "Song"}


def test_tag_to_filename_is_preview_only_and_windows_safe():
    action = builtin_registry().get("tags_to_filename")
    result = action.evaluate(TagActionContext(9, "old.mp3", ".mp3", "mp3", {"artist": "A/B", "title": "Song"}), {"template": "{artist} - {title}"})
    assert result.item_id == 9 and result.filename == "A B - Song.mp3" and not result.fields


def test_ambiguous_and_reserved_templates_are_rejected():
    try:
        compile_template("{artist}{title}", direction="filename_to_tags")
    except TemplateError as exc:
        assert str(exc) == "adjacent_fields_are_ambiguous"
    else:
        raise AssertionError("ambiguous template accepted")
    try:
        safe_filename("CON", ".mp3")
    except TemplateError as exc:
        assert str(exc) == "reserved_filename"
    else:
        raise AssertionError("reserved filename accepted")


def test_custom_presets_round_trip_and_corruption_are_safe(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    preset = store.create("My rename", "tags_to_filename", {"template": "{artist} - {title}"})
    store.save([preset])
    loaded, diagnostic = store.load()
    assert diagnostic is None and loaded == [preset]
    store.path.write_text("not json", encoding="utf-8")
    assert store.load() == ([], "preset_store_corrupt")


def test_preview_is_non_mutating_and_accept_uses_one_canonical_change_command(tmp_path):
    item = AudioTrackItem(tmp_path / "old.mp3", tmp_path, ".mp3", original=OriginalTags(title="Old", artist="Artist"))
    workspace = TagEditorWorkspaceState(); workspace.set_tracks([item]); workspace.set_selected_paths({item.path})
    service = TagActionService(builtin_registry())
    preview = service.preview(workspace, "tags_to_filename", parameters={"template": "{artist} - {title}"})
    assert item.proposed_filename is None and not workspace.change_set.records()
    assert service.accept(workspace, preview)
    assert item.proposed_filename == "Artist - Old.mp3"
    assert workspace.change_set.records()[0].item_id == workspace.item_id(item)
    assert workspace.undo_proposals() and item.proposed_filename is None


def test_preview_becomes_stale_after_another_canonical_proposal(tmp_path):
    item = AudioTrackItem(tmp_path / "old.mp3", tmp_path, ".mp3", original=OriginalTags(title="Old"))
    workspace = TagEditorWorkspaceState(); workspace.set_tracks([item]); workspace.set_selected_paths({item.path})
    service = TagActionService(builtin_registry())
    preview = service.preview(workspace, "tags_to_filename", parameters={"template": "{title}"})
    item.proposed.title = "Changed"; workspace.capture_proposals([item])
    assert not service.accept(workspace, preview)


def test_controller_action_production_path_uses_workspace_change_set(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path)); monkeypatch.setenv("HOME", str(tmp_path))
    from PySide6.QtWidgets import QApplication
    from ui.controllers.metadata_controller import MetadataController
    app = QApplication.instance() or QApplication([])
    controller = MetadataController()
    try:
        item = AudioTrackItem(tmp_path / "old.mp3", tmp_path, ".mp3", original=OriginalTags(title="Old"))
        controller.workspace_state.set_tracks([item]); controller.workspace_state.set_selected_paths({item.path})
        preview = controller.preview_tag_action("tags_to_filename", {"template": "{title}"})
        assert item.proposed_filename is None
        assert controller.accept_tag_action_preview(preview)
        assert item.proposed_filename == "Old.mp3"
        assert controller.workspace_state.change_set.records()[0].origin.value == "template"
    finally:
        controller.deleteLater()
