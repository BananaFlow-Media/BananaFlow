"""B1/B10 regressions: only structurally valid, Qt-decodable artwork enters Apply."""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from core.artwork import ArtworkValidationError, validate_artwork_bytes
from core.metadata_backend import METADATA_BACKEND
from core.metadata_models import ArtworkReadState, ProposedTags
from core.metadata_processor import atomic_write_tags, read_tags
from tests.audio_fixtures import make_empty_audio


def image_bytes(fmt: str = "PNG") -> bytes:
    image = QImage(2, 2, QImage.Format.Format_RGB32); image.fill(QColor("red"))
    payload = QByteArray(); buffer = QBuffer(payload); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(payload)


def test_valid_png_and_jpeg_are_decodable_by_the_shared_boundary():
    assert validate_artwork_bytes(image_bytes("PNG")).mime_type == "image/png"
    assert validate_artwork_bytes(image_bytes("JPEG")).mime_type == "image/jpeg"


@pytest.mark.parametrize("mutate", [
    lambda data: data[:-1],                         # no IEND
    lambda data: data[:29] + b"\x00" + data[30:],  # invalid IHDR CRC
    lambda data: data.replace(b"IDAT", b"tDAT"),  # no image data
    lambda data: data[:20],                         # truncated chunk
    lambda data: b"\x89PNG\r\n\x1a\n" + b"random",
])
def test_malformed_png_is_rejected(mutate):
    with pytest.raises(ArtworkValidationError): validate_artwork_bytes(mutate(image_bytes()))


@pytest.mark.parametrize("mutate", [
    lambda data: data[:-2],                         # no EOI
    lambda data: data[:2] + b"\xff\xc0\x00\x08\x08\x00\x02\x00\x02\x03" + data[-2:],  # no SOS
    lambda data: data[:4] + b"\xff\xe0\xff\xff", # overflowing segment
])
def test_malformed_jpeg_is_rejected(mutate):
    with pytest.raises(ArtworkValidationError): validate_artwork_bytes(mutate(image_bytes("JPEG")))


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".m4a", ".wav"])
def test_invalid_artwork_never_reaches_real_writer(tmp_path: Path, extension: str):
    media = tmp_path / f"bad{extension}"; make_empty_audio(media)
    before = media.read_bytes()
    with pytest.raises(ArtworkValidationError):
        validate_artwork_bytes(b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x02\x00\x02\x03\xff\xd9")
    assert media.read_bytes() == before


def test_corrupt_embedded_apic_is_diagnostic_not_no_artwork(tmp_path: Path):
    from mutagen.id3 import APIC, ID3, Encoding
    media = tmp_path / "bad.mp3"; make_empty_audio(media)
    tags = ID3(str(media)); tags.add(APIC(encoding=Encoding.UTF8, mime="image/jpeg", type=3, desc="bad", data=b"\xff\xd8\xff\xd9")); tags.save(str(media))
    artwork = METADATA_BACKEND.read(media).artwork
    assert artwork.read_state is ArtworkReadState.INVALID
    assert artwork.diagnostics and not artwork.entries


def test_explicit_removal_can_repair_invalid_embedded_artwork(tmp_path: Path):
    from mutagen.id3 import APIC, ID3, Encoding
    media = tmp_path / "repair.mp3"; make_empty_audio(media)
    tags = ID3(str(media)); tags.add(APIC(encoding=Encoding.UTF8, mime="image/jpeg", type=3, desc="bad", data=b"\xff\xd8\xff\xd9")); tags.save(str(media))
    original = read_tags(media); proposal = ProposedTags(); proposal.remove_all_artwork(original=original.artwork)
    assert "artwork" in proposal.changed_fields(original)
    atomic_write_tags(media, proposal, original)
    assert not ID3(str(media)).getall("APIC")


def test_atomic_readback_rejects_an_undecodable_persisted_artwork(tmp_path: Path, monkeypatch):
    media = tmp_path / "verify.mp3"; make_empty_audio(media)
    original = read_tags(media); proposed = ProposedTags(); proposed.set_artwork(validate_artwork_bytes(image_bytes()), original=original.artwork)
    monkeypatch.setattr("core.metadata_backend.MetadataBackend._read_artwork", staticmethod(lambda *_: __import__("core.metadata_models", fromlist=["ArtworkValue"]).ArtworkValue(read_state=ArtworkReadState.INVALID)))
    with pytest.raises(Exception): atomic_write_tags(media, proposed, original)
