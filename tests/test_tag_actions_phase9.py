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


# --------------------------------------------------------------------------- #
# Actions added after the 30 July 2026 audit
# --------------------------------------------------------------------------- #

def _ctx(values, item_id=1):
    return TagActionContext(item_id, "song.mp3", ".mp3", "mp3", values)


def test_regex_replace_supports_capture_groups():
    action = builtin_registry().get("tag.replace_regex.v1")
    result = action.evaluate(
        _ctx({"title": "Ribo, Ishay"}),
        {"field": "title", "pattern": r"(\w+), (\w+)", "replace": r"\2 \1",
         "case_sensitive": True},
    )
    assert result.fields == {"title": "Ishay Ribo"}


def test_regex_replace_reports_a_bad_pattern_instead_of_raising():
    """A half-typed pattern must not take the live preview down."""
    from core.tag_actions import ActionResultStatus

    action = builtin_registry().get("tag.replace_regex.v1")
    result = action.evaluate(
        _ctx({"title": "Song"}),
        {"field": "title", "pattern": "([unclosed", "replace": "", "case_sensitive": False},
    )
    assert result.status is ActionResultStatus.WARNING
    assert result.diagnostic == "invalid_pattern"
    assert not result.fields


def test_split_field_moves_the_second_half_and_keeps_the_tail():
    action = builtin_registry().get("tag.split_field.v1")
    result = action.evaluate(
        _ctx({"title": "Ishay Ribo - Seter - Live", "artist": ""}),
        {"field": "title", "separator": " - ", "target_field": "artist",
         "target_first": False},
    )
    # Only the first separator splits, so the tail stays with the second half
    # rather than being silently dropped.
    assert result.fields == {"title": "Ishay Ribo", "artist": "Seter - Live"}


def test_split_field_without_the_separator_changes_nothing():
    from core.tag_actions import ActionResultStatus

    action = builtin_registry().get("tag.split_field.v1")
    result = action.evaluate(
        _ctx({"title": "Seter", "artist": ""}),
        {"field": "title", "separator": " - ", "target_field": "artist",
         "target_first": False},
    )
    assert result.status is ActionResultStatus.NO_OP


def test_split_field_refuses_to_leave_a_half_empty():
    from core.tag_actions import ActionResultStatus

    action = builtin_registry().get("tag.split_field.v1")
    result = action.evaluate(
        _ctx({"title": "Seter - ", "artist": ""}),
        {"field": "title", "separator": " - ", "target_field": "artist",
         "target_first": False},
    )
    assert result.status is ActionResultStatus.WARNING
    assert result.diagnostic == "empty_half"


def test_encoding_repair_recovers_hebrew_stored_as_cp1255():
    """The exact fault: cp1255 bytes in the file, decoded as Latin-1 on read."""
    action = builtin_registry().get("tag.repair_encoding.v1")
    original = "ישי ריבו"
    mojibake = original.encode("cp1255").decode("latin-1")
    assert mojibake != original

    result = action.evaluate(
        _ctx({"artist": mojibake}),
        {"field": "artist", "all_fields": False, "codepage": "cp1255"},
    )
    assert result.fields == {"artist": original}


def test_encoding_repair_leaves_correct_text_alone():
    """Rewriting a tag that was already fine is worse than the bug."""
    from core.tag_actions import ActionResultStatus

    action = builtin_registry().get("tag.repair_encoding.v1")
    result = action.evaluate(
        _ctx({"artist": "ישי ריבו"}),
        {"field": "artist", "all_fields": False, "codepage": "cp1255"},
    )
    assert result.status is ActionResultStatus.NO_OP
    assert result.diagnostic == "nothing_to_repair"


def test_encoding_repair_leaves_plain_ascii_alone():
    from core.tag_actions import ActionResultStatus

    action = builtin_registry().get("tag.repair_encoding.v1")
    result = action.evaluate(
        _ctx({"artist": "Ishay Ribo"}),
        {"field": "artist", "all_fields": False, "codepage": "cp1255"},
    )
    assert result.status is ActionResultStatus.NO_OP


def test_encoding_repair_can_sweep_every_text_field():
    action = builtin_registry().get("tag.repair_encoding.v1")
    broken = {name: value.encode("cp1255").decode("latin-1")
              for name, value in (("title", "סתר"), ("artist", "ישי ריבו"))}
    result = action.evaluate(
        _ctx({**broken, "album": "Elul 5779"}),
        {"field": "title", "all_fields": True, "codepage": "cp1255"},
    )
    assert result.fields == {"title": "סתר", "artist": "ישי ריבו"}


def test_repair_mojibake_refuses_a_repair_that_still_looks_broken():
    """A round trip that lands on U+FFFD has not recovered anything."""
    from core.tag_actions import repair_mojibake

    assert repair_mojibake("", "cp1255") is None
    # Genuine Unicode cannot have come from a Latin-1 mis-decode.
    assert repair_mojibake("ישי ריבו", "cp1255") is None
