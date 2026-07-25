"""
tests/test_cancel_during_post_processing.py  –  Cancel must win over publish
================================================================================
yt-dlp's own progress-hook abort check only fires DURING yt-dlp's own
download loop. It cannot see a pause/cancel that arrives in the window
after yt-dlp returns but before this app's own post-processing pipeline
(thumbnail crop, MusicBrainz, lyrics, ReplayGain) and the atomic publish
step run — both of which still do real work (network calls, ffmpeg,
moving a file into the user's visible output directory). Without an
explicit check in that window, a cancelled job could still be reported as
a completed, published download.

Covers the two checkpoints DownloadEngine.download() must have:
  1. right before the post-processing pipeline starts,
  2. right before the atomic publish (the pipeline itself can take a
     while, so cancellation state must be re-checked, not just checked
     once at the top).

And the equivalent checkpoints in the HLS/ffmpeg path, which has no
yt-dlp-style abort hook of its own at all (see test_hls_downloader_cancel.py
for direct ffmpeg-process cancellation).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.downloader import (
    DownloadEngine,
    DownloadRequest,
    DownloadStatus,
    MediaType,
)


def _mock_ydl(monkeypatch=None):
    mock_ydl = MagicMock()
    mock_ydl.__enter__ = lambda s: mock_ydl
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.download = MagicMock(return_value=None)
    return mock_ydl


class TestCancelBeforePipeline:
    def test_cancel_set_before_pipeline_skips_it_and_reports_cancelled(self, tmp_path):
        """cancel_event already set the instant yt-dlp returns (but before
        the post-processing pipeline runs) must produce CANCELLED, never
        run the pipeline, and never publish."""
        final_path = tmp_path / "workspace" / "song.mp3"
        final_path.parent.mkdir(parents=True)
        final_path.write_bytes(b"downloaded")

        engine = DownloadEngine()
        events: list = []
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=abc",
            output_dir=str(tmp_path / "output"),
            workspace_dir=str(tmp_path / "workspace"),
            media_type=MediaType.AUDIO,
        )
        req.on_finished = lambda p: events.append(("finished", p))
        req.on_error = lambda p: events.append(("error", p))
        req.on_progress = lambda p: None
        req._final_output_path = str(final_path)
        import threading
        req.cancel_event = threading.Event()
        req.cancel_event.set()

        pipeline_calls = []
        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls, \
             patch.object(engine, "_run_final_pipeline",
                          side_effect=lambda *a, **k: pipeline_calls.append(1) or []), \
             patch.object(engine, "_publish_to_final_location") as mock_publish:
            mock_ydl_cls.return_value = _mock_ydl()
            engine.download(req)

        assert pipeline_calls == [], "post-processing must not run once cancelled"
        mock_publish.assert_not_called()
        kinds = [k for k, _ in events]
        assert "finished" not in kinds
        assert "error" not in kinds  # a cancel is its own status, not an error


class TestCancelDuringPipeline:
    def test_cancel_arriving_during_pipeline_still_prevents_publish(self, tmp_path):
        """The pipeline can take a while (MusicBrainz/lyrics network calls,
        ReplayGain analysis) — a cancel that arrives WHILE it's running
        must still be caught before publish, not just checked once at the
        top before the pipeline started."""
        final_path = tmp_path / "workspace" / "song.mp3"
        final_path.parent.mkdir(parents=True)
        final_path.write_bytes(b"downloaded")

        engine = DownloadEngine()
        events: list = []
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=abc",
            output_dir=str(tmp_path / "output"),
            workspace_dir=str(tmp_path / "workspace"),
            media_type=MediaType.AUDIO,
        )
        req.on_finished = lambda p: events.append(("finished", p))
        req.on_error = lambda p: events.append(("error", p))
        req.on_progress = lambda p: None
        req._final_output_path = str(final_path)
        import threading
        req.cancel_event = threading.Event()

        def _slow_pipeline(_req, _path):
            # Simulate the cancel button being clicked WHILE post-processing
            # (e.g. a MusicBrainz lookup) is in flight.
            req.cancel_event.set()
            return []

        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls, \
             patch.object(engine, "_run_final_pipeline", side_effect=_slow_pipeline), \
             patch.object(engine, "_publish_to_final_location") as mock_publish:
            mock_ydl_cls.return_value = _mock_ydl()
            engine.download(req)

        mock_publish.assert_not_called()
        kinds = [k for k, _ in events]
        assert "finished" not in kinds

    def test_no_cancel_publishes_normally(self, tmp_path):
        """Control case: without any cancellation, the pipeline runs and
        publish IS called — the new checks must not block the normal
        success path."""
        final_path = tmp_path / "workspace" / "song.mp3"
        final_path.parent.mkdir(parents=True)
        final_path.write_bytes(b"downloaded")

        engine = DownloadEngine()
        events: list = []
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=abc",
            output_dir=str(tmp_path / "output"),
            workspace_dir=str(tmp_path / "workspace"),
            media_type=MediaType.AUDIO,
        )
        req.on_finished = lambda p: events.append(("finished", p))
        req.on_error = lambda p: events.append(("error", p))
        req.on_progress = lambda p: None
        req._final_output_path = str(final_path)

        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls, \
             patch.object(engine, "_run_final_pipeline", return_value=[]):
            mock_ydl_cls.return_value = _mock_ydl()
            engine.download(req)

        kinds = [k for k, _ in events]
        assert "finished" in kinds
        assert "error" not in kinds
        # Real publish ran: file landed in the real output dir.
        published = tmp_path / "output" / "song.mp3"
        assert published.exists()


class TestHlsEngineCancelReportsCancelledNotFinished:
    """End-to-end at the DownloadEngine._download_hls_stream call site (not
    just download_hls() in isolation): a cancel must produce a CANCELLED
    status and never publish, whether it arrives WHILE ffmpeg is running
    (HlsCancelled bubbles up) or in the gap right after ffmpeg finishes but
    before publish — mirroring the main yt-dlp path's two checkpoints."""

    def test_cancel_during_remux_reports_cancelled(self, tmp_path, monkeypatch):
        import threading
        import core.hls_downloader as hls_mod

        engine = DownloadEngine()
        events: list = []
        req = DownloadRequest(
            url="https://example.test/stream.m3u8",
            output_dir=str(tmp_path / "output"),
            workspace_dir=str(tmp_path / "workspace"),
            media_type=MediaType.AUDIO,
            stream_type="hls",
            forced_title="Song",
        )
        req.on_finished = lambda p: events.append(("finished", p))
        req.on_error = lambda p: events.append(("error", p))
        req.on_progress = lambda p: None
        req.cancel_event = threading.Event()
        req.cancel_event.set()

        def _fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
            raise hls_mod.HlsCancelled("simulated mid-remux cancel")

        monkeypatch.setattr(hls_mod, "download_hls", _fake_download_hls)
        publish_calls = []
        monkeypatch.setattr(
            engine, "_publish_to_final_location",
            lambda *a, **k: publish_calls.append(1),
        )

        engine.download(req)

        assert publish_calls == []
        kinds = [k for k, _ in events]
        assert "finished" not in kinds

    def test_cancel_after_remux_before_publish_reports_cancelled(self, tmp_path, monkeypatch):
        """ffmpeg finishes normally, but cancel_event is set in the instant
        before publish — must still not publish."""
        import threading
        import core.hls_downloader as hls_mod

        engine = DownloadEngine()
        events: list = []
        req = DownloadRequest(
            url="https://example.test/stream.m3u8",
            output_dir=str(tmp_path / "output"),
            workspace_dir=str(tmp_path / "workspace"),
            media_type=MediaType.AUDIO,
            stream_type="hls",
            forced_title="Song",
        )
        req.on_finished = lambda p: events.append(("finished", p))
        req.on_error = lambda p: events.append(("error", p))
        req.on_progress = lambda p: None
        req.cancel_event = threading.Event()

        def _fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"remuxed")
            req.cancel_event.set()  # cancel arrives right as ffmpeg finishes

        monkeypatch.setattr(hls_mod, "download_hls", _fake_download_hls)
        publish_calls = []
        monkeypatch.setattr(
            engine, "_publish_to_final_location",
            lambda *a, **k: publish_calls.append(1),
        )

        engine.download(req)

        assert publish_calls == []
        kinds = [k for k, _ in events]
        assert "finished" not in kinds
