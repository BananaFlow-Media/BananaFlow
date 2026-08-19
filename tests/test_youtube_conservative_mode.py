"""
tests/test_youtube_conservative_mode.py  –  Reliability-hardening phase 2
============================================================================
YouTube-only conservative reliability mode: single-fragment concurrency
per job (options builder) and serialized-with-cooldown scheduling across
jobs in a batch (orchestrator). Non-YouTube jobs (Spotify-matched tracks
resolve to a YouTube URL before download and so *are* covered; generic
sites are not) must be unaffected either way.
"""

from __future__ import annotations

import logging
import threading
import time
from unittest.mock import patch

import pytest

from core.downloader import DownloadEngine, DownloadRequest, MediaType


# ──────────────────────────────────────────────────────────────────────────────
# 1. Options builder
# ──────────────────────────────────────────────────────────────────────────────

class TestConservativeOptionsBuilder:

    def _opts(self, tmp_path, url, mode="conservative"):
        req = DownloadRequest(
            url=url,
            output_dir=str(tmp_path),
            media_type=MediaType.AUDIO,
            youtube_reliability_mode=mode,
        )
        return DownloadEngine()._build_ydl_opts(req)  # noqa: SLF001

    def test_youtube_url_conservative_forces_single_fragment(self, tmp_path):
        opts = self._opts(tmp_path, "https://www.youtube.com/watch?v=TESTVIDEOAAA")
        assert opts["concurrent_fragment_downloads"] == 1

    def test_youtube_url_fast_mode_keeps_default_fragment_concurrency(self, tmp_path):
        opts = self._opts(tmp_path, "https://www.youtube.com/watch?v=TESTVIDEOAAA", mode="fast")
        assert opts["concurrent_fragment_downloads"] == 5  # existing default, untouched

    def test_youtu_be_url_conservative_forces_single_fragment(self, tmp_path):
        opts = self._opts(tmp_path, "https://youtu.be/TESTVIDEOAAA")
        assert opts["concurrent_fragment_downloads"] == 1

    def test_non_youtube_url_unaffected_by_conservative_default(self, tmp_path):
        opts = self._opts(tmp_path, "https://example.com/some-video")
        assert opts["concurrent_fragment_downloads"] == 5  # existing default, untouched

    def test_youtube_player_client_uses_web_and_web_embedded(self, tmp_path):
        for media_type in (MediaType.AUDIO, MediaType.VIDEO):
            req = DownloadRequest(
                url="https://www.youtube.com/watch?v=TESTVIDEOAAA",
                output_dir=str(tmp_path),
                media_type=media_type,
            )
            opts = DownloadEngine()._build_ydl_opts(req)  # noqa: SLF001
            assert opts.get("extractor_args", {}).get("youtube", {}).get("player_client") == ["web", "web_embedded"]


# ──────────────────────────────────────────────────────────────────────────────
# 2. Scheduler (DownloadOrchestrator) — parallelism + cooldown
# ──────────────────────────────────────────────────────────────────────────────

class ConcurrencyTrackingEngine:
    """Fake DownloadEngine that records how many downloads were in flight
    at once, split by whether the URL is a YouTube URL, so a test can
    assert "YouTube jobs never overlap" while "non-YouTube jobs do"."""

    def __init__(self, work_time: float = 0.05) -> None:
        from core.youtube_reliability import is_youtube_url
        self._is_youtube_url = is_youtube_url
        self._cancel_event = threading.Event()
        self._work_time = work_time
        self._lock = threading.Lock()
        self._active_youtube = 0
        self._active_other = 0
        self.max_active_youtube = 0
        self.max_active_other = 0

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        is_yt = self._is_youtube_url(req.url)
        with self._lock:
            if is_yt:
                self._active_youtube += 1
                self.max_active_youtube = max(self.max_active_youtube, self._active_youtube)
            else:
                self._active_other += 1
                self.max_active_other = max(self.max_active_other, self._active_other)
        time.sleep(self._work_time)
        with self._lock:
            if is_yt:
                self._active_youtube -= 1
            else:
                self._active_other -= 1
        if req.on_finished:
            from core.downloader import DownloadProgress, DownloadStatus
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED, url=req.url, fraction=1.0,
                output_path="/tmp/out.mp3",
            ))


