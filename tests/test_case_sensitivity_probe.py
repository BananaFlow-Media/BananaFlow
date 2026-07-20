"""
tests/test_case_sensitivity_probe.py  –  Real filesystem case-folding detection
========================================================================
Issue #22 was closed by making two tests agree with
``_CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"`` — a
guess about the *operating system* standing in for a fact about the
*filesystem*. That made Linux CI green without establishing that the
assumption it encodes is true, and the assumption is not:

* a music library on an exFAT/NTFS external drive mounted under Linux folds
  case, and the guess says it does not;
* macOS APFS can be formatted case-sensitive, and the guess says it folds.

Both wrong answers land in ``plan_renames``' collision detection, where the
consequence is a rename planned against the wrong collision model — either a
legitimate case-only rename blocked as a collision, or two genuinely distinct
files treated as one. So the guess is replaced by asking the filesystem, and
these tests cover the probe and the planner behavior that depends on it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.metadata_processor as mp
from core.metadata_models import AudioTrackItem, OriginalTags


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The probe is lru_cached per directory; tests that patch it or reuse a
    path must not inherit another test's answer."""
    mp._dir_is_case_insensitive.cache_clear()
    yield
    mp._dir_is_case_insensitive.cache_clear()


class TestProbe:

    def test_probe_agrees_with_what_the_filesystem_actually_does(self, tmp_path):
        """The probe's answer must match a direct experiment on the same
        directory — this is the correctness anchor for everything below."""
        (tmp_path / "probe.mp3").write_bytes(b"x")
        really_folds = (tmp_path / "PROBE.MP3").exists()

        assert mp._dir_is_case_insensitive(str(tmp_path)) is really_folds

    def test_probe_leaves_no_files_behind(self, tmp_path):
        before = set(os.listdir(tmp_path))
        mp._dir_is_case_insensitive(str(tmp_path))
        assert set(os.listdir(tmp_path)) == before, (
            "the case probe must clean up after itself — it runs inside the "
            "user's music library, not a scratch directory"
        )

    def test_a_failing_close_still_removes_the_probe_file(self, tmp_path, monkeypatch):
        """Safety review blocker: os.close can raise (EBADF/EINTR). It used to
        sit before the cleanup handler was armed, so a raise there left the
        probe file in the user's music folder permanently — with the only path
        that could remove it discarded along the stack frame."""
        before = set(os.listdir(tmp_path))
        real_close = os.close

        def exploding_close(fd):
            real_close(fd)
            raise OSError(9, "Bad file descriptor")

        monkeypatch.setattr(os, "close", exploding_close)
        result = mp._dir_is_case_insensitive(str(tmp_path))

        assert result is mp._PLATFORM_CASE_INSENSITIVE_DEFAULT, (
            "a probe that could not complete must fall back, not guess"
        )
        assert set(os.listdir(tmp_path)) == before, (
            "the probe file must be removed even when os.close raises"
        )

    def test_probe_result_is_cached_per_directory(self, tmp_path):
        first = mp._dir_is_case_insensitive(str(tmp_path))
        info = mp._dir_is_case_insensitive.cache_info()
        second = mp._dir_is_case_insensitive(str(tmp_path))

        assert first == second
        assert mp._dir_is_case_insensitive.cache_info().hits == info.hits + 1, (
            "a repeated probe of the same directory must be served from cache "
            "— plan_renames calls this once per path"
        )

    def test_unprobeable_directory_falls_back_to_the_platform_default(self, tmp_path):
        """A directory that cannot be written to (or does not exist) is one
        nothing is about to be renamed in; answer from the platform rather
        than raising into the planner."""
        missing = tmp_path / "does-not-exist"

        assert mp._dir_is_case_insensitive(str(missing)) is mp._PLATFORM_CASE_INSENSITIVE_DEFAULT

    def test_platform_default_is_only_a_fallback_not_the_answer(self):
        """Guard the actual regression: if someone reintroduces a module-level
        constant as the answer, this file's premise is gone."""
        source = Path(mp.__file__).read_text(encoding="utf-8")
        assert "_CASE_INSENSITIVE_FS" not in source, (
            "case folding must be probed per directory (issue #22), not "
            "decided once per process from sys.platform"
        )


