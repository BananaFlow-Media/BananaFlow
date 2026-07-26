"""
tests/test_batch_workspace_paths.py  –  utils.paths.make_batch_workspace
=============================================================================
The batch download workspace must be:
  * created fresh and uniquely per call (no collisions across concurrent
    batches),
  * on the SAME volume as the output directory it's nested under, so the
    later atomic-publish os.replace() is guaranteed same-filesystem,
  * genuinely hidden on Windows (the Hidden file attribute, not just a
    dot-prefixed name — a user with "show hidden files" on should see a
    dimmed folder, not a normal one).

Pure stdlib, no Qt.
"""

from __future__ import annotations

import os

import pytest

from utils.paths import make_batch_workspace


def test_creates_a_real_directory(tmp_path):
    workspace = make_batch_workspace(str(tmp_path))
    assert workspace.exists()
    assert workspace.is_dir()


def test_nested_under_base_output_dir(tmp_path):
    """Same-volume guarantee for the atomic publish step: the workspace
    must live INSIDE base_output_dir, not some unrelated system temp dir."""
    workspace = make_batch_workspace(str(tmp_path))
    assert tmp_path.resolve() in workspace.resolve().parents


def test_two_calls_produce_different_workspaces(tmp_path):
    a = make_batch_workspace(str(tmp_path))
    b = make_batch_workspace(str(tmp_path))
    assert a != b
    assert a.exists() and b.exists()


def test_container_folder_name_is_dot_prefixed(tmp_path):
    """Even where the Windows Hidden attribute doesn't apply (non-Windows),
    a dot-prefix hides it by the same convention as get_app_data_dir()."""
    workspace = make_batch_workspace(str(tmp_path))
    container = workspace.parent
    assert container.name == ".bananaflow_tmp"
    assert container.parent == tmp_path.resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hidden attribute")
class TestWindowsHiddenAttribute:
    def test_workspace_has_hidden_attribute(self, tmp_path):
        import ctypes

        workspace = make_batch_workspace(str(tmp_path))
        FILE_ATTRIBUTE_HIDDEN = 0x02
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(workspace))
        assert attrs & FILE_ATTRIBUTE_HIDDEN, (
            "batch workspace must carry the real Windows Hidden attribute, "
            "not just a dot-prefixed name — a user must never see it in a "
            "normal Explorer window"
        )

    def test_container_has_hidden_attribute(self, tmp_path):
        import ctypes

        workspace = make_batch_workspace(str(tmp_path))
        FILE_ATTRIBUTE_HIDDEN = 0x02
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(workspace.parent))
        assert attrs & FILE_ATTRIBUTE_HIDDEN

    def test_set_hidden_attribute_returns_true_on_success(self, tmp_path):
        from utils.paths import _set_hidden_attribute

        d = tmp_path / "somedir"
        d.mkdir()
        assert _set_hidden_attribute(d) is True

    def test_set_hidden_attribute_reports_failure_not_false_success(self, tmp_path, caplog):
        """The whole point of checking SetFileAttributesW's return value: a
        genuine failure (here: a path that doesn't exist) must return False
        and log, never silently claim success."""
        from utils.paths import _set_hidden_attribute

        missing = tmp_path / "does_not_exist"
        result = _set_hidden_attribute(missing, retry_delay_s=0.0)
        assert result is False

    def test_transient_failure_then_success_is_reported_as_success(self, tmp_path, monkeypatch):
        """A directory just created can transiently fail this call (e.g. an
        antivirus/indexer briefly holding a handle) — a retry that then
        succeeds must report True, not give up after one attempt."""
        import utils.paths as paths_mod

        d = tmp_path / "somedir"
        d.mkdir()
        calls = {"n": 0}

        import ctypes as real_ctypes
        original = real_ctypes.windll.kernel32.SetFileAttributesW

        def _flaky(path, attrs):
            calls["n"] += 1
            if calls["n"] < 3:
                return 0  # FALSE — simulated transient failure
            return original(path, attrs)

        monkeypatch.setattr(
            real_ctypes.windll.kernel32, "SetFileAttributesW", _flaky, raising=False,
        )

        result = paths_mod._set_hidden_attribute(d, attempts=3, retry_delay_s=0.0)
        assert result is True
        assert calls["n"] == 3

    def test_persistent_failure_gives_up_after_all_attempts(self, tmp_path, monkeypatch):
        import ctypes as real_ctypes

        calls = {"n": 0}

        def _always_fail(path, attrs):
            calls["n"] += 1
            return 0

        monkeypatch.setattr(
            real_ctypes.windll.kernel32, "SetFileAttributesW", _always_fail, raising=False,
        )

        import utils.paths as paths_mod
        d = tmp_path / "somedir"
        d.mkdir()
        result = paths_mod._set_hidden_attribute(d, attempts=3, retry_delay_s=0.0)
        assert result is False
        assert calls["n"] == 3  # every attempt was actually tried, not just one


