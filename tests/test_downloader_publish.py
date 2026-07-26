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

import os
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


def test_path_outside_workspace_is_rejected_not_accepted(tmp_path):
    """A file that isn't actually under the declared workspace must NOT be
    accepted as a successfully published result — publishing an arbitrary
    path, or silently returning it as 'done', would both be wrong. It must
    raise, and must never move or lose the out-of-workspace file."""
    from core.downloader import PublishError

    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    other = tmp_path / "elsewhere"
    workspace.mkdir()
    other.mkdir()

    src = other / "song.mp3"
    src.write_bytes(b"x")
    req = _req(output_dir=str(tmp_path / "output"), workspace_dir=str(workspace))

    with pytest.raises(PublishError):
        engine._publish_to_final_location(req, str(src))

    assert src.exists()  # the out-of-workspace file is never touched


def test_publish_failure_raises_and_leaves_existing_destination_intact(tmp_path, monkeypatch):
    """When the atomic move itself fails, publish must raise (so the
    download is reported as an error, never a false success) AND leave any
    existing destination file untouched."""
    from core.downloader import PublishError

    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()

    dest = output_dir / "song.mp3"
    dest.write_bytes(b"EXISTING-CONTENT")

    src = workspace / "song.mp3"
    src.write_bytes(b"NEW-CONTENT")
    req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))

    # Force the atomic placement to fail with a non-recoverable error.
    def _boom(_src, _dest, **_kwargs):
        raise OSError("simulated publish failure")

    monkeypatch.setattr(engine, "_atomic_place", _boom)

    with pytest.raises(PublishError):
        engine._publish_to_final_location(req, str(src))

    assert dest.read_bytes() == b"EXISTING-CONTENT"  # untouched


def test_cross_volume_publish_copies_then_atomically_renames(tmp_path, monkeypatch):
    """When the workspace is on a different volume (os.replace raises EXDEV),
    publish must copy to a temp adjacent to dest and os.replace THAT into
    place — never leave a visible half-copy, and land the full content."""
    import errno
    import core.downloader as dl_mod

    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()

    src = workspace / "song.mp3"
    src.write_bytes(b"CROSS-VOLUME-CONTENT")
    req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))

    real_replace = os.replace

    def _fake_replace(a, b):
        # The direct workspace->dest move looks cross-device; the temp->dest
        # rename (temp lives next to dest) is same-volume and goes through.
        if str(a) == str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(dl_mod.os, "replace", _fake_replace)

    result = engine._publish_to_final_location(req, str(src))

    dest = output_dir / "song.mp3"
    assert Path(result) == dest
    assert dest.read_bytes() == b"CROSS-VOLUME-CONTENT"
    assert not src.exists()  # source removed after successful cross-volume publish
    # No leftover publish temp file in the output directory.
    leftovers = [p.name for p in output_dir.iterdir() if "publish-tmp" in p.name]
    assert leftovers == []


def test_concurrent_cross_volume_publishes_to_same_destination_do_not_collide(tmp_path, monkeypatch):
    """Two THREADS publishing to the identical destination filename at the
    same time must never corrupt the temp-file mechanics — each gets its
    own securely unique temp name (tempfile.mkstemp), so there is no
    shared-name race. Uses the real _publish_to_final_location call site
    (not the raw _atomic_place), so its existing lock-error retry absorbs
    the genuine, expected OS-level contention of two renames landing on the
    identical destination path at the same instant — that transient race is
    a Windows filesystem fact of life, independent of this code."""
    import errno
    import threading
    import core.downloader as dl_mod

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    dest_name = "song.mp3"

    real_replace = os.replace

    def _force_cross_volume(a, b):
        # Only the temp->dest rename (temp name carries the publish-tmp
        # marker) is allowed through; the direct src->dest attempt always
        # looks cross-device, forcing the copy-then-rename branch.
        if str(a).endswith(DownloadEngine.PUBLISH_TMP_SUFFIX):
            return real_replace(a, b)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(dl_mod.os, "replace", _force_cross_volume)
    monkeypatch.setattr(dl_mod.time, "sleep", lambda _s: None)  # no real 2s waits in the test

    engine = DownloadEngine()
    errors = []

    def _publish(content: bytes) -> None:
        try:
            workspace = tmp_path / f"workspace_{content!r}"
            workspace.mkdir()
            src = workspace / dest_name
            src.write_bytes(content)
            req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))
            engine._publish_to_final_location(req, str(src))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_publish, args=(c,)) for c in (b"ONE", b"TWO")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)

    assert errors == [], f"concurrent publish to the same destination raised: {errors}"
    assert (output_dir / dest_name).read_bytes() in (b"ONE", b"TWO")


