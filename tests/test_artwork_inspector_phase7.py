"""B5-B7 Inspector proposal semantics without direct media writes."""
from __future__ import annotations

from pathlib import Path

from core.metadata_inspector import MetadataInspectorState, ValueState
from core.metadata_models import ARTWORK_FIELD, AudioTrackItem, ArtworkOperation, ArtworkValue, OriginalTags
from core.artwork import validate_artwork_bytes
from tests.test_artwork_validation_phase7 import image_bytes


def track(tmp_path: Path, name: str, artwork=(), fmt="mp3", editable=True):
    return AudioTrackItem(tmp_path / name, tmp_path, "." + fmt, original=OriginalTags(
        artwork=ArtworkValue(tuple(artwork)), artwork_captured=True), format_id=fmt, metadata_editable=editable)


def test_add_and_replace_are_distinct_and_preserve_the_existing_primary(tmp_path: Path):
    state = MetadataInspectorState(); first = validate_artwork_bytes(image_bytes(), picture_type=3); new = validate_artwork_bytes(image_bytes("JPEG"), picture_type=4)
    item = track(tmp_path, "a.mp3", (first,))
    result = state.propose_add_artwork([item], new)
    assert result.affected_count == 1
    effective = state.effective_value(item, ARTWORK_FIELD)
    assert effective.operation is ArtworkOperation.ADD and effective.entries == (first, new)
    item.proposed.revert_artwork()
    state.propose_set([item], ARTWORK_FIELD, new)
    effective = state.effective_value(item, ARTWORK_FIELD)
    assert effective.operation is ArtworkOperation.REPLACE and effective.entries == (new,)


def test_mp4_add_is_honestly_disabled_by_the_canonical_capability(tmp_path: Path):
    item = track(tmp_path, "a.m4a", (), fmt="m4a")
    result = MetadataInspectorState().propose_add_artwork([item], validate_artwork_bytes(image_bytes()))
    assert result.affected_count == 0 and result.unsupported_count == 1 and not item.proposed.has_changes(item.original)


def test_mixed_selection_stays_mixed_until_explicit_remove_from_all(tmp_path: Path):
    a = validate_artwork_bytes(image_bytes()); b = validate_artwork_bytes(image_bytes("JPEG"))
    one, two = track(tmp_path, "one.mp3", (a,)), track(tmp_path, "two.mp3", (b,))
    state = MetadataInspectorState()
    assert state.field_state([one, two], ARTWORK_FIELD).value_state is ValueState.MIXED
    result = state.propose_clear([one, two], ARTWORK_FIELD)
    assert result.affected_count == 2
    assert not state.effective_value(one, ARTWORK_FIELD).entries
    assert not state.effective_value(two, ARTWORK_FIELD).entries


def test_mixed_remove_skips_no_artwork_and_read_only_targets(tmp_path: Path):
    art = validate_artwork_bytes(image_bytes()); writable = track(tmp_path, "one.mp3", (art,)); none = track(tmp_path, "none.mp3", ()); ro = track(tmp_path, "ro.aac", (art,), fmt="aac", editable=False)
    result = MetadataInspectorState().propose_clear([writable, none, ro], ARTWORK_FIELD)
    assert result.affected_count == 1 and result.unsupported_count == 1
    assert not writable.original.artwork.entries or writable.proposed.has_changes(writable.original)
    assert not none.proposed.has_changes(none.original) and not ro.proposed.has_changes(ro.original)
