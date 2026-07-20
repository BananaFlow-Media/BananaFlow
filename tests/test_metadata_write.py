"""
tests/test_metadata_write.py  –  Real MP3/FLAC/M4A tag write/read round-trips
================================================================================
Phase 4 investigates the "known FLAC/M4A year tag edge case" flagged in
earlier audits. Using tests/audio_fixtures.py's minimal-but-real
containers (mutagen actually opens/tags/saves/re-reads these — no
mocking of read_tags/write_tags/mutagen itself), this file:

* proves the bug: year is silently never written for FLAC/M4A, and
  never *cleared* for MP3 (every sibling field deletes its frame/atom
  when set to "" — year was the one field that didn't),
* pins every other field's round-trip so a future regression in any
  format's write path is caught immediately,
* pins the "no real change → no write at all" contract, and safe
  handling of missing/None track number and year.

No destructive writes outside tmp_path — every fixture file lives under
pytest's per-test temporary directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.metadata_processor import read_tags, write_tags
from core.metadata_models import OriginalTags, ProposedTags
from tests.audio_fixtures import make_empty_audio

FORMATS = [".mp3", ".flac", ".m4a"]


def _fixture(tmp_path: Path, ext: str) -> Path:
    p = tmp_path / f"song{ext}"
    make_empty_audio(p)
    return p


class TestFieldRoundTrip:
    """Every field write_tags supports must actually persist and read
    back identically, on all three supported formats."""

    @pytest.mark.parametrize("ext", FORMATS)
    def test_all_fields_round_trip(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        original = read_tags(p)
        proposed = ProposedTags(
            title="Song Title", artist="Artist Name", album="Album Name",
            album_artist="Album Artist", track_num=7, comment="A comment",
            year="2024", genre="Rock",
        )
        assert write_tags(p, proposed, original) is True

        after = read_tags(p)
        assert after.title == "Song Title"
        assert after.artist == "Artist Name"
        assert after.album == "Album Name"
        assert after.album_artist == "Album Artist"
        assert after.track_num == 7
        assert after.comment == "A comment"
        assert after.genre == "Rock"
        assert after.year == "2024", (
            f"{ext}: year did not round-trip (wrote '2024', read back "
            f"{after.year!r}) — this is the known FLAC/M4A year edge case"
        )

    @pytest.mark.parametrize("ext", FORMATS)
    def test_track_total_round_trips_alongside_track_num(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        original = read_tags(p)
        proposed = ProposedTags(track_num=3)
        # track_total isn't a ProposedTags field (it rides along with
        # track_num via effective_tags picking up original.track_total,
        # which is None here) — just confirm track_num alone round-trips
        # without requiring a total.
        assert write_tags(p, proposed, original)
        after = read_tags(p)
        assert after.track_num == 3
        assert after.track_total is None


class TestYearFieldSpecifically:
    """The year field, isolated: this is what earlier audits flagged as
    a FLAC/M4A edge case, plus the MP3 clear-year gap that code reading
    during Phase 4 additionally found."""

    @pytest.mark.parametrize("ext", FORMATS)
    def test_setting_year_on_untagged_file_persists(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        original = read_tags(p)
        assert write_tags(p, ProposedTags(year="1999"), original)
        assert read_tags(p).year == "1999"

    @pytest.mark.parametrize("ext", FORMATS)
    def test_changing_an_existing_year_persists(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        write_tags(p, ProposedTags(year="1999"), read_tags(p))
        mid = read_tags(p)
        assert write_tags(p, ProposedTags(year="2024"), mid)
        assert read_tags(p).year == "2024"

    @pytest.mark.parametrize("ext", FORMATS)
    def test_clearing_year_removes_it(self, tmp_path, ext):
        """ProposedTags.year = "" is the Tag Editor's "clear this field"
        convention (see MetadataController.clear_year) — every other
        field deletes its underlying frame/atom when cleared this way;
        year must behave the same, not silently keep the old value."""
        p = _fixture(tmp_path, ext)
        write_tags(p, ProposedTags(year="2020"), read_tags(p))
        tagged = read_tags(p)
        assert tagged.year == "2020"

        assert write_tags(p, ProposedTags(year=""), tagged)
        cleared = read_tags(p)
        assert cleared.year == "", (
            f"{ext}: clearing the year field left {cleared.year!r} on disk "
            f"instead of removing it"
        )


class TestNoPointlessWrites:
    """write_tags must not touch the file at all when nothing actually
    changes — has_changes() is the gate before any format-specific
    writer runs."""

    @pytest.mark.parametrize("ext", FORMATS)
    def test_identical_proposed_is_a_no_op(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        write_tags(p, ProposedTags(title="Same", year="2020"), read_tags(p))
        original = read_tags(p)
        mtime_before = p.stat().st_mtime_ns

        # Proposed carries the exact same values as original — no real change.
        same_proposed = ProposedTags(title="Same", year="2020")
        assert write_tags(p, same_proposed, original) is True
        assert p.stat().st_mtime_ns == mtime_before, (
            f"{ext}: write_tags touched the file even though nothing changed"
        )

    @pytest.mark.parametrize("ext", FORMATS)
    def test_all_none_proposed_is_a_no_op(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        original = read_tags(p)
        mtime_before = p.stat().st_mtime_ns
        assert write_tags(p, ProposedTags(), original) is True
        assert p.stat().st_mtime_ns == mtime_before


class TestMissingFieldsHandledSafely:
    """None/absent track number and year must never raise, on any format."""

    @pytest.mark.parametrize("ext", FORMATS)
    def test_none_track_num_does_not_raise(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        original = read_tags(p)
        assert original.track_num is None
        proposed = ProposedTags(title="No track number")
        assert write_tags(p, proposed, original) is True
        assert read_tags(p).track_num is None

    @pytest.mark.parametrize("ext", FORMATS)
    def test_clearing_track_num_removes_it(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        write_tags(p, ProposedTags(track_num=5), read_tags(p))
        tagged = read_tags(p)
        assert tagged.track_num == 5

        # ProposedTags convention: -1 means "clear the track number".
        assert write_tags(p, ProposedTags(track_num=-1), tagged)
        assert read_tags(p).track_num is None

    @pytest.mark.parametrize("ext", FORMATS)
    def test_empty_year_on_untagged_file_does_not_raise(self, tmp_path, ext):
        p = _fixture(tmp_path, ext)
        original = read_tags(p)
        assert original.year == ""
        assert write_tags(p, ProposedTags(title="X", year=""), original) is True
        assert read_tags(p).year == ""


class TestWriteErrorHandling:

    def test_unsupported_extension_returns_false(self, tmp_path):
        p = tmp_path / "song.wav"
        p.write_bytes(b"")
        original = OriginalTags()
        proposed = ProposedTags(title="X")
        assert write_tags(p, proposed, original) is False

    def test_permission_error_is_caught_and_returns_false(self, tmp_path, monkeypatch):
        p = _fixture(tmp_path, ".mp3")
        original = read_tags(p)

        def _raise_permission_error(*a, **k):
            raise PermissionError("locked")

        # The processor is now a compatibility boundary; format writing lives
        # in the canonical shared backend.
        monkeypatch.setattr("core.metadata_processor.METADATA_BACKEND.write_legacy", _raise_permission_error)
        assert write_tags(p, ProposedTags(title="X"), original) is False
