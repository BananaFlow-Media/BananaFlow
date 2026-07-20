"""B8 deterministic multi-entry artwork export safety."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.artwork import ArtworkValidationError, export_artwork_entries, safe_artwork_export_name, validate_artwork_bytes
from tests.test_artwork_validation_phase7 import image_bytes


def test_export_all_entries_uses_original_bytes_and_deterministic_names(tmp_path: Path):
    front = validate_artwork_bytes(image_bytes(), picture_type=3, description="Front:cover")
    back = validate_artwork_bytes(image_bytes("JPEG"), picture_type=4, description="Back")
    paths = export_artwork_entries(tmp_path, "AUX", (front, back))
    assert [path.name for path in paths] == ["AUX-01-front-Front_cover.png", "AUX-02-back-Back.jpg"]
    assert paths[0].read_bytes() == front.data and paths[1].read_bytes() == back.data


def test_export_rejects_collisions_and_invalid_destination(tmp_path: Path):
    entry = validate_artwork_bytes(image_bytes())
    (tmp_path / safe_artwork_export_name("song", entry, 0)).write_bytes(b"existing")
    with pytest.raises(ArtworkValidationError): export_artwork_entries(tmp_path, "song", (entry,))
    with pytest.raises(ArtworkValidationError): export_artwork_entries(tmp_path / "missing", "song", (entry,))