class _NullCallbacks:
    def on_track_progress(self, key, fraction): pass
    def on_track_speed(self, key, speed_bps, eta_seconds): pass
    def on_track_status(self, key, status): pass
    def on_track_finished(self, key, output_path): pass
    def on_track_error(self, key, error): pass
    def on_overall_progress(self, fraction): pass
    def on_metrics(self, speed, eta): pass
    def on_status_message(self, msg): pass
    def on_job_count_changed(self, completed, total): pass
    def on_batch_finished(self): pass
    def on_track_thumbnail(self, key, thumbnail_url): pass


def _job(key, url, mode="conservative"):
    return (key, DownloadRequest(
        url=url, output_dir="/tmp", media_type=MediaType.AUDIO,
        forced_title=key, youtube_reliability_mode=mode,
    ))


class TestOrchestratorYoutubeSerialization:

    def test_multiple_youtube_jobs_do_not_overlap(self):
        from core.download_orchestrator import DownloadOrchestrator

        engine = ConcurrencyTrackingEngine(work_time=0.05)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=4)

        jobs = [
            _job("yt1", "https://www.youtube.com/watch?v=AAAAAAAAAAA"),
            _job("yt2", "https://www.youtube.com/watch?v=BBBBBBBBBBB"),
            _job("yt3", "https://youtu.be/CCCCCCCCCCC"),
        ]
        # Keep the cooldown tiny so the test runs fast; the real 5-10s
        # default is verified separately in TestConservativeDelayRange.
        with patch("core.download_orchestrator.CONSERVATIVE_DELAY_RANGE", (0.01, 0.02)):
            result = orch.run_batch(jobs)

        assert result.completed == 3
        assert engine.max_active_youtube == 1

    def test_non_youtube_jobs_not_blocked_by_youtube_jobs(self):
        from core.download_orchestrator import DownloadOrchestrator

        engine = ConcurrencyTrackingEngine(work_time=0.05)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=4)

        jobs = [
            _job("yt1", "https://www.youtube.com/watch?v=AAAAAAAAAAA"),
            _job("yt2", "https://www.youtube.com/watch?v=BBBBBBBBBBB"),
            _job("gen1", "https://example.com/video-1"),
            _job("gen2", "https://example.com/video-2"),
        ]
        with patch("core.download_orchestrator.CONSERVATIVE_DELAY_RANGE", (0.01, 0.02)):
            result = orch.run_batch(jobs)

        assert result.completed == 4
        assert engine.max_active_youtube == 1
        # The two generic jobs should have been free to run at the same time
        # as each other (and as a YouTube job), unlike the YouTube jobs.
        assert engine.max_active_other == 2

    def test_single_youtube_job_is_not_serialized(self):
        """A lone YouTube job has no sibling to protect — it must not pay
        the cooldown delay (this keeps single-track downloads snappy)."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = ConcurrencyTrackingEngine(work_time=0.01)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=2)

        jobs = [_job("yt1", "https://www.youtube.com/watch?v=AAAAAAAAAAA")]

        # Deliberately do NOT patch CONSERVATIVE_DELAY_RANGE: if the single
        # job were (incorrectly) serialized, this test would take 5-10s.
        start = time.time()
        result = orch.run_batch(jobs)
        elapsed = time.time() - start

        assert result.completed == 1
        assert elapsed < 2.0

    def test_fast_mode_allows_youtube_jobs_to_overlap(self):
        """youtube_reliability_mode='fast' is opt-in and skips serialization."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = ConcurrencyTrackingEngine(work_time=0.1)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=4)

        jobs = [
            _job("yt1", "https://www.youtube.com/watch?v=AAAAAAAAAAA", mode="fast"),
            _job("yt2", "https://www.youtube.com/watch?v=BBBBBBBBBBB", mode="fast"),
            _job("yt3", "https://youtu.be/CCCCCCCCCCC", mode="fast"),
        ]
        result = orch.run_batch(jobs)

        assert result.completed == 3
        assert engine.max_active_youtube > 1


