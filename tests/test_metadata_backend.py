"""Phase 5 contract and real-container tests for the shared metadata backend."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.metadata_backend import CapabilityLevel, FORMAT_CAPABILITIES, METADATA_BACKEND
from core.metadata_models import ChangeAction, OriginalTags, ProposedTags, normalize_multi_value, metadata_values_equal
from core.metadata_processor import atomic_write_tags, build_track_item, read_tags
from tests.audio_fixtures import make_empty_audio


def test_capability_registry_is_explicit_for_app_outputs_and_future_formats():
    assert FORMAT_CAPABILITIES.by_id("mp3").level is CapabilityLevel.FULL
    assert FORMAT_CAPABILITIES.by_id("flac").level is CapabilityLevel.FULL
    assert FORMAT_CAPABILITIES.by_id("m4a").level is CapabilityLevel.FULL
    assert FORMAT_CAPABILITIES.by_id("opus").level is CapabilityLevel.FULL
    wav = FORMAT_CAPABILITIES.by_id("wav")
    assert wav.level is CapabilityLevel.LIMITED and "title" in wav.editable_fields
    assert FORMAT_CAPABILITIES.by_extension(".aac").level is CapabilityLevel.READ_ONLY
    assert FORMAT_CAPABILITIES.by_extension(".ape").level is CapabilityLevel.FUTURE
    assert FORMAT_CAPABILITIES.by_extension(".xyz").level is CapabilityLevel.UNSUPPORTED


def test_canonical_delta_distinguishes_unchanged_set_and_clear():
    original = OriginalTags(title="Before", track_num=3, track_total=10)
    delta = METADATA_BACKEND.proposal_delta(ProposedTags(title="", track_num=3, track_total=12), original)
    assert delta.changes["title"].action is ChangeAction.CLEAR
    assert delta.changes["track_total"].action is ChangeAction.SET
    assert "track_num" not in delta.changed_fields


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".m4a", ".wav"])
def test_real_format_roundtrip_clear_and_unknown_tag_preservation(tmp_path: Path, extension: str):
    path = tmp_path / f"track{extension}"
    make_empty_audio(path)
    initial = read_tags(path)
    atomic_write_tags(path, ProposedTags(title="Original", composer="Composer", disc_num=2, disc_total=3), initial)
    before = read_tags(path)
    assert before.title == "Original" and before.composer == "Composer"
    fields = atomic_write_tags(path, ProposedTags(title="Changed", composer="", disc_total=4), before)
    after = read_tags(path)
    assert {"title", "composer", "disc_total"}.issubset(fields)
    assert after.title == "Changed" and after.composer == "" and after.disc_num == 2 and after.disc_total == 4


def _make_opus(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable for real Opus fixture")
    completed = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.05", "-c:a", "libopus", "-y", str(path)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def _make_ogg_vorbis(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable for real Ogg Vorbis fixture")
    completed = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.05", "-c:a", "libvorbis", "-y", str(path)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_real_opus_roundtrip_clear_and_container_detection(tmp_path: Path):
    path = tmp_path / "track.opus"
    _make_opus(path)
    assert METADATA_BACKEND.detect(path).format_id == "opus"
    atomic_write_tags(path, ProposedTags(title="Opus title", artist="One; Two", isrc="ISRC-1"), read_tags(path))
    tagged = read_tags(path)
    assert tagged.title == "Opus title" and tagged.artist == "One; Two" and tagged.isrc == "ISRC-1"
    atomic_write_tags(path, ProposedTags(title="", isrc=""), tagged)
    after = read_tags(path)
    assert after.title == "" and after.isrc == "" and after.artist == "One; Two"


def test_read_only_file_is_excluded_from_metadata_editing(tmp_path: Path):
    path = tmp_path / "stream.aac"
    path.write_bytes(b"not audio")
    item = build_track_item(path)
    assert item.metadata_editable is False
    assert item.status == "read_only"


def test_container_detection_rejects_unsafe_extension_fallback(tmp_path: Path):
    wav = tmp_path / "misnamed.mp3"
    make_empty_audio(tmp_path / "source.wav")
    (tmp_path / "source.wav").replace(wav)
    before = wav.read_bytes()
    detected = METADATA_BACKEND.detect(wav)
    assert detected.format_id == "wav" and detected.detected_by == "container"
    with pytest.raises(ValueError):
        METADATA_BACKEND.write_legacy(wav, OriginalTags(title="unsafe"), {"title"}, format_id="mp3")
    assert wav.read_bytes() == before


def test_ogg_container_type_beats_extension(tmp_path: Path):
    opus = tmp_path / "opus.ogg"; _make_opus(opus)
    vorbis = tmp_path / "vorbis.opus"; _make_ogg_vorbis(vorbis)
    assert METADATA_BACKEND.detect(opus).format_id == "opus"
    assert METADATA_BACKEND.detect(vorbis).format_id == "ogg_vorbis"
    invalid = tmp_path / "invalid.mp3"; invalid.write_bytes(b"not a container")
    assert METADATA_BACKEND.detect(invalid).format_id == "unknown"


@pytest.mark.parametrize("number,total,proposal,expected", [
    (3, 10, ProposedTags(track_total=12), (3, 12)),
    (3, 10, ProposedTags(track_num=4), (4, 10)),
    (3, 10, ProposedTags(track_total=-1), (3, None)),
    (3, 10, ProposedTags(track_num=-1), (None, 10)),
    (None, None, ProposedTags(track_num=2, track_total=12), (2, 12)),
])
def test_m4a_track_components_round_trip_independently(tmp_path, number, total, proposal, expected):
    path = tmp_path / "track.m4a"; make_empty_audio(path)
    seed = OriginalTags(track_num=number, track_total=total)
    if number is not None or total is not None:
        atomic_write_tags(path, ProposedTags(track_num=number or -1, track_total=total or -1), read_tags(path))
    atomic_write_tags(path, proposal, read_tags(path))
    after = read_tags(path)
    assert (after.track_num, after.track_total) == expected


@pytest.mark.parametrize("number,total,proposal,expected", [
    (1, 2, ProposedTags(disc_total=3), (1, 3)),
    (1, 2, ProposedTags(disc_num=2), (2, 2)),
    (1, 2, ProposedTags(disc_total=-1), (1, None)),
    (1, 2, ProposedTags(disc_num=-1), (None, 2)),
])
def test_m4a_disc_components_round_trip_independently(tmp_path, number, total, proposal, expected):
    path = tmp_path / "disc.m4a"; make_empty_audio(path)
    atomic_write_tags(path, ProposedTags(disc_num=number, disc_total=total), read_tags(path))
    atomic_write_tags(path, proposal, read_tags(path))
    after = read_tags(path)
    assert (after.disc_num, after.disc_total) == expected


def test_multivalue_normalization_and_real_flac_m4a_roundtrip(tmp_path):
    assert normalize_multi_value("One;Two") == ("One", "Two")
    assert normalize_multi_value(["One", "Two"]) == ("One", "Two")
    assert metadata_values_equal("artist", "One;Two", ("One", "Two"))
    flac = tmp_path / "values.flac"; make_empty_audio(flac)
    atomic_write_tags(flac, ProposedTags(artist="One;Two", genre="Rock;Jazz"), read_tags(flac))
    assert read_tags(flac).artist == "One; Two" and read_tags(flac).genre == "Rock; Jazz"
    m4a = tmp_path / "values.m4a"; make_empty_audio(m4a)
    atomic_write_tags(m4a, ProposedTags(artist="One;Two", genre="Rock;Jazz"), read_tags(m4a))
    canonical = METADATA_BACKEND.read(m4a)
    assert canonical.values["artist"] == ("One", "Two")
    assert canonical.values["genre"] == ("Rock", "Jazz")
    atomic_write_tags(m4a, ProposedTags(artist="", genre=""), read_tags(m4a))
    assert "artist" not in METADATA_BACKEND.read(m4a).values