class TestNormalizationFollowsTheProbe:
    """``_rename_norm`` is where the probe's answer reaches the planner: it
    decides whether two spellings are one path key or two, which is what
    ``plan_renames`` builds its collision and move graph out of.

    These force the probe to each answer and assert the normalization changes
    with it. Deliberately tested at this level rather than through
    ``plan_renames``: the planner also calls ``Path.exists()`` against the real
    filesystem, so forcing "case-sensitive" while running on a folding volume
    (or vice versa) describes a filesystem that cannot exist, and any
    assertion about the resulting plan would be about that contradiction
    rather than about the code.
    """

    def _with_probe(self, directory: Path, folds: bool):
        original = mp._dir_is_case_insensitive

        class _Forced:
            def __enter__(_self):
                mp._dir_is_case_insensitive = (
                    lambda d: folds if d == str(directory) else original(d))
                return _self

            def __exit__(_self, *a):
                mp._dir_is_case_insensitive = original
                return False

        return _Forced()

    def test_folding_filesystem_makes_case_variants_the_same_path(self, tmp_path):
        with self._with_probe(tmp_path, folds=True):
            assert mp._same_file_ci(tmp_path / "song.mp3", tmp_path / "SONG.mp3"), (
                "on a folding filesystem these two spellings name the same "
                "file, so a rename between them is the safe case-only path"
            )

    def test_case_sensitive_filesystem_keeps_case_variants_distinct(self, tmp_path):
        with self._with_probe(tmp_path, folds=False):
            assert not mp._same_file_ci(tmp_path / "song.mp3", tmp_path / "SONG.mp3"), (
                "on a case-sensitive filesystem these are two unrelated files; "
                "conflating them would let one rename clobber the other"
            )

    def test_identical_paths_are_the_same_file_either_way(self, tmp_path):
        for folds in (True, False):
            with self._with_probe(tmp_path, folds=folds):
                assert mp._same_file_ci(tmp_path / "song.mp3", tmp_path / "song.mp3")

    def test_two_directories_on_different_filesystems_are_judged_separately(self, tmp_path):
        """The real point of probing per directory: one process can hold a
        folding external drive and a case-sensitive home directory at once,
        which a single module-level constant cannot express."""
        folding = tmp_path / "external"
        sensitive = tmp_path / "home"
        folding.mkdir()
        sensitive.mkdir()

        original = mp._dir_is_case_insensitive
        try:
            mp._dir_is_case_insensitive = lambda d: d == str(folding)
            assert mp._same_file_ci(folding / "a.mp3", folding / "A.mp3")
            assert not mp._same_file_ci(sensitive / "a.mp3", sensitive / "A.mp3")
        finally:
            mp._dir_is_case_insensitive = original


class TestPlannerAgreesWithTheRealFilesystem:
    """End-to-end on whatever filesystem the test actually runs on."""

    def _track(self, path: Path) -> AudioTrackItem:
        path.write_bytes(b"media")
        return AudioTrackItem(path, path.parent, path.suffix, original=OriginalTags(title="t"))

    def test_a_case_only_rename_is_planned_not_blocked(self, tmp_path):
        """This is the behavior issue #22's 'case' parameter was skipped over
        on Linux. It must hold on every filesystem — folding or not — because
        on a folding one it is the temp-hop path and on a case-sensitive one
        the destination simply does not exist."""
        track = self._track(tmp_path / "song.mp3")
        track.proposed_filename = "SONG.mp3"

        plan = mp.plan_renames([track])

        assert not plan.blocked, f"a case-only rename must not be blocked: {plan.blocked}"
        assert plan.components, "expected a rename component to be planned"

    def test_renaming_onto_an_independently_existing_file_is_always_blocked(self, tmp_path):
        """The collision that must be caught regardless of case folding."""
        track = self._track(tmp_path / "song.mp3")
        (tmp_path / "occupied.mp3").write_bytes(b"a different file")
        track.proposed_filename = "occupied.mp3"

        plan = mp.plan_renames([track])

        assert plan.blocked, "renaming onto an existing, unrelated file must be blocked"

    def test_step_count_matches_what_the_probe_says_about_this_directory(self, tmp_path):
        """A case-only rename needs the temp hop only where the two names are
        the same file. Rather than asserting a platform-specific number, this
        asserts the plan is consistent with what the filesystem reported."""
        track = self._track(tmp_path / "song.mp3")
        track.proposed_filename = "SONG.mp3"

        plan = mp.plan_renames([track])
        steps = sum(len(component.steps) for component in plan.components)

        expected = 2 if mp._dir_is_case_insensitive(str(tmp_path)) else 1
        assert steps == expected, (
            f"{steps} rename step(s) planned but the filesystem at {tmp_path} "
            f"reports case-folding={mp._dir_is_case_insensitive(str(tmp_path))}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