class TestConservativeDelayRangeValue:
    """Pin the documented default so a future edit can't silently drift
    from the "5-10 seconds" reliability requirement."""

    def test_default_delay_range_is_5_to_10_seconds(self):
        from core.youtube_reliability import CONSERVATIVE_DELAY_RANGE
        assert CONSERVATIVE_DELAY_RANGE == (5.0, 10.0)

    def test_default_max_parallel_youtube_is_1(self):
        from core.youtube_reliability import CONSERVATIVE_MAX_PARALLEL_YOUTUBE
        assert CONSERVATIVE_MAX_PARALLEL_YOUTUBE == 1


# ──────────────────────────────────────────────────────────────────────────────
# 3. Logging accuracy (phase 2.1 cleanup) — each component may only log the
# behavior it actually controls/engages, never a behavior owned by another
# layer or one that didn't actually happen for this job.
# ──────────────────────────────────────────────────────────────────────────────

class TestConservativeLoggingAccuracy:

    def test_builder_log_only_mentions_fragment_concurrency(self, tmp_path, caplog):
        """_build_ydl_opts() only controls per-job fragment concurrency —
        it must not also claim parallel=/delay= behavior, which is decided
        by the orchestrator and may not happen at all for this request."""
        caplog.set_level(logging.INFO, logger="core.downloader")
        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=TESTVIDEOAAA",
            output_dir=str(tmp_path),
            media_type=MediaType.AUDIO,
        )
        DownloadEngine()._build_ydl_opts(req)  # noqa: SLF001

        lines = [r.getMessage() for r in caplog.records if "youtube_conservative" in r.getMessage()]
        assert lines, "expected a youtube_conservative log line from the options builder"
        assert "fragment_concurrency=1" in lines[0]
        assert "delay=" not in lines[0]
        assert "parallel=" not in lines[0]

    def test_single_youtube_job_batch_logs_no_serialization(self, caplog):
        """A lone YouTube job is never gated (see run_batch's
        youtube_job_count > 1 check) — the orchestrator must not log a
        "serializing"/cooldown line implying a delay that never happens."""
        from core.download_orchestrator import DownloadOrchestrator

        caplog.set_level(logging.INFO, logger="core.download_orchestrator")
        engine = ConcurrencyTrackingEngine(work_time=0.01)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=2)

        orch.run_batch([_job("yt1", "https://www.youtube.com/watch?v=AAAAAAAAAAA")])

        assert not any("youtube_conservative" in r.getMessage() for r in caplog.records)

    def test_multi_youtube_job_batch_logs_serialization_once_per_job(self, caplog):
        """Once the gate actually engages (>1 YouTube job in the batch),
        the orchestrator's log line is the one that may mention parallel=
        and cooldown= — it owns that behavior."""
        from core.download_orchestrator import DownloadOrchestrator

        caplog.set_level(logging.INFO, logger="core.download_orchestrator")
        engine = ConcurrencyTrackingEngine(work_time=0.01)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=2)

        with patch("core.download_orchestrator.CONSERVATIVE_DELAY_RANGE", (0.01, 0.02)):
            orch.run_batch([
                _job("yt1", "https://www.youtube.com/watch?v=AAAAAAAAAAA"),
                _job("yt2", "https://www.youtube.com/watch?v=BBBBBBBBBBB"),
            ])

        lines = [r.getMessage() for r in caplog.records if "youtube_conservative" in r.getMessage()]
        assert len(lines) == 2  # one announcement per gated job
        assert all("parallel=1" in line and "cooldown=" in line for line in lines)
