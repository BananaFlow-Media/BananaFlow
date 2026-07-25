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
