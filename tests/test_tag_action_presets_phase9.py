from __future__ import annotations

import json

import pytest

from core.tag_action_presets import PresetStep, PresetStore, SCHEMA, TagActionPreset
from core.tag_actions import builtin_registry


def test_schema1_migrates_deterministically_to_current_schema(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({"schema": 1, "presets": [
        {"id": "z", "name": "Zulu", "action_id": "tags_to_filename",
         "parameters": {"template": "{title}"}},
        {"id": "a", "name": "Alpha", "action_id": "filename_to_tags",
         "parameters": {"template": "{title}"}},
    ]}), encoding="utf-8")
    store = PresetStore(path, builtin_registry())
    presets, diagnostic = store.load()
    assert diagnostic == "preset_store_migrated"
    assert [preset.name for preset in presets] == ["Alpha", "Zulu"]
    assert [preset.action_id for preset in presets] == [
        "template.filename_to_tags.v1", "template.tags_to_filename.v1",
    ]
    store.save(presets)
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == SCHEMA


def test_malformed_entries_unknown_actions_and_absolute_paths_are_ignored(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({"schema": SCHEMA, "presets": [
        {"id": "good", "name": "Good", "action_id": "tag.change_case.v1",
         "parameters": {"field": "title", "mode": "upper"}},
        {"id": "unknown", "name": "Unknown", "action_id": "evil.action", "parameters": {}},
        {"id": "path", "name": "Path", "action_id": "tag.change_case.v1",
         "parameters": {"value": "C:\\Music\\secret.mp3"}},
        {"id": "bad", "name": "Bad", "action_id": "tag.change_case.v1", "parameters": object().__class__.__name__},
    ]}), encoding="utf-8")
    presets, diagnostic = PresetStore(path, builtin_registry()).load()
    assert diagnostic is None and [preset.id for preset in presets] == ["good"]


def test_atomic_save_readback_and_previous_backup(tmp_path):
    store = PresetStore(tmp_path / "presets.json", builtin_registry())
    first = store.create("First", "tag.change_case.v1", {"field": "title", "mode": "upper"})
    store.save([first])
    previous = store.path.read_bytes()
    second = store.create("Second", "tag.normalize_spaces.v1", {"field": "title"})
    store.save([second])
    assert store.path.with_suffix(".json.bak").read_bytes() == previous
    assert store.load()[0] == [second]


def test_crud_builtin_immutability_and_copy_to_custom():
    builtin = PresetStore.builtins()[0]
    with pytest.raises(ValueError, match="builtin_preset_immutable"):
        PresetStore.rename(builtin, "Changed")
    custom = PresetStore.duplicate(builtin, "Editable copy")
    assert not custom.builtin and custom.id != builtin.id
    renamed = PresetStore.rename(custom, "Renamed")
    assert renamed.name == "Renamed" and renamed.version == 2
    updated = PresetStore.update(renamed, action_id="tag.change_case.v1",
                                 parameters={"field": "title", "mode": "lower"})
    assert updated.version == 3 and updated.action_id == "tag.change_case.v1"
    assert PresetStore.delete([updated], updated.id) == []
    with pytest.raises(ValueError, match="builtin_preset_immutable"):
        PresetStore.delete([builtin], builtin.id)


def test_legacy_auto_ops_migrate_to_versioned_action_sequence():
    preset = PresetStore.migrate_legacy_auto_ops([
        "title_strip", "track_num", "normalize_spaces", "unknown",
    ])
    assert preset.builtin
    assert [step.action_id for step in preset.steps] == [
        "tag.title_from_filename.v1", "tag.track_from_filename.v1", "tag.normalize_spaces.v1",
    ]
    assert preset.steps[0].parameters == {"strip_numbering": True}


def test_sequences_round_trip_without_transient_ids_or_qt_objects(tmp_path):
    store = PresetStore(tmp_path / "presets.json", builtin_registry())
    preset = TagActionPreset(
        "sequence", "Cleanup", "", {}, False, 1,
        (PresetStep("tag.normalize_spaces.v1", {"field": "title"}),
         PresetStep("tag.change_case.v1", {"field": "title", "mode": "title"})),
    )
    store.save([preset])
    loaded, diagnostic = store.load()
    assert diagnostic is None and loaded == [preset]
    raw = store.path.read_text(encoding="utf-8")
    assert "workspace" not in raw and "PySide" not in raw


def test_preset_parameter_sanitation_rejects_non_json_and_media_paths():
    with pytest.raises(ValueError, match="preset_parameter_type_invalid"):
        PresetStore.create("Bad", "tag.change_case.v1", {"object": object()})
    with pytest.raises(ValueError, match="preset_absolute_path_forbidden"):
        PresetStore.create("Bad", "tag.change_case.v1", {"path": "D:/Media/song.mp3"})
