import json
from pathlib import Path

from core.preset_transfer import (
    PresetConflictDecision, PresetConflictPolicy, PresetImportState, accept_preset_import,
    build_transfer_package, export_transfer_package, preview_preset_import,
)
from core.metadata_io import IOErrorKind
from core.tag_action_presets import PresetStep, PresetStore, TagActionPreset
from core.tag_actions import builtin_registry


def custom(preset_id="custom.one", name="One"):
    return TagActionPreset(preset_id, name, "tag.set_field.v1", {}, False, 1,
                           (PresetStep("tag.set_field.v1", {"field": "title", "value": "New"}),))


def test_selected_custom_export_excludes_builtins_and_roundtrips(tmp_path):
    registry = builtin_registry()
    package = build_transfer_package([custom(), *PresetStore.builtins()], registry)
    assert [preset.id for preset in package.presets] == ["custom.one"]
    destination = tmp_path / "two.bananaflow-presets.json"
    export_transfer_package(package, destination)
    raw = json.loads(destination.read_text(encoding="utf-8"))
    assert raw["schema"] == "bananaflow.presets.transfer.v1"
    assert raw["product"] == "bananaflow"
    assert raw["presets"][0]["steps"][0]["action_id"] == "tag.set_field.v1"
    assert not any(entry["id"].startswith("builtin.") for entry in raw["presets"])