def test_set_hidden_attribute_is_true_on_non_windows_by_convention():
    """On non-Windows there is no attribute to set; the dot-prefixed name is
    the hiding mechanism, so the helper reports success (True) rather than a
    spurious failure. Simulated by forcing os.name."""
    import utils.paths as paths_mod

    if os.name == "nt":
        pytest.skip("covered by the real-attribute tests on Windows")
    # On a genuinely non-Windows runner this exercises the real path.
    from utils.paths import _set_hidden_attribute
    from pathlib import Path
    assert _set_hidden_attribute(Path(".")) is True


def test_falls_back_to_app_data_when_output_dir_cannot_hold_a_workspace(tmp_path, monkeypatch):
    """If the same-volume container can't be created (here: a FILE occupies
    the `.bananaflow_tmp` name), make_batch_workspace must still ISOLATE the
    download — falling back to the app-data dir — rather than either raising
    or (worse) letting the caller write visible partials into output_dir.
    The invariant 'downloads never appear in the output folder' wins."""
    from utils import paths as paths_mod

    # A distinct base for the app-data fallback, NOT nested under the output
    # dir, so "landed in the fallback, not the output dir" is unambiguous.
    app_data = tmp_path.parent / f"appdata-{tmp_path.name}"
    app_data.mkdir()
    monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    blocker = output_dir / ".bananaflow_tmp"
    blocker.write_bytes(b"not a directory")

    workspace = make_batch_workspace(str(output_dir))

    assert workspace.exists()
    # Landed under the app-data fallback, NOT inside the output dir.
    assert app_data.resolve() in workspace.resolve().parents
    assert output_dir.resolve() not in workspace.resolve().parents


def test_raises_only_when_no_location_is_writable(tmp_path, monkeypatch):
    """Both the same-volume container and the app-data fallback failing is
    the only case that raises — the caller then errors the jobs rather than
    writing visible partials."""
    from utils import paths as paths_mod

    # Block the same-volume location with a file...
    (tmp_path / ".bananaflow_tmp").write_bytes(b"x")
    # ...and point the app-data fallback at an unusable location too.
    app_data_blocker = tmp_path / "appdata_file"
    app_data_blocker.write_bytes(b"x")
    monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data_blocker)

    with pytest.raises(OSError):
        make_batch_workspace(str(tmp_path))