def test_hls_publish_failure_reports_error_not_finished(tmp_path, monkeypatch):
    """End-to-end at a real call site (the HLS path): if publish raises, the
    engine must fire ERROR, never FINISHED — a failed publish is never a
    completed download."""
    from core.downloader import DownloadProgress, DownloadStatus, MediaType, PublishError

    engine = DownloadEngine()
    workspace = tmp_path / "workspace"
    output_dir = tmp_path / "output"
    workspace.mkdir()
    output_dir.mkdir()

    events: list = []
    req = DownloadRequest(
        url="https://example.test/stream.m3u8",
        output_dir=str(output_dir),
        workspace_dir=str(workspace),
        media_type=MediaType.AUDIO,
        stream_type="hls",
        forced_title="Song",
    )
    req.on_finished = lambda p: events.append(("finished", p))
    req.on_error = lambda p: events.append(("error", p))
    req.on_progress = lambda p: None

    # Fake the actual stream download so no ffmpeg/network runs; it just
    # "creates" the output file the publish step would then move.
    import core.hls_downloader as hls_mod

    def _fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
        Path(output_path).write_bytes(b"stream-bytes")

    monkeypatch.setattr(hls_mod, "download_hls", _fake_download_hls)

    def _boom(_req, _path):
        raise PublishError("simulated publish failure")

    monkeypatch.setattr(engine, "_publish_to_final_location", _boom)

    engine.download(req)

    kinds = [k for k, _ in events]
    assert "error" in kinds
    assert "finished" not in kinds
    # Confirms the error came from the intended publish failure, not some
    # unrelated exception (e.g. an incompatible fake signature) that would
    # also produce an "error" event and make this assertion vacuous.
    error_progress = next(p for k, p in events if k == "error")
    assert "simulated publish failure" in error_progress.error_message


# ── Cancellation during publication ──────────────────────────────────────────
# Publication is the LAST thing a job does and — across volumes — it is not
# instantaneous. A cancel/pause landing inside it must abandon the publish,
# not be overtaken by a file appearing in the user's output folder and a
# reported success.

