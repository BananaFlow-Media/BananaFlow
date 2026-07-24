"""
tests/test_downloader_publish.py  –  Atomic publish (workspace -> output_dir)
=================================================================================
DownloadEngine._publish_to_final_location moves a fully-ready file out of
the hidden batch workspace into the user's real output directory, only
once every post-processing step has already finished — the user must never
see a half-built file. Covers:

  * no-op when request.workspace_dir isn't set (old direct-write callers
    are unaffected — tests, CLI).
  * the file actually moves, preserving its relative subpath (including a
    playlist_name subfolder) under output_dir.
  * the destination directory is created if it doesn't exist yet.
  * publishing overwrites an existing destination file (the "overwrite"
    duplicate policy needs this — a stale duplicate must not block the
    fresh download from landing).
  * a path outside the workspace root degrades safely (returns the
    original path, logs, never raises / never loses the file).

Also covers the other half of "never write visibly inside the user's
output directory": DownloadEngine._build_ydl_opts must root yt-dlp's
outtmpl at workspace_dir when one is set, not output_dir.

Pure stdlib file I/O — no yt-dlp, no network, no Qt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.downloader import DownloadEngine, DownloadRequest, MediaType


def _req(output_dir: str, workspace_dir: str = None) -> DownloadRequest:
    return DownloadRequest(
        url="https://example.test/x", output_dir=output_dir, workspace_dir=workspace_dir,
    )


def test_noop_when_no_workspace_dir(tmp_path):
    engine = DownloadEngine()
    src = tmp_path / "song.mp3"
    src.write_bytes(b"audio")
    req = _req(output_dir=str(tmp_path))

    result = engine._publish_to_final_location(req, str(src))

    assert result == str(src)
    assert src.exists()  # never touched


def test_moves_file_from_workspace_to_output_dir(tmp_path):
    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()

    src = workspace / "song.mp3"
    src.write_bytes(b"audio-bytes")
    req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))

    result = engine._publish_to_final_location(req, str(src))

    dest = output_dir / "song.mp3"
    assert Path(result) == dest
    assert dest.exists()
    assert dest.read_bytes() == b"audio-bytes"
    assert not src.exists()  # moved, not copied


def test_preserves_relative_subfolder(tmp_path):
    """A playlist_name subfolder created inside the workspace must land in
    the SAME relative subfolder under output_dir, not flattened."""
    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    sub = workspace / "My Artist" / "My Album"
    sub.mkdir(parents=True)
    output_dir.mkdir()

    src = sub / "01 - Track.mp3"
    src.write_bytes(b"x")
    req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))

    result = engine._publish_to_final_location(req, str(src))

    dest = output_dir / "My Artist" / "My Album" / "01 - Track.mp3"
    assert Path(result) == dest
    assert dest.exists()


def test_creates_missing_destination_parent_dirs(tmp_path):
    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"  # deliberately not created yet
    (workspace / "sub").mkdir(parents=True)

    src = workspace / "sub" / "song.mp3"
    src.write_bytes(b"x")
    req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))

    result = engine._publish_to_final_location(req, str(src))

    assert Path(result).exists()
    assert Path(result).parent.is_dir()


def test_overwrites_existing_destination_file(tmp_path):
    """The "overwrite" duplicate policy re-downloads and replaces — publish
    must not refuse or error just because the old file is still there."""
    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()

    dest = output_dir / "song.mp3"
    dest.write_bytes(b"OLD-STALE-CONTENT")

    src = workspace / "song.mp3"
    src.write_bytes(b"NEW-CONTENT")
    req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))

    result = engine._publish_to_final_location(req, str(src))

    assert Path(result).read_bytes() == b"NEW-CONTENT"


def test_path_outside_workspace_degrades_safely(tmp_path, caplog):
    """If the given path isn't actually under workspace_dir (shouldn't
    happen given how _build_ydl_opts constructs it, but must never lose
    the file if it does), return the original path unchanged rather than
    raising or silently discarding it."""
    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    other = tmp_path / "elsewhere"
    workspace.mkdir()
    other.mkdir()

    src = other / "song.mp3"
    src.write_bytes(b"x")
    req = _req(output_dir=str(tmp_path / "output"), workspace_dir=str(workspace))

    result = engine._publish_to_final_location(req, str(src))

    assert result == str(src)
    assert src.exists()  # file is never lost


# ── outtmpl routing (the write side of the same guarantee) ──────────────────

class TestBuildYdlOptsWorkspaceRouting:
    def test_outtmpl_rooted_at_workspace_when_set(self, tmp_path):
        workspace = tmp_path / "workspace"
        output_dir = tmp_path / "output"
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=x",
            output_dir=str(output_dir),
            workspace_dir=str(workspace),
            media_type=MediaType.AUDIO,
        )

        opts = DownloadEngine()._build_ydl_opts(req)  # noqa: SLF001

        assert opts["outtmpl"].startswith(str(workspace.resolve()))
        assert str(output_dir.resolve()) not in opts["outtmpl"]

    def test_outtmpl_rooted_at_output_dir_when_no_workspace(self, tmp_path):
        """Old direct-write behavior for callers that don't opt into a
        workspace (tests, CLI) must be unchanged."""
        output_dir = tmp_path / "output"
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=x",
            output_dir=str(output_dir),
            media_type=MediaType.AUDIO,
        )

        opts = DownloadEngine()._build_ydl_opts(req)  # noqa: SLF001

        assert opts["outtmpl"].startswith(str(output_dir.resolve()))

    def test_playlist_subfolder_nested_under_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        output_dir = tmp_path / "output"
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=x",
            output_dir=str(output_dir),
            workspace_dir=str(workspace),
            media_type=MediaType.AUDIO,
            playlist_name="My Album",
        )

        opts = DownloadEngine()._build_ydl_opts(req)  # noqa: SLF001

        expected_prefix = str((workspace.resolve() / "My Album"))
        assert opts["outtmpl"].startswith(expected_prefix)
        assert (workspace / "My Album").is_dir()
        assert not (output_dir / "My Album").exists(), (
            "the playlist subfolder must not appear in the user's output "
            "directory until publish — only in the workspace"
        )