def test_falls_back_to_app_data_when_primary_location_cannot_be_hidden(tmp_path, monkeypatch):
    """Genuinely hidden is a hard product requirement, not a cosmetic
    nicety: a location that CAN hold a workspace but can't be made hidden
    must be rejected in favour of the app-data fallback, exactly like a
    location that can't even create the directory — never silently expose
    the workspace just because the primary location happened to work."""
    from utils import paths as paths_mod

    app_data = tmp_path.parent / f"appdata-hide-{tmp_path.name}"
    app_data.mkdir()
    monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data)

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    primary_container = output_dir / ".bananaflow_tmp"

    real_set_hidden = paths_mod._set_hidden_attribute

    def _fail_only_under_primary(path, **kwargs):
        if str(path).startswith(str(primary_container)):
            return False
        return real_set_hidden(path, **kwargs)

    monkeypatch.setattr(paths_mod, "_set_hidden_attribute", _fail_only_under_primary)

    workspace = make_batch_workspace(str(output_dir))

    assert workspace.exists()
    assert app_data.resolve() in workspace.resolve().parents
    assert output_dir.resolve() not in workspace.resolve().parents
    # The rejected primary attempt must not leave a visible partial behind.
    assert not primary_container.exists() or not any(primary_container.iterdir())


def test_raises_when_no_location_can_be_hidden(tmp_path, monkeypatch):
    """If every candidate location creates fine but none can be hidden, this
    must raise (so the caller fails the jobs) rather than falling through to
    a visible workspace."""
    from utils import paths as paths_mod

    app_data = tmp_path.parent / f"appdata-hide2-{tmp_path.name}"
    app_data.mkdir()
    monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data)
    monkeypatch.setattr(paths_mod, "_set_hidden_attribute", lambda path, **kwargs: False)

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(OSError):
        make_batch_workspace(str(output_dir))


# ── Filesystem-safe workspace removal (remove_workspace_tree) ────────────────
# The single authority that keeps cancellation / stale cleanup from ever
# escaping the BananaFlow workspace tree into the user's own files.