class TestCancelDuringPublish:
    def _setup(self, tmp_path):
        workspace = tmp_path / "workspace"
        output_dir = tmp_path / "output"
        workspace.mkdir()
        output_dir.mkdir()
        src = workspace / "song.mp3"
        src.write_bytes(b"CONTENT" * 4096)
        req = _req(output_dir=str(output_dir), workspace_dir=str(workspace))
        return workspace, output_dir, src, req

    def test_per_request_cancel_before_publish_aborts_without_touching_dest(self, tmp_path):
        import threading

        from core.downloader import PublishCancelled

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)
        req.cancel_event = threading.Event()
        req.cancel_event.set()

        with pytest.raises(PublishCancelled):
            engine._publish_to_final_location(req, str(src))

        assert not (output_dir / "song.mp3").exists()
        assert src.exists(), "the workspace file survives so a resume can use it"

    def test_engine_wide_cancel_before_publish_aborts(self, tmp_path):
        from core.downloader import PublishCancelled

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)
        engine.cancel_all()

        with pytest.raises(PublishCancelled):
            engine._publish_to_final_location(req, str(src))

        assert not (output_dir / "song.mp3").exists()

    def test_cancel_arriving_during_cross_volume_copy_is_honoured(self, tmp_path, monkeypatch):
        """The window the pre-publish check cannot cover: a multi-gigabyte
        cross-volume copy. Cancelling half-way must abandon the copy, remove
        the staging temp, and leave the destination untouched."""
        import errno
        import threading

        import core.downloader as dl_mod
        from core.downloader import PublishCancelled

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)
        # Big enough to need several chunks.
        src.write_bytes(b"x" * (DownloadEngine._PUBLISH_COPY_CHUNK * 4))
        cancel = threading.Event()
        req.cancel_event = cancel

        real_replace = os.replace

        def _force_cross_volume(a, b):
            if str(a) == str(src):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(a, b)

        monkeypatch.setattr(dl_mod.os, "replace", _force_cross_volume)

        # Trip the cancel once the copy is genuinely under way.
        chunks = {"n": 0}
        real_fdopen = dl_mod.os.fdopen

        class _CountingWriter:
            def __init__(self, fh):
                self._fh = fh

            def write(self, data):
                chunks["n"] += 1
                if chunks["n"] == 2:
                    cancel.set()
                return self._fh.write(data)

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

        monkeypatch.setattr(
            dl_mod.os, "fdopen", lambda fd, mode: _CountingWriter(real_fdopen(fd, mode)),
        )

        with pytest.raises(PublishCancelled):
            engine._publish_to_final_location(req, str(src))

        assert not (output_dir / "song.mp3").exists()
        assert src.exists()
        leftovers = [
            p.name for p in output_dir.iterdir()
            if DownloadEngine.PUBLISH_TMP_SUFFIX in p.name
        ]
        assert leftovers == [], "the abandoned staging temp must be removed"

    def test_cross_volume_staging_temp_is_hidden_before_any_content_is_written(
        self, tmp_path, monkeypatch,
    ):
        """The destination-side partial must never be visible. The temp has to
        carry the Hidden attribute from creation — hiding it only after the
        copy leaves a growing, visible file in the user's output folder for
        the whole duration of the copy."""
        import errno

        import core.downloader as dl_mod
        from utils import paths as paths_mod

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)

        real_replace = os.replace

        def _force_cross_volume(a, b):
            if str(a) == str(src):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(a, b)

        monkeypatch.setattr(dl_mod.os, "replace", _force_cross_volume)

        events: list[tuple[str, str, bool]] = []
        real_set_hidden = paths_mod._set_hidden_attribute

        def _tracking(path, **kwargs):
            hidden = kwargs.get("hidden", True)
            size = 0
            try:
                size = os.path.getsize(str(path))
            except OSError:
                pass
            events.append(("hide" if hidden else "unhide", str(path), size > 0))
            return real_set_hidden(path, **kwargs)

        monkeypatch.setattr(dl_mod, "_set_hidden_attribute", _tracking)

        result = engine._publish_to_final_location(req, str(src))

        assert Path(result) == output_dir / "song.mp3"
        assert (output_dir / "song.mp3").exists()
        hides = [e for e in events if e[0] == "hide"]
        assert hides, "the staging temp must be hidden"
        assert all(not had_content for _, _, had_content in hides), (
            "the temp was hidden only after content was written — a visible "
            "partial file existed in the output directory during the copy"
        )
        # And unhidden again before the rename, so the published file is not
        # itself hidden from the user.
        assert [e[0] for e in events][-1] == "unhide"

    def test_published_file_is_not_hidden_after_a_cross_volume_publish(
        self, tmp_path, monkeypatch,
    ):
        """os.replace carries the source's attributes across — a staging temp
        that stayed hidden would publish the user's finished track as a
        hidden file they cannot find."""
        import errno

        import core.downloader as dl_mod

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)

        real_replace = os.replace

        def _force_cross_volume(a, b):
            if str(a) == str(src):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(a, b)

        monkeypatch.setattr(dl_mod.os, "replace", _force_cross_volume)

        engine._publish_to_final_location(req, str(src))

        dest = output_dir / "song.mp3"
        assert dest.exists()
        if os.name == "nt":
            import ctypes

            FILE_ATTRIBUTE_HIDDEN = 0x02
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(dest))
            assert not (attrs & FILE_ATTRIBUTE_HIDDEN), (
                "the published file must be visible to the user"
            )

    def test_publish_is_refused_when_the_staging_temp_cannot_be_hidden(
        self, tmp_path, monkeypatch,
    ):
        """Hiding the staging temp is not cosmetic: if it fails, the copy
        would write a visible, growing partial straight into the user's
        output folder — the exact thing the staging step exists to prevent.
        Refuse instead, leaving the finished file safely in the workspace."""
        import errno

        import core.downloader as dl_mod
        from core.downloader import PublishError

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)

        real_replace = os.replace

        def _force_cross_volume(a, b):
            if str(a) == str(src):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(a, b)

        monkeypatch.setattr(dl_mod.os, "replace", _force_cross_volume)
        monkeypatch.setattr(dl_mod, "_set_hidden_attribute", lambda p, **kw: False)

        with pytest.raises(PublishError):
            engine._publish_to_final_location(req, str(src))

        assert not (output_dir / "song.mp3").exists()
        assert src.exists(), "the finished file stays in the workspace"
        leftovers = [
            p.name for p in output_dir.iterdir()
            if DownloadEngine.PUBLISH_TMP_SUFFIX in p.name
        ]
        assert leftovers == [], "the un-hideable staging file must not be left behind"

    def test_publish_is_refused_when_the_temp_cannot_be_unhidden(
        self, tmp_path, monkeypatch,
    ):
        """os.replace carries the source's attributes across, so renaming a
        still-hidden temp publishes the user's finished track as a file they
        cannot find. Refuse the rename instead."""
        import errno

        import core.downloader as dl_mod
        from core.downloader import PublishError
        from utils import paths as paths_mod

        engine = DownloadEngine()
        _ws, output_dir, src, req = self._setup(tmp_path)

        real_replace = os.replace

        def _force_cross_volume(a, b):
            if str(a) == str(src):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(a, b)

        monkeypatch.setattr(dl_mod.os, "replace", _force_cross_volume)

        real_set_hidden = paths_mod._set_hidden_attribute

        def _hide_ok_unhide_fails(path, **kwargs):
            if kwargs.get("hidden", True) is False:
                return False
            return real_set_hidden(path, **kwargs)

        monkeypatch.setattr(dl_mod, "_set_hidden_attribute", _hide_ok_unhide_fails)

        with pytest.raises(PublishError):
            engine._publish_to_final_location(req, str(src))

        assert not (output_dir / "song.mp3").exists(), (
            "a file the user cannot see is not a published file"
        )
        assert src.exists()
        leftovers = [
            p.name for p in output_dir.iterdir()
            if DownloadEngine.PUBLISH_TMP_SUFFIX in p.name
        ]
        assert leftovers == []

    def test_hls_path_reports_cancelled_not_error_when_publish_is_cancelled(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end at a real call site: a cancel during publish is a
        cancellation, never an error and never a success."""
        import threading

        from core.downloader import DownloadStatus

        engine = DownloadEngine()
        workspace = tmp_path / "workspace"
        output_dir = tmp_path / "output"
        workspace.mkdir()
        output_dir.mkdir()

        events: list = []
        cancel = threading.Event()
        req = DownloadRequest(
            url="https://example.test/stream.m3u8",
            output_dir=str(output_dir),
            workspace_dir=str(workspace),
            media_type=MediaType.AUDIO,
            stream_type="hls",
            forced_title="Song",
            cancel_event=cancel,
        )
        req.on_finished = lambda p: events.append(("finished", p))
        req.on_error = lambda p: events.append(("error", p))
        req.on_progress = lambda p: events.append(("progress", p))

        import core.hls_downloader as hls_mod

        def _fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
            Path(output_path).write_bytes(b"stream-bytes")
            # The cancel lands after ffmpeg finished but before publish — the
            # exact window the pre-publish check was added for; here it is
            # tripped one step later still, inside the publish itself.

        monkeypatch.setattr(hls_mod, "download_hls", _fake_download_hls)

        real_publish = engine._publish_to_final_location

        def _cancel_then_publish(r, path):
            cancel.set()
            return real_publish(r, path)

        monkeypatch.setattr(engine, "_publish_to_final_location", _cancel_then_publish)

        engine.download(req)

        assert "finished" not in [k for k, _ in events]
        assert "error" not in [k for k, _ in events]
        statuses = [p.status for k, p in events if k == "progress"]
        assert DownloadStatus.CANCELLED in statuses
        assert not (output_dir / "Song.mp3").exists()


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
