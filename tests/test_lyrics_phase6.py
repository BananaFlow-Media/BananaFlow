"""Canonical Lyrics and real Mutagen round trips for Phase 6."""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from core.metadata_models import ChangeAction, LyricsEntry, LyricsValue, OriginalTags, ProposedTags
from core.metadata_processor import atomic_write_tags, read_tags
from tests.audio_fixtures import make_empty_audio


def _make_compressed(path: Path, codec: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    command = [
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
        "-t", "0.05", "-c:a", codec, "-y", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def _fixture(tmp_path: Path, extension: str) -> Path:
    path = tmp_path / f"מילים mixed path {extension[1:]}{extension}"
    if extension in {".mp3", ".flac", ".m4a", ".wav"}:
        make_empty_audio(path)
    elif extension == ".opus":
        _make_compressed(path, "libopus")
    else:
        _make_compressed(path, "libvorbis")
    return path


def test_lyrics_canonical_normalization_and_primary_selection():
    value = LyricsValue((
        LyricsEntry("עברית\r\nEnglish  \n", language="heb", description="Other"),
        LyricsEntry("Primary\n", language="eng", description="Lyrics"),
    ))
    assert value.primary is not None and value.primary.text == "Primary\n"
    assert value.entries[0].text == "עברית\nEnglish  \n"
    replaced = value.replace_primary("חדש\nNew")
    assert replaced.primary.text == "חדש\nNew"
    assert replaced.entries[0] == value.entries[0]


def test_lyrics_absent_unchanged_set_and_clear_are_distinct():
    original = OriginalTags()
    proposal = ProposedTags()
    assert proposal.lyrics_change.action is ChangeAction.UNCHANGED
    proposal.set_lyrics("text")
    assert proposal.lyrics_change.action is ChangeAction.SET
    proposal.clear_lyrics()
    assert proposal.lyrics_change.action is ChangeAction.CLEAR
    assert not proposal.has_changes(original)


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"])
def test_real_lyrics_set_replace_clear_and_verify(tmp_path, extension):
    path = _fixture(tmp_path, extension)
    original = read_tags(path)
    set_proposal = ProposedTags()
    set_proposal.set_lyrics("שורה ראשונה\nEnglish line  \n", original=original.lyrics, language="heb", description="Lyrics")
    fields = atomic_write_tags(path, set_proposal, original)
    assert "lyrics" in fields
    stored = read_tags(path)
    assert stored.lyrics.primary.text == "שורה ראשונה\nEnglish line  \n"

    replace = ProposedTags()
    replace.set_lyrics("Replacement\nשורה", original=stored.lyrics)
    atomic_write_tags(path, replace, stored)
    replaced = read_tags(path)
    assert replaced.lyrics.primary.text == "Replacement\nשורה"

    clear = ProposedTags()
    clear.clear_lyrics()
    fields = atomic_write_tags(path, clear, replaced)
    assert "lyrics" in fields
    assert not read_tags(path).lyrics.has_unsynchronized


def test_id3_primary_edit_preserves_secondary_and_synchronized_entries(tmp_path):
    from mutagen.id3 import Encoding, ID3, SYLT, USLT

    path = _fixture(tmp_path, ".mp3")
    tags = ID3(str(path))
    tags.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="Lyrics", text="Primary"))
    tags.add(USLT(encoding=Encoding.UTF8, lang="heb", desc="Hebrew", text="משני"))
    tags.add(SYLT(
        encoding=Encoding.UTF8, lang="eng", format=2, type=1,
        desc="Timed", text=[("word", 0)],
    ))
    tags.save(str(path))

    original = read_tags(path)
    assert original.lyrics.secondary_count == 1
    assert original.lyrics.has_synchronized
    proposal = ProposedTags()
    proposal.set_lyrics("Changed", original=original.lyrics)
    atomic_write_tags(path, proposal, original)
    after = read_tags(path)
    assert after.lyrics.primary.text == "Changed"
    assert any(entry.text == "משני" for entry in after.lyrics.entries)
    assert after.lyrics.has_synchronized


def test_wav_audio_samples_are_unchanged_by_lyrics_apply(tmp_path):
    path = _fixture(tmp_path, ".wav")
    with wave.open(str(path), "rb") as source:
        before = source.readframes(source.getnframes())
    proposal = ProposedTags()
    proposal.set_lyrics("מילים", original=read_tags(path).lyrics)
    atomic_write_tags(path, proposal, read_tags(path))
    with wave.open(str(path), "rb") as source:
        after = source.readframes(source.getnframes())
    assert after == before