class TestRemoveWorkspaceTree:
    def _make_container(self, base):
        from utils.paths import make_batch_workspace
        ws = make_batch_workspace(str(base))       # base/.bananaflow_tmp/batch-<id>
        (ws / "job-a").mkdir()
        (ws / "job-a" / "song.part").write_bytes(b"partial")
        return ws  # the batch-<id> container

    def test_removes_a_batch_container(self, tmp_path):
        from utils.paths import remove_workspace_tree
        container = self._make_container(tmp_path)
        assert container.exists()
        remove_workspace_tree(container)
        assert not container.exists()

    def test_removes_a_single_job_subdir_leaving_siblings(self, tmp_path):
        from utils.paths import remove_workspace_tree
        container = self._make_container(tmp_path)
        (container / "job-b").mkdir()
        (container / "job-b" / "keep.part").write_bytes(b"x")

        remove_workspace_tree(container / "job-a")

        assert not (container / "job-a").exists()
        assert (container / "job-b" / "keep.part").exists()

    def test_prunes_empty_container_and_root(self, tmp_path):
        from utils.paths import remove_workspace_tree
        container = self._make_container(tmp_path)
        root = container.parent  # .bananaflow_tmp

        remove_workspace_tree(container)

        assert not container.exists()
        assert not root.exists(), "empty .bananaflow_tmp root should be pruned too"
        assert tmp_path.exists(), "the user's output dir must NEVER be removed"

    def test_refuses_to_remove_a_non_workspace_path(self, tmp_path, caplog):
        """The core safety invariant: a path that is not inside a BananaFlow
        workspace container must never be deleted, even if asked."""
        from utils.paths import remove_workspace_tree

        precious = tmp_path / "user_music"
        precious.mkdir()
        (precious / "important.mp3").write_bytes(b"do not delete")

        remove_workspace_tree(precious)

        assert precious.exists()
        assert (precious / "important.mp3").exists()

    def test_refuses_to_remove_the_output_dir_itself(self, tmp_path):
        from utils.paths import remove_workspace_tree
        # Even the container's grandparent (the output dir) is off-limits.
        container = self._make_container(tmp_path)
        remove_workspace_tree(tmp_path)
        assert tmp_path.exists()

    def test_traversal_path_cannot_escape_into_a_sibling_directory(self, tmp_path):
        """A path whose lexical components include workspace-marker names
        (.bananaflow_tmp, batch-x) -- so a naive name-only check would
        wrongly accept it -- but which resolves via '..' segments to
        somewhere OUTSIDE any BananaFlow workspace must never be touched."""
        from utils.paths import make_batch_workspace, remove_workspace_tree

        precious = tmp_path / "precious_dir"
        precious.mkdir()
        (precious / "important.mp3").write_bytes(b"do not delete")

        # A real, owned container, so the traversal is rejected on where it
        # RESOLVES to and not merely on the root being unrecorded.
        container = make_batch_workspace(str(tmp_path))
        traversal = container / ".." / ".." / "precious_dir"
        assert traversal.resolve() == precious.resolve()

        remove_workspace_tree(traversal)

        assert precious.exists()
        assert (precious / "important.mp3").exists()

    def test_refuses_a_lookalike_directory_the_app_never_created(self, tmp_path):
        """Ownership by containment, not by name. A user folder that merely
        LOOKS like ours -- a `batch-…` directory, or anything under a folder
        someone happened to call `download_workspaces` -- was previously
        accepted by the name-based check and handed to a recursive delete."""
        from utils.paths import remove_workspace_tree

        lookalike_batch = tmp_path / "batch-2019"
        lookalike_batch.mkdir()
        (lookalike_batch / "wedding.mp3").write_bytes(b"irreplaceable")

        lookalike_root = tmp_path / "download_workspaces" / "mixes"
        lookalike_root.mkdir(parents=True)
        (lookalike_root / "set.mp3").write_bytes(b"irreplaceable")

        remove_workspace_tree(lookalike_batch)
        remove_workspace_tree(lookalike_root)

        assert (lookalike_batch / "wedding.mp3").exists()
        assert (lookalike_root / "set.mp3").exists()

    def test_refuses_a_workspace_under_an_unrecorded_root(self, tmp_path):
        """Even the exact container layout is refused under a root this
        installation never recorded creating a workspace under — that
        directory belongs to someone else."""
        from utils.paths import remove_workspace_tree

        foreign = tmp_path / "someone_elses_output" / ".bananaflow_tmp" / "batch-zzz"
        foreign.mkdir(parents=True)
        (foreign / "keep.part").write_bytes(b"not ours")

        remove_workspace_tree(foreign)

        assert (foreign / "keep.part").exists()


class TestIsWorkspacePath:
    def test_recognises_a_container_we_created(self, tmp_path):
        from utils.paths import is_workspace_path, make_batch_workspace

        workspace = make_batch_workspace(str(tmp_path))
        assert is_workspace_path(workspace.parent)          # .bananaflow_tmp
        assert is_workspace_path(workspace)                 # batch-<id>
        assert is_workspace_path(workspace / "job-1" / "x.part")

    def test_recognises_the_app_data_fallback_container(self, tmp_path):
        from utils.paths import get_app_data_dir, is_workspace_path

        container = get_app_data_dir() / "download_workspaces"
        assert is_workspace_path(container)
        assert is_workspace_path(container / "batch-abc" / "job-1")

    def test_rejects_arbitrary_user_path(self, tmp_path):
        from utils.paths import is_workspace_path
        assert not is_workspace_path(tmp_path)
        assert not is_workspace_path(tmp_path / "Music" / "song.mp3")

    def test_rejects_lookalike_names_outside_an_owned_container(self, tmp_path):
        """Names are not ownership: `batch-…` anywhere, or anything under a
        user folder called `download_workspaces`, is not ours."""
        from utils.paths import is_workspace_path

        assert not is_workspace_path(tmp_path / "batch-abc")
        assert not is_workspace_path(tmp_path / "download_workspaces")
        assert not is_workspace_path(tmp_path / "download_workspaces" / "batch-abc")
        assert not is_workspace_path(tmp_path / ".bananaflow_tmp" / "batch-abc")

    def test_rejects_traversal_path_that_resolves_outside_the_workspace(self, tmp_path):
        """Component names alone must not be enough -- the path has to
        actually RESOLVE inside an owned container, not just look like it
        before its '..' segments are collapsed."""
        from utils.paths import is_workspace_path, make_batch_workspace

        workspace = make_batch_workspace(str(tmp_path))
        (tmp_path / "precious_dir").mkdir()

        traversal = workspace / ".." / ".." / "precious_dir"
        assert not is_workspace_path(traversal)

    def test_accepts_traversal_path_that_resolves_to_a_real_workspace_dir(self, tmp_path):
        """The flip side: a path with redundant '..' segments that still
        genuinely resolves INSIDE the workspace must keep working -- the
        check must resolve-then-check, not reject every non-canonical path."""
        from utils.paths import is_workspace_path, make_batch_workspace

        workspace = make_batch_workspace(str(tmp_path))
        (workspace / "job-1").mkdir()

        traversal = workspace / "job-1" / ".." / "job-1"
        assert is_workspace_path(traversal)


