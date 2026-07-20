"""Focused Phase 7 artwork contracts using real temporary media containers."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from core.artwork import ArtworkValidationError, validate_artwork_bytes
from core.metadata_models import ARTWORK_FIELD, ArtworkEntry, ArtworkValue, ChangeAction, FieldChange, ProposedTags
from core.metadata_processor import atomic_write_tags, read_tags
from tests.audio_fixtures import make_empty_audio

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACXBIWXMAAA9hAAAPYQGoP6dpAAAADUlEQVQImWP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _entry(data: bytes = PNG, picture_type: int = 3, description: str = "Front") -> ArtworkEntry:
    entry = validate_artwork_bytes(data, description=description, picture_type=picture_type)
    return entry


def test_artwork_value_has_stable_semantic_identity_and_primary_policy():
    front, back = _entry(picture_type=3), _entry(picture_type=4, description="Back")
    value = ArtworkValue((back, front))
    assert value.primary is front
    assert value.semantically_equal(ArtworkValue((back, front)))
    assert not value.semantically_equal(ArtworkValue((front, back)))
    assert value.without_primary().entries == (back,)


def test_artwork_validation_rejects_bad_or_oversized_payloads():
    with pytest.raises(ArtworkValidationError):
        validate_artwork_bytes(b"not an image")
    with pytest.raises(ArtworkValidationError):
        validate_artwork_bytes(b"x" * (20 * 1024 * 1024 + 1))


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".m4a"])
def test_artwork_apply_replace_remove_preserves_secondary_entries(tmp_path: Path, extension: str):
    media = tmp_path / f"art{extension}"
    make_empty_audio(media)
    original = read_tags(media)
    first = _entry()
    proposal = ProposedTags(); proposal.set_artwork(first, original=original.artwork)
    written = atomic_write_tags(media, proposal, original)
    assert ARTWORK_FIELD in written
    after_add = read_tags(media)
    assert after_add.artwork.primary and after_add.artwork.primary.content_hash == first.content_hash

    # Add an unrelated secondary picture directly to the canonical intended set;
    # replacing the primary must retain it after real writer/readback.
    secondary = _entry(picture_type=4, description="Back")
    with_secondary = ArtworkValue((after_add.artwork.primary, secondary))
    direct = ProposedTags(); direct.artwork_change = FieldChange(ChangeAction.SET, with_secondary)
    atomic_write_tags(media, direct, after_add)
    seeded = read_tags(media)
    replacement = _entry(description="New front")
    replace = ProposedTags(); replace.set_artwork(replacement, original=seeded.artwork)
    atomic_write_tags(media, replace, seeded)
    replaced = read_tags(media)
    assert len(replaced.artwork.entries) == 2
    if extension == ".m4a":
        # MP4 covr intentionally has no standardized picture-type/description.
        assert any(entry.content_hash == secondary.content_hash for entry in replaced.artwork.entries)
    else:
        assert any(entry.picture_type == 4 and entry.description == "Back" for entry in replaced.artwork.entries)
    remove = ProposedTags(); remove.remove_artwork(original=replaced.artwork)
    atomic_write_tags(media, remove, replaced)
    assert len(read_tags(media).artwork.entries) == 1


def test_artwork_only_proposal_is_in_memory_until_apply(tmp_path: Path):
    media = tmp_path / "pending.mp3"; make_empty_audio(media)
    original = read_tags(media)
    proposal = ProposedTags(); proposal.set_artwork(_entry(), original=original.artwork)
    assert ARTWORK_FIELD in proposal.changed_fields(original)
    assert not read_tags(media).artwork.entries
