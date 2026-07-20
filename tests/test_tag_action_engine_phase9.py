"""Phase 9 registry, scope, Change Set, template and planner production paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.change_sets import ChangeOrigin
from core.metadata_processor import _dir_is_case_insensitive
from core.metadata_models import AudioTrackItem, OriginalTags
from core.tag_actions import ActionResultStatus, TagActionContext, builtin_registry
from core.tag_action_service import TagActionService
from core.tag_action_presets import PresetStep
from core.tag_templates import TemplateError, compile_template, safe_filename
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def _item(path: Path, **tags) -> AudioTrackItem:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unchanged-media")
    return AudioTrackItem(path, path.parent, path.suffix, original=OriginalTags(**tags))


def _workspace(tmp_path: Path, specs: list[tuple[str, dict]]) -> tuple[TagEditorWorkspaceState, list[AudioTrackItem]]:
    items = [_item(tmp_path / name, **tags) for name, tags in specs]
    workspace = TagEditorWorkspaceState(); workspace.set_tracks(items)
    return workspace, items


def test_registry_has_unique_versioned_metadata_for_every_action():
    actions = builtin_registry().actions()
    assert len(actions) >= 25
    assert len({action.id for action in actions}) == len(actions)
    for action in actions:
        assert action.id.endswith(".v1")
        assert action.name_key.startswith("meta_") and action.description_key.startswith("meta_")
        assert action.category and action.scopes
        assert action.writes and callable(action.evaluator)


def test_every_registered_action_has_a_valid_explicit_evaluation_result():
    registry = builtin_registry()
    parameters = {
        "tag.title_from_filename.v1": {"strip_numbering": True},
        "tag.normalize_spaces.v1": {"field": "title"},
        "tag.strip_web_junk.v1": {
            "remove_web_junk": True, "remove_hebrew": True, "fix_punctuation": True,
        },
        "file.clean.v1": {
            "smart_brackets": True, "remove_domains": True,
            "remove_emojis": True, "fix_spaces": True,
        },
        "template.filename_to_tags.v1": {
            "template": "{track_num} - {artist} - {title}", "overwrite": True,
        },
        "template.tags_to_filename.v1": {
            "template": "{artist} - {title}", "sanitize": True,
        },
        "tag.set_field.v1": {"field": "album", "value": "New Album"},
        "tag.set_artist.v1": {"value": "New Artist"},
        "tag.replace_text.v1": {
            "field": "title", "find": "WORLD", "replace": "World", "case_sensitive": True,
        },
        "tag.change_case.v1": {"field": "title", "mode": "lower"},
        "tag.number_tracks.v1": {"start": 1, "step": 1},
    }
    values = {
        "title": "hello__WORLD [HD]", "artist": "Artist", "album": "Album",
        "album_artist": "Different", "track_num": 8, "disc_num": 1,
        "year": "2020", "genre": "Rock", "comment": "Comment", "composer": "Composer",
    }
    evaluated = set()
    for action in registry.actions():
        result = action.evaluate(
            TagActionContext(1, "01 - Artist - Song [HD].mp3", ".mp3", "mp3", values),
            parameters.get(action.id),
        )
        evaluated.add(action.id)
        assert isinstance(result.status, ActionResultStatus)
        assert result.status is not ActionResultStatus.BLOCKER, (action.id, result.diagnostic)
    assert evaluated == {action.id for action in registry.actions()}


@pytest.mark.parametrize(("action_id", "values", "filename", "parameters", "expected"), [
    ("tag.title_from_filename.v1", {"title": ""}, "01 - Song.mp3", {"strip_numbering": True}, {"title": "Song"}),
    ("tag.track_from_filename.v1", {"track_num": None}, "07 Song.mp3", {}, {"track_num": 7}),
    ("tag.split_artist_title.v1", {"artist": "", "title": ""}, "Artist - Song.mp3", {}, {"artist": "Artist", "title": "Song"}),
    ("tag.album_artist_from_artist.v1", {"artist": "Artist", "album_artist": ""}, "x.mp3", {}, {"album_artist": "Artist"}),
    ("tag.normalize_spaces.v1", {"title": "A__  B"}, "x.mp3", {"field": "title"}, {"title": "A B"}),
    ("tag.clear_comments.v1", {"comment": "old"}, "x.mp3", {}, {"comment": ""}),
    ("tag.clear_track_num.v1", {"track_num": 4}, "x.mp3", {}, {"track_num": -1}),
    ("tag.replace_text.v1", {"title": "Hello WORLD"}, "x.mp3",
     {"field": "title", "find": "world", "replace": "music", "case_sensitive": False}, {"title": "Hello music"}),
    ("tag.change_case.v1", {"title": "hello WORLD"}, "x.mp3", {"field": "title", "mode": "title"}, {"title": "Hello World"}),
    ("tag.number_tracks.v1", {"track_num": None}, "x.mp3", {"start": 3, "step": 2}, {"track_num": 3}),
])
def test_registered_action_transformations(action_id, values, filename, parameters, expected):
    action = builtin_registry().get(action_id)
    result = action.evaluate(TagActionContext(1, filename, ".mp3", "mp3", values), parameters)
    assert result.status is ActionResultStatus.CHANGED
    assert result.fields == expected


def test_parameter_validation_and_unsupported_results_are_explicit():
    action = builtin_registry().get("tag.change_case.v1")
    bad = action.evaluate(TagActionContext(1, "x.mp3", ".mp3", "mp3", {"title": "x"}),
                          {"field": "title", "mode": "execute"})
    assert bad.status is ActionResultStatus.BLOCKER and "parameter_choice" in bad.diagnostic
    unsupported = action.evaluate(TagActionContext(1, "x.mp3", ".mp3", "mp3", {"title": "x"}, editable=False))
    assert unsupported.status is ActionResultStatus.UNSUPPORTED


def test_all_scope_modes_use_stable_ids_and_scope_changes_invalidate(tmp_path):
    workspace, items = _workspace(tmp_path, [("a.mp3", {"title": "a"}), ("b.mp3", {"title": "b"})])
    service = TagActionService(builtin_registry())
    workspace.set_selected_items([items[1]])
    selected = service.preview(workspace, "tag.change_case.v1", scope="selected",
                               parameters={"field": "title", "mode": "upper"})
    assert selected.target_ids == (workspace.item_id(items[1]),)
    workspace.set_selected_items([items[0]])
    assert not service.accept(workspace, selected)
    workspace.set_visible_items([items[0]])
    visible = service.preview(workspace, "tag.change_case.v1", scope="visible",
                              parameters={"field": "title", "mode": "upper"})
    workspace.set_visible_items([items[1]])
    assert not service.accept(workspace, visible)
    current = service.preview(workspace, "tag.change_case.v1", scope="current",
                              current_item_id=workspace.item_id(items[1]),
                              parameters={"field": "title", "mode": "upper"})
    assert current.target_ids == (workspace.item_id(items[1]),)
    folder = service.preview(workspace, "tag.change_case.v1", scope="active_folder",
                             active_folder=tmp_path, parameters={"field": "title", "mode": "upper"})
    assert set(folder.target_ids) == {workspace.item_id(item) for item in items}


def test_generation_and_change_revision_invalidate_preview(tmp_path):
    workspace, items = _workspace(tmp_path, [("a.mp3", {"title": "a"})])
    service = TagActionService(builtin_registry())
    workspace.set_selected_items(items)
    preview = service.preview(workspace, "tag.change_case.v1", scope="selected",
                              parameters={"field": "title", "mode": "upper"})
    items[0].proposed.album = "new"; workspace.capture_proposals(items)
    assert not service.accept(workspace, preview)
    preview = service.preview(workspace, "tag.change_case.v1", item_ids=[workspace.item_id(items[0])],
                              parameters={"field": "title", "mode": "upper"})
    workspace.set_tracks(items)
    assert not service.accept(workspace, preview)


def test_accept_is_one_undo_command_preserves_origin_and_exclusion(tmp_path):
    workspace, items = _workspace(tmp_path, [("a.mp3", {"title": "first"})])
    item = items[0]; item.proposed.title = "manual"
    workspace.capture_proposals([item], ChangeOrigin.MANUAL)
    workspace.set_apply_excluded_ids({workspace.item_id(item)}, True)
    service = TagActionService(builtin_registry())
    preview = service.preview(workspace, "tag.change_case.v1", item_ids=[workspace.item_id(item)],
                              parameters={"field": "title", "mode": "upper"})
    assert service.accept(workspace, preview)
    record = workspace.change_set.records()[0]
    assert record.proposed_value == "MANUAL" and record.origin is ChangeOrigin.CLEANUP
    assert record.excluded_from_apply
    assert workspace.undo_proposals()
    restored = workspace.change_set.records()[0]
    assert restored.proposed_value == "manual" and restored.origin is ChangeOrigin.MANUAL
    assert workspace.redo_proposals()
    assert workspace.change_set.records()[0].proposed_value == "MANUAL"
    workspace.revert_items([item])
    assert not workspace.change_set.records()
    assert item.proposed.title is None


def test_action_sequence_is_composed_without_intermediate_mutation_and_undoes_once(tmp_path):
    workspace, items = _workspace(tmp_path, [("01_song.mp3", {"title": ""})])
    item = items[0]
    service = TagActionService(builtin_registry())
    preview = service.preview_sequence(workspace, (
        PresetStep("tag.title_from_filename.v1", {"strip_numbering": True}),
        PresetStep("tag.change_case.v1", {"field": "title", "mode": "title"}),
    ), item_ids=[workspace.item_id(item)])
    assert item.proposed.title is None and not workspace.change_set.records()
    assert service.accept(workspace, preview)
    assert item.proposed.title == "Song"
    assert workspace.undo_proposals() and item.proposed.title is None
    assert not workspace.undo_proposals()  # one coherent command, no intermediate step


def test_controller_legacy_delegate_keeps_action_origin_and_one_undo(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from PySide6.QtWidgets import QApplication
    from ui.controllers.metadata_controller import MetadataController

    QApplication.instance() or QApplication([])
    controller = MetadataController()
    try:
        workspace, items = _workspace(tmp_path / "controller", [("01_song.mp3", {"title": "old"})])
        item = items[0]
        controller.workspace_state.set_tracks([item])
        controller.apply_title_from_filename([item], True)
        records = controller.workspace_state.change_set.records()
        assert len(records) == 1
        assert records[0].origin is ChangeOrigin.CLEANUP
        assert records[0].proposed_value == "song"
        assert controller.workspace_state.undo_proposals()
        assert item.proposed.title is None
        assert not controller.workspace_state.undo_proposals()
    finally:
        controller.deleteLater()


def test_preview_and_accept_do_not_write_or_rename_media(tmp_path):
    workspace, items = _workspace(tmp_path, [("old.mp3", {"title": "new"})])
    before = items[0].path.read_bytes()
    service = TagActionService(builtin_registry())
    preview = service.preview(workspace, "file.from_title.v1", item_ids=[workspace.item_id(items[0])])
    assert items[0].path.exists() and not (tmp_path / "new.mp3").exists()
    assert service.accept(workspace, preview)
    assert items[0].path.read_bytes() == before and not (tmp_path / "new.mp3").exists()
    assert items[0].proposed_filename == "new.mp3"


def test_production_planner_reports_occupied_and_duplicate_destinations(tmp_path):
    workspace, items = _workspace(tmp_path, [("source.mp3", {"title": "taken"}), ("taken.mp3", {"title": "other"})])
    service = TagActionService(builtin_registry())
    occupied = service.preview(workspace, "file.from_title.v1", item_ids=[workspace.item_id(items[0])])
    assert occupied.collisions == {workspace.item_id(items[0])}
    workspace2, items2 = _workspace(tmp_path / "dupes", [("a.mp3", {"title": "same"}), ("b.mp3", {"title": "same"})])
    duplicate = service.preview(workspace2, "file.from_title.v1",
                                item_ids=[workspace2.item_id(item) for item in items2])
    assert duplicate.collisions == {workspace2.item_id(item) for item in items2}


def test_production_planner_supports_case_swap_and_cycle_without_mutation(tmp_path):
    service = TagActionService(builtin_registry())
    case_ws, case_items = _workspace(tmp_path / "case", [("song.mp3", {"title": "SONG"})])
    case = service.preview(case_ws, "file.from_title.v1", item_ids=[case_ws.item_id(case_items[0])])
    # A case-only rename ("song.mp3" -> "SONG.mp3") only needs the temp-hop
    # dance on a case-folding filesystem, where the two names refer to the same
    # physical file. On a case-sensitive one they are simply different,
    # unrelated names, so it is a normal one-step rename with nothing to hop
    # around. Asked of the filesystem actually holding the workspace rather
    # than inferred from sys.platform (issue #22).
    expected_case_steps = 2 if _dir_is_case_insensitive(str(case_items[0].path.parent)) else 1
    assert not case.blocked and len(case.rename_steps) == expected_case_steps
    swap_ws, swap_items = _workspace(tmp_path / "swap", [("a.mp3", {"title": "b"}), ("b.mp3", {"title": "a"})])
    swap = service.preview(swap_ws, "file.from_title.v1", item_ids=[swap_ws.item_id(item) for item in swap_items])
    assert not swap.blocked and len(swap.rename_steps) >= 3
    cycle_ws, cycle_items = _workspace(tmp_path / "cycle", [
        ("a.mp3", {"title": "b"}), ("b.mp3", {"title": "c"}), ("c.mp3", {"title": "a"})])
    cycle = service.preview(cycle_ws, "file.from_title.v1", item_ids=[cycle_ws.item_id(item) for item in cycle_items])
    assert not cycle.blocked and len(cycle.rename_steps) >= 4
    assert {item.path.name for item in cycle_items} == {"a.mp3", "b.mp3", "c.mp3"}


@pytest.mark.parametrize("title,diagnostic", [
    ("CON", "reserved_filename"), (".", "empty_filename"), ("x" * 260, "filename_too_long"),
])
def test_windows_filename_blockers_are_exact(title, diagnostic):
    action = builtin_registry().get("file.from_title.v1")
    result = action.evaluate(TagActionContext(1, "old.mp3", ".mp3", "mp3", {"title": title}))
    assert result.status is ActionResultStatus.BLOCKER and result.diagnostic == diagnostic


def test_windows_sanitation_blocks_injection_without_changing_extension():
    assert safe_filename("../escape", ".flac") == "escape.flac"
    assert safe_filename(r"C:\\Media\\song", ".mp3") == "C Media song.mp3"
    assert safe_filename("name. ", ".m4a") == "name.m4a"
    with pytest.raises(TemplateError, match="invalid_filename_characters"):
        safe_filename(r"..\\escape", ".mp3", sanitize=False)
    with pytest.raises(TemplateError, match="invalid_filename_characters"):
        safe_filename("bad:name", ".mp3", sanitize=False)


def test_template_optional_padding_unicode_bidi_and_round_trip():
    rendered = compile_template("{track_num:02} - {artist} - {title}[ - {album}]",
                                direction="tags_to_filename").render(
        {"track_num": 3, "artist": "שלום", "title": "Song", "album": ""})
    assert rendered == "03 - שלום - Song"
    parser = compile_template("{track_num:02} - {artist} - {title}", direction="filename_to_tags")
    assert parser.parse("03 - שלום - Song") == {"track_num": "03", "artist": "שלום", "title": "Song"}
    clean = compile_template("{artist} - {title}", direction="tags_to_filename").render(
        {"artist": "A\u202eB", "title": "שיר Song"})
    assert clean == "AB - שיר Song"


@pytest.mark.parametrize("source", ["{artist}{title}", "{artist}-{artist}", "{unknown}", "{title!r}"])
def test_template_security_and_ambiguity_rejects_unsafe_grammar(source):
    with pytest.raises(TemplateError):
        compile_template(source, direction="filename_to_tags")