# ── Stale-workspace sweep (startup recovery) ─────────────────────────────────

class TestSweepStaleWorkspaces:
    def _mk(self, out, jobname="jobA"):
        from utils.paths import make_batch_workspace
        w = make_batch_workspace(str(out))
        (w / jobname).mkdir()
        (w / jobname / "x.part").write_bytes(b"p")
        return w

    def test_removes_abandoned_keeps_valid(self, tmp_path):
        from utils.paths import sweep_stale_workspaces
        out = tmp_path / "out"
        out.mkdir()
        valid = self._mk(out)
        abandoned1 = self._mk(out)
        abandoned2 = self._mk(out)

        removed = sweep_stale_workspaces([str(out)], [str(valid / "jobA")])

        assert valid.exists() and (valid / "jobA" / "x.part").exists()
        assert not abandoned1.exists()
        assert not abandoned2.exists()
        assert set(removed) == {abandoned1, abandoned2}

    def test_empty_keep_removes_everything(self, tmp_path):
        from utils.paths import sweep_stale_workspaces
        out = tmp_path / "out"
        out.mkdir()
        a = self._mk(out)
        b = self._mk(out)
        sweep_stale_workspaces([str(out)], [])
        assert not a.exists() and not b.exists()

    def test_keeps_all_containers_that_hold_a_valid_job(self, tmp_path):
        """Keeping any subdir of a container keeps the whole container."""
        from utils.paths import sweep_stale_workspaces
        out = tmp_path / "out"
        out.mkdir()
        w = self._mk(out, "jobA")
        (w / "jobB").mkdir()
        (w / "jobB" / "y.part").write_bytes(b"p")

        sweep_stale_workspaces([str(out)], [str(w / "jobA")])

        assert w.exists()
        assert (w / "jobB" / "y.part").exists(), "sibling in a kept container survives"

    def test_never_touches_the_output_dir_or_user_files(self, tmp_path):
        from utils.paths import sweep_stale_workspaces
        out = tmp_path / "out"
        out.mkdir()
        (out / "user_song.mp3").write_bytes(b"keep me")
        self._mk(out)

        sweep_stale_workspaces([str(out)], [])

        assert out.exists()
        assert (out / "user_song.mp3").read_bytes() == b"keep me"

    def test_no_workspaces_is_a_noop(self, tmp_path):
        from utils.paths import sweep_stale_workspaces
        out = tmp_path / "out"
        out.mkdir()
        removed = sweep_stale_workspaces([str(out)], [])
        assert removed == []

    def test_finds_a_workspace_under_a_previously_used_output_dir(self, tmp_path):
        """An output directory the user has since changed away from is still
        swept: it is reachable through the recorded output roots. Passing
        only the CURRENT output dir left such a workspace permanently
        invisible to cleanup once no persisted paused job pointed at it."""
        from utils.paths import sweep_stale_workspaces

        old_out = tmp_path / "old"
        new_out = tmp_path / "new"
        old_out.mkdir()
        new_out.mkdir()
        stranded = self._mk(old_out)          # records `old` as an output root
        self._mk(new_out)                     # the user's current directory

        removed = sweep_stale_workspaces([str(new_out)], [])

        assert not stranded.exists(), (
            "a workspace under a previously-used output dir must still be "
            "discoverable, with nothing left pointing at it"
        )
        assert stranded in removed


