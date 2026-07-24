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


def test_raises_when_base_output_dir_cannot_hold_a_directory(tmp_path):
    """If base_output_dir contains a FILE where the workspace container
    would need to go, directory creation must fail loudly (OSError) so the
    caller can decide to fall back to writing directly into output_dir —
    never silently swallow the error."""
    blocker = tmp_path / ".bananaflow_tmp"
    blocker.write_bytes(b"not a directory")

    with pytest.raises(OSError):
        make_batch_workspace(str(tmp_path))
