"""B2-B4 schema integrity, authoritative empty artwork, and atomic restore."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.metadata_models import ArtworkValue, OriginalTags, ProposedTags, RestoreStatus
from core.metadata_processor import ApplyWriteError, backup_tags, build_track_item, load_tag_backup, restore_tags
from tests.audio_fixtures import make_empty_audio
from tests.test_artwork_validation_phase7 import image_bytes
from core.artwork import validate_artwork_bytes


def _with_artwork() -> OriginalTags:
    entry = validate_artwork_bytes(image_bytes())
    return OriginalTags(artwork=ArtworkValue((entry,)), artwork_captured=True)


def test_schema3_hash_tampering_is_rejected_before_restore(tmp_path: Path):
    media = tmp_path / "a.mp3"; make_empty_audio(media)
    item = build_track_item(media); item.original = _with_artwork()
    backup = tmp_path / "backup.json"; backup_tags([item], backup)
    raw = json.loads(backup.read_text(encoding="utf-8")); raw["records"][0]["original"]["artwork"]["entries"][0]["data"] = "aGVsbG8="
    backup.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError): load_tag_backup(backup)


@pytest.mark.parametrize("field,value", [("content_hash", "0" * 64), ("content_hash", "bad"), ("content_hash", None)])
def test_schema3_bad_or_missing_hash_is_rejected(tmp_path: Path, field, value):
    media = tmp_path / "a.mp3"; make_empty_audio(media); item = build_track_item(media); item.original = _with_artwork()
    backup = tmp_path / "backup.json"; backup_tags([item], backup); raw = json.loads(backup.read_text(encoding="utf-8"))
    entry = raw["records"][0]["original"]["artwork"]["entries"][0]
    if value is None: entry.pop(field)
    else: entry[field] = value
    backup.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError): load_tag_backup(backup)


def test_schema3_empty_artwork_is_authoritative_but_schema2_is_not(tmp_path: Path):
    media = tmp_path / "a.mp3"; make_empty_audio(media)
    saved = OriginalTags(artwork=ArtworkValue(), artwork_captured=True)
    current = build_track_item(media).original
    proposal = __import__("core.metadata_processor", fromlist=["_restore_proposal"])._restore_proposal(saved, current)
    assert not proposal.has_changes(current)
    current_with = OriginalTags(artwork=ArtworkValue((validate_artwork_bytes(image_bytes()),)), artwork_captured=True)
    proposal = __import__("core.metadata_processor", fromlist=["_restore_proposal"])._restore_proposal(saved, current_with)
    assert "artwork" in proposal.changed_fields(current_with)
    legacy = OriginalTags(artwork=ArtworkValue(), artwork_captured=False)
    assert "artwork" not in __import__("core.metadata_processor", fromlist=["_restore_proposal"])._restore_proposal(legacy, current_with).changed_fields(current_with)


def test_real_schema3_empty_backup_removes_later_artwork(tmp_path: Path):
    from core.metadata_processor import atomic_write_tags, read_tags
    media = tmp_path / "empty.mp3"; make_empty_audio(media)
    backup = tmp_path / "empty.json"; backup_tags([build_track_item(media)], backup)
    current = read_tags(media); proposal = ProposedTags(); proposal.set_artwork(validate_artwork_bytes(image_bytes()), original=current.artwork); atomic_write_tags(media, proposal, current)
    assert read_tags(media).artwork.entries
    outcome = restore_tags(load_tag_backup(backup))[0]
    assert outcome.status is RestoreStatus.RESTORED and not read_tags(media).artwork.entries


def test_restore_uses_atomic_writer_and_reports_verification_failure(tmp_path: Path, monkeypatch):
    media = tmp_path / "a.mp3"; make_empty_audio(media)
    current = build_track_item(media); saved = OriginalTags(title="saved", artwork_captured=True)
    monkeypatch.setattr("core.metadata_processor.build_track_item", lambda _: current)
    monkeypatch.setattr("core.metadata_processor.atomic_write_tags", lambda *_: (_ for _ in ()).throw(ApplyWriteError("verify", "verify_failed", "no-op")))
    outcome = restore_tags([(media, saved)])[0]
    assert outcome.status is RestoreStatus.FAILED and "verify" in outcome.error


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".m4a", ".wav"])
def test_real_atomic_restore_returns_saved_artwork_or_authoritative_empty(tmp_path: Path, extension: str):
    from core.metadata_processor import atomic_write_tags, read_tags
    media = tmp_path / f"restore{extension}"; make_empty_audio(media)
    original = read_tags(media); saved_entry = validate_artwork_bytes(image_bytes())
    seed = ProposedTags(); seed.set_artwork(saved_entry, original=original.artwork); atomic_write_tags(media, seed, original)
    backup_item = build_track_item(media); backup = tmp_path / f"{extension}.json"; backup_tags([backup_item], backup)
    current = read_tags(media); changed = ProposedTags(); changed.set_artwork(validate_artwork_bytes(image_bytes("JPEG")), original=current.artwork); atomic_write_tags(media, changed, current)
    outcome = restore_tags(load_tag_backup(backup))[0]
    assert outcome.status is RestoreStatus.RESTORED
    assert read_tags(media).artwork.primary.content_hash == saved_entry.content_hash
