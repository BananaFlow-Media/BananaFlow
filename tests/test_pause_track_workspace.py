"""
tests/test_pause_track_workspace.py  –  pause_track carries workspace_dir forward
======================================================================================
Once DownloadOrchestrator.run_batch() routes downloads through a hidden
batch workspace (see core.downloader / utils.paths.make_batch_workspace),
a paused track's .part file lives inside that workspace, not directly in
output_dir. If DownloadController.pause_track()'s saved DownloadRequest
copy didn't carry workspace_dir forward, resume_track() would resubmit it
through a NEW run_batch() call that creates a brand-new, unrelated
workspace — the resumed download would have nothing to continue from,
silently starting over despite resumable=True.

Headless (QT_QPA_PLATFORM=offscreen); skipped when PySide6 is missing.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


class _FakeCard:
    def __init__(self, title: str = "Track", artist: str = "Artist") -> None:
        self.title = title
        self.artist = artist
        self.track_url = "https://www.youtube.com/watch?v=abc"
        self.status_calls: list[str] = []

    def set_status(self, status: str) -> None:
        self.status_calls.append(status)


class _FakeWorker:
    """Exposes just enough of DownloadWorker for pause_track()."""

    def __init__(self, jobs) -> None:
        self._jobs = jobs
        self.cancelled_keys: list[str] = []

    def cancel_track(self, key: str) -> None:
        self.cancelled_keys.append(key)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _controller(tmp_path, monkeypatch, app):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    from config import AppConfig
    from core.downloader import DownloadEngine
    from ui.controllers.download_controller import DownloadController

    return DownloadController(AppConfig(), DownloadEngine())


def test_pause_track_copies_workspace_dir(tmp_path, monkeypatch, app):
    from core.downloader import DownloadRequest, MediaType

    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))

    workspace = str(tmp_path / ".bananaflow_tmp" / "batch-live")
    live_req = DownloadRequest(
        url=card.track_url, output_dir=str(tmp_path), media_type=MediaType.AUDIO,
        workspace_dir=workspace,
    )
    ctrl._dl_worker = _FakeWorker(jobs=[(key, live_req)])

    ctrl.pause_track(card)

    saved = ctrl._paused_requests[key]
    assert saved.workspace_dir == workspace
    assert saved.resumable is True
    assert key in ctrl._dl_worker.cancelled_keys
    assert card.status_calls[-1] == "paused"


def test_pause_track_with_no_workspace_stays_none(tmp_path, monkeypatch, app):
    """A batch that fell back to direct-write (workspace creation failed,
    or an older-style request) must not fabricate a workspace_dir out of
    nowhere."""
    from core.downloader import DownloadRequest, MediaType

    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))

    live_req = DownloadRequest(
        url=card.track_url, output_dir=str(tmp_path), media_type=MediaType.AUDIO,
    )
    ctrl._dl_worker = _FakeWorker(jobs=[(key, live_req)])

    ctrl.pause_track(card)

    assert ctrl._paused_requests[key].workspace_dir is None


def test_pause_track_preserves_all_request_fields(tmp_path, monkeypatch, app):
    """Regression: the old hand-written pause copy silently dropped several
    fields (thumbnail_url, forced_album, platform, category,
    cookies_browser, is_solo, …), so a resumed track lost its custom
    artwork/album/crop behaviour. The snapshot must carry every field."""
    from core.downloader import DownloadRequest, MediaType
    from core.playlist_parser import SourcePlatform

    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))

    live_req = DownloadRequest(
        url=card.track_url, output_dir=str(tmp_path), media_type=MediaType.AUDIO,
        thumbnail_url="http://thumb", forced_album="Album", forced_artist="Artist",
        platform=SourcePlatform.YOUTUBE, category="stream:hls",
        cookies_browser="chrome", is_solo=True, square_thumbnails=True,
        expand_thumbnails=True, embed_lyrics=True, forced_duration=321,
        workspace_dir=str(tmp_path / ".bananaflow_tmp" / "b" / "k"),
    )
    ctrl._dl_worker = _FakeWorker(jobs=[(key, live_req)])

    ctrl.pause_track(card)
    saved = ctrl._paused_requests[key]

    assert saved.thumbnail_url == "http://thumb"
    assert saved.forced_album == "Album"
    assert saved.forced_artist == "Artist"
    assert saved.platform == SourcePlatform.YOUTUBE
    assert saved.category == "stream:hls"
    assert saved.cookies_browser == "chrome"
    assert saved.is_solo is True
    assert saved.square_thumbnails is True
    assert saved.expand_thumbnails is True
    assert saved.embed_lyrics is True
    assert saved.forced_duration == 321
    assert saved.workspace_dir == str(tmp_path / ".bananaflow_tmp" / "b" / "k")
    assert saved.resumable is True