def test_import_preview_conflicts_unknown_actions_invalid_paths_and_duplicates(tmp_path):
    registry = builtin_registry(); path = tmp_path / "package.json"
    payload = {
        "schema": "bananaflow.presets.transfer.v1", "product": "bananaflow", "exported_at": "now",
        "presets": [
            {"id": "custom.one", "name": "Conflict", "version": 1,
             "steps": [{"action_id": "tag.set_field.v1", "parameters": {"field": "title", "value": "X"}}]},
            {"id": "unknown", "name": "Unknown", "version": 1,
             "steps": [{"action_id": "tag.not-real.v1", "parameters": {}}]},
            {"id": "path", "name": "Path", "version": 1,
             "steps": [{"action_id": "tag.set_field.v1", "parameters": {"field": "title", "value": "C:\\secret"}}]},
            {"id": "custom.one", "name": "Duplicate", "version": 1,
             "steps": [{"action_id": "tag.set_field.v1", "parameters": {"field": "title", "value": "Y"}}]},
            {"id": "builtin.filename.artist-title.v1", "name": "Builtin", "version": 1,
             "steps": [{"action_id": "tag.set_field.v1", "parameters": {"field": "title", "value": "Y"}}]},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = PresetStore(tmp_path / "preview-store.json", registry); store.save([custom()])
    preview = preview_preset_import(path, registry=registry, store=store)
    assert [item.state for item in preview.items] == [
        PresetImportState.EXISTING_CUSTOM_CONFLICT,
        PresetImportState.UNKNOWN_ACTION,
        PresetImportState.INVALID_PARAMETERS,
        PresetImportState.DUPLICATE_PACKAGE_ID,
        PresetImportState.BUILTIN_CONFLICT,
    ]
    blocked = accept_preset_import(preview, store=store, decisions=(
        PresetConflictDecision(4, "builtin.filename.artist-title.v1",
                               PresetConflictPolicy.REPLACE_CUSTOM),))
    assert not blocked.accepted and blocked.error.kind is IOErrorKind.INVALID_MAPPING
    assert [preset.id for preset in store.load()[0]] == ["custom.one"]


def test_conflict_policies_are_atomic_and_execute_no_action(tmp_path):
    registry = builtin_registry(); package_path = tmp_path / "package.json"
    export_transfer_package(build_transfer_package([custom(name="Imported")], registry), package_path)
    existing = [custom(name="Existing")]
    for policy, expected_count, imported in (
        (PresetConflictPolicy.SKIP, 1, 0),
        (PresetConflictPolicy.KEEP_BOTH, 2, 1),
        (PresetConflictPolicy.REPLACE_CUSTOM, 1, 1),
    ):
        store_path = tmp_path / f"{policy.value}.json"
        store = PresetStore(store_path, registry); store.save(existing)
        preview = preview_preset_import(package_path, registry=registry, store=store)
        result = accept_preset_import(preview, store=store, existing_custom=existing, policy=policy)
        loaded, diagnostic = store.load()
        assert diagnostic is None and len(loaded) == expected_count and result.imported == imported
        assert all(not preset.builtin for preset in loaded)
        assert not (tmp_path / "metadata_changes.json").exists()


def test_unsupported_schema_is_structured_and_never_changes_store(tmp_path):
    path = tmp_path / "future.json"; path.write_text('{"schema":"future","presets":[]}', encoding="utf-8")
    registry = builtin_registry(); store = PresetStore(tmp_path / "store.json", registry); store.save([custom()])
    preview = preview_preset_import(path, registry=registry, store=store)
    assert preview.items[0].state is PresetImportState.UNSUPPORTED_SCHEMA
    result = accept_preset_import(preview, store=store, existing_custom=[custom()])
    assert result.accepted and result.imported == 0
    assert [preset.id for preset in store.load()[0]] == ["custom.one"]


def test_conflict_policy_is_per_preset_and_existing_store_is_revalidated(tmp_path):
    registry = builtin_registry(); path = tmp_path / "mixed.json"
    incoming = [custom("custom.one", "Incoming One"), custom("custom.two", "Incoming Two")]
    export_transfer_package(build_transfer_package(incoming, registry), path)
    existing = [custom("custom.one", "Existing One"), custom("custom.two", "Existing Two")]
    store = PresetStore(tmp_path / "mixed-store.json", registry); store.save(existing)
    preview = preview_preset_import(path, registry=registry, store=store)
    result = accept_preset_import(preview, store=store, existing_custom=existing,
        policy_by_id={"custom.one": PresetConflictPolicy.KEEP_BOTH,
                      "custom.two": PresetConflictPolicy.RENAME},
        rename_by_id={"custom.two": "Renamed Two"})
    loaded, _ = store.load()
    assert result.accepted and result.imported == 2 and len(loaded) == 4
    assert "Renamed Two" in {preset.name for preset in loaded}

    stale = accept_preset_import(preview, store=store, existing_custom=loaded)
    assert not stale.accepted and stale.error.kind.value == "stale_preview"


def test_store_change_after_preview_is_rejected_and_refresh_preserves_new_presets(tmp_path):
    registry = builtin_registry(); package_path = tmp_path / "new.json"
    incoming = custom("custom.c", "Incoming C")
    export_transfer_package(build_transfer_package([incoming], registry), package_path)
    store = PresetStore(tmp_path / "store.json", registry)
    preset_a, preset_b = custom("custom.a", "A"), custom("custom.b", "B")
    store.save([preset_a])
    stale_preview = preview_preset_import(package_path, registry=registry, store=store)
    store.save([preset_a, preset_b])

    rejected = accept_preset_import(stale_preview, store=store)
    assert not rejected.accepted and rejected.error.kind is IOErrorKind.STALE_PREVIEW
    assert {preset.id for preset in store.load()[0]} == {"custom.a", "custom.b"}

    refreshed = preview_preset_import(package_path, registry=registry, store=store)
    accepted = accept_preset_import(refreshed, store=store)
    assert accepted.accepted and accepted.imported == 1
    assert {preset.id for preset in store.load()[0]} == {"custom.a", "custom.b", "custom.c"}


def test_store_conflict_appearing_or_disappearing_requires_a_fresh_preview(tmp_path):
    registry = builtin_registry(); package_path = tmp_path / "conflict-change.json"
    incoming = custom("custom.c", "Incoming C")
    export_transfer_package(build_transfer_package([incoming], registry), package_path)
    store = PresetStore(tmp_path / "conflict-store.json", registry)
    preset_a, preset_c = custom("custom.a", "A"), custom("custom.c", "Existing C")

    store.save([preset_a])
    formerly_valid = preview_preset_import(package_path, registry=registry, store=store)
    assert formerly_valid.items[0].state is PresetImportState.VALID
    store.save([preset_a, preset_c])
    appeared = accept_preset_import(formerly_valid, store=store)
    assert not appeared.accepted and appeared.error.kind is IOErrorKind.STALE_PREVIEW
    fresh_conflict = preview_preset_import(package_path, registry=registry, store=store)
    assert fresh_conflict.items[0].state is PresetImportState.EXISTING_CUSTOM_CONFLICT

    store.save([preset_a])
    disappeared = accept_preset_import(fresh_conflict, store=store, decisions=(
        PresetConflictDecision(0, "custom.c", PresetConflictPolicy.REPLACE_CUSTOM),))
    assert not disappeared.accepted and disappeared.error.kind is IOErrorKind.STALE_PREVIEW
    fresh_valid = preview_preset_import(package_path, registry=registry, store=store)
    accepted = accept_preset_import(fresh_valid, store=store)
    assert accepted.accepted and accepted.imported == 1


def test_per_row_decisions_support_replace_keep_both_rename_and_skip_together(tmp_path):
    registry = builtin_registry(); package_path = tmp_path / "four.json"
    incoming = [custom(f"custom.{letter}", f"Incoming {letter.upper()}")
                for letter in "abcd"]
    export_transfer_package(build_transfer_package(incoming, registry), package_path)
    store = PresetStore(tmp_path / "four-store.json", registry)
    store.save([custom(f"custom.{letter}", f"Existing {letter.upper()}")
                for letter in "abcd"])
    preview = preview_preset_import(package_path, registry=registry, store=store)
    decisions = (
        PresetConflictDecision(0, "custom.a", PresetConflictPolicy.REPLACE_CUSTOM),
        PresetConflictDecision(1, "custom.b", PresetConflictPolicy.KEEP_BOTH),
        PresetConflictDecision(2, "custom.c", PresetConflictPolicy.RENAME, "Renamed C"),
        PresetConflictDecision(3, "custom.d", PresetConflictPolicy.SKIP),
    )
    result = accept_preset_import(preview, store=store, decisions=decisions)
    loaded = store.load()[0]
    assert result.accepted and result.imported == 3 and result.skipped == 1
    assert len(loaded) == 6
    by_name = {preset.name: preset for preset in loaded}
    assert "Incoming A" in by_name and "Existing A" not in by_name
    assert "Existing B" in by_name and "Incoming B" in by_name
    assert "Existing C" in by_name and "Renamed C" in by_name
    assert "Existing D" in by_name and "Incoming D" not in by_name


def test_missing_duplicate_and_stale_per_row_decisions_are_safe(tmp_path):
    registry = builtin_registry(); package_path = tmp_path / "one.json"
    export_transfer_package(build_transfer_package([custom()], registry), package_path)
    store = PresetStore(tmp_path / "decision-store.json", registry); store.save([custom(name="Existing")])
    preview = preview_preset_import(package_path, registry=registry, store=store)

    skipped = accept_preset_import(preview, store=store, decisions=())
    assert skipped.accepted and skipped.imported == 0 and skipped.skipped == 1
    preview = preview_preset_import(package_path, registry=registry, store=store)
    duplicate = accept_preset_import(preview, store=store, decisions=(
        PresetConflictDecision(0, "custom.one", PresetConflictPolicy.REPLACE_CUSTOM),
        PresetConflictDecision(0, "custom.one", PresetConflictPolicy.KEEP_BOTH),
    ))
    assert not duplicate.accepted and duplicate.error.kind is IOErrorKind.INVALID_MAPPING
    stale = accept_preset_import(preview, store=store, decisions=(
        PresetConflictDecision(0, "different.id", PresetConflictPolicy.REPLACE_CUSTOM),))
    assert not stale.accepted and stale.error.kind is IOErrorKind.STALE_PREVIEW


def test_corrupt_or_disappeared_store_after_preview_fails_structurally(tmp_path):
    registry = builtin_registry(); package_path = tmp_path / "package.json"
    export_transfer_package(build_transfer_package([custom("custom.new", "New")], registry), package_path)
    store = PresetStore(tmp_path / "corrupt-store.json", registry); store.save([custom()])
    preview = preview_preset_import(package_path, registry=registry, store=store)
    store.path.write_text("{broken", encoding="utf-8")
    corrupt = accept_preset_import(preview, store=store)
    assert not corrupt.accepted and corrupt.error.kind is IOErrorKind.INVALID_FORMAT

    store.save([custom()])
    preview = preview_preset_import(package_path, registry=registry, store=store)
    store.path.rename(tmp_path / "moved-store.json")
    disappeared = accept_preset_import(preview, store=store)
    assert not disappeared.accepted and disappeared.error.kind is IOErrorKind.STALE_PREVIEW