# ── Recorded output roots ────────────────────────────────────────────────────
# The persisted record of "output directories BananaFlow has actually placed a
# workspace container under". Two later mechanisms depend on it: proving a
# path is ours before deleting it, and rediscovering a workspace stranded
# under an output directory the user has since changed away from.

class TestKnownOutputRoots:
    def test_creating_a_workspace_records_its_output_root(self, tmp_path):
        from utils.paths import known_output_roots

        out = tmp_path / "out"
        out.mkdir()
        make_batch_workspace(str(out))

        assert out.resolve() in known_output_roots()

    def test_recording_is_idempotent_across_batches(self, tmp_path):
        from utils.paths import known_output_roots

        out = tmp_path / "out"
        out.mkdir()
        make_batch_workspace(str(out))
        make_batch_workspace(str(out))
        make_batch_workspace(str(out))

        assert [p for p in known_output_roots() if p == out.resolve()] == [out.resolve()]

    def test_every_output_dir_the_user_ever_used_is_remembered(self, tmp_path):
        """The whole point: after the user switches output directory, the OLD
        one must still be reachable — nothing else on disk will ever point at
        it again once its paused jobs are gone."""
        from utils.paths import known_output_roots

        old = tmp_path / "old"
        new = tmp_path / "new"
        old.mkdir()
        new.mkdir()
        make_batch_workspace(str(old))
        make_batch_workspace(str(new))

        roots = known_output_roots()
        assert old.resolve() in roots
        assert new.resolve() in roots

    def test_app_data_fallback_does_not_record_an_output_root(self, tmp_path, monkeypatch):
        """The fallback container lives at a fixed app-data location, so
        there is no per-user root worth recording — and recording the output
        dir there would claim ownership of a container we never created
        under it."""
        from utils import paths as paths_mod

        app_data = tmp_path.parent / f"appdata-roots-{tmp_path.name}"
        app_data.mkdir()
        monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data)

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        (output_dir / ".bananaflow_tmp").write_bytes(b"not a directory")

        workspace = make_batch_workspace(str(output_dir))

        assert app_data.resolve() in workspace.resolve().parents
        assert output_dir.resolve() not in paths_mod.known_output_roots()

    def test_corrupt_record_file_reads_back_as_empty_not_a_crash(self, tmp_path, monkeypatch):
        from utils import paths as paths_mod

        app_data = tmp_path / "appdata"
        app_data.mkdir()
        monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data)
        (app_data / "known_output_roots.json").write_text("{not json", encoding="utf-8")

        assert paths_mod.known_output_roots() == []

    def test_record_is_bounded(self, tmp_path, monkeypatch):
        from utils import paths as paths_mod

        app_data = tmp_path / "appdata"
        app_data.mkdir()
        monkeypatch.setattr(paths_mod, "get_app_data_dir", lambda: app_data)

        for i in range(paths_mod._MAX_KNOWN_OUTPUT_ROOTS + 10):
            paths_mod.register_output_root(tmp_path / f"root{i}")

        roots = paths_mod.known_output_roots()
        assert len(roots) == paths_mod._MAX_KNOWN_OUTPUT_ROOTS
        # Oldest entries drop out first; the most recent one is always kept.
        newest = tmp_path / f"root{paths_mod._MAX_KNOWN_OUTPUT_ROOTS + 9}"
        assert newest.resolve() in roots
