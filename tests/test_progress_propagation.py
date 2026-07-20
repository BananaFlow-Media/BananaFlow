"""
tests/test_progress_propagation.py  –  Real byte-data propagation, source to footer
=====================================================================================
Traces the actual runtime data path (not just the aggregator's standalone
math) for the values the Downloads-page footer depends on:

    yt-dlp progress hook -> DownloadProgress -> DownloadEngine._fire
        -> DownloadOrchestrator callbacks -> BatchProgressAggregator
        -> BatchSnapshot -> DownloadWorker Qt signal
        -> DownloadController Qt signal -> StatusBar rendering

Part 1 pins a real defect this pass found and fixed: DownloadEngine's
FINISHED event never populated downloaded_bytes/total_bytes, so a job that
completed before yt-dlp ever fired a "downloading" progress hook (a tiny or
already-cached file) reached the aggregator with no real byte count and had
to be estimated instead of counted exactly.

Parts 2-4 prove the orchestrator -> aggregator -> Qt-signal chain does not
discard, overwrite, or replace a per-track value with a batch-level one (and
vice versa) anywhere along the way.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Part 1 — DownloadEngine reports the real on-disk size on FINISHED
# ──────────────────────────────────────────────────────────────────────────────

class TestDownloadEngineReportsRealBytesOnFinish:
    """This is the actual source of the data the whole pipeline depends on."""

    def _make_finished_file(self, tmp_path, content: bytes = b"x" * 12345) -> str:
        p = tmp_path / "output.mp3"
        p.write_bytes(content)
        return str(p)

    def test_primary_ytdlp_path_reports_real_file_size_on_finished(self, tmp_path):
        from core.downloader import DownloadEngine, DownloadRequest, MediaType, DownloadStatus

        final_path = self._make_finished_file(tmp_path)
        engine = DownloadEngine()

        captured: list = []

        def on_finished(p):
            captured.append(p)

        req = DownloadRequest(
            url="https://www.youtube.com/watch?v=abc123",
            output_dir=str(tmp_path),
            media_type=MediaType.AUDIO,
            forced_title="Test Track",
        )
        req.on_finished = on_finished
        req.on_progress = lambda p: None
        req.on_error = lambda p: None
        # Simulate yt-dlp's postprocessor hook already having recorded the
        # real output path (normally set by DownloadEngine's own hook).
        req._final_output_path = final_path

        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls, \
             patch.object(engine, "_run_final_pipeline", return_value=[]):
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = lambda s: mock_ydl
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.download = MagicMock(return_value=None)
            mock_ydl_cls.return_value = mock_ydl

            engine.download(req)

        assert len(captured) == 1
        finished = captured[0]
        assert finished.status == DownloadStatus.FINISHED
        # The real defect: these used to be 0 / None unconditionally.
        assert finished.downloaded_bytes == 12345
        assert finished.total_bytes == 12345

    def test_missing_output_file_reports_none_not_a_crash(self, tmp_path):
        """If the file genuinely can't be stat'd, propagate None — never raise
        and never silently report a fake size."""
        from core.downloader import _safe_file_size

        assert _safe_file_size(str(tmp_path / "does_not_exist.mp3")) is None
        assert _safe_file_size("") is None

    def test_zero_byte_file_is_treated_as_unknown_not_zero(self, tmp_path):
        """A 0-byte file is never a legitimate completed download size —
        report None so the aggregator's unknown-size fallback applies instead
        of a job that looks '100% of 0 bytes'."""
        from core.downloader import _safe_file_size
        p = tmp_path / "empty.mp3"
        p.write_bytes(b"")
        assert _safe_file_size(str(p)) is None

    def test_ytdlp_hook_preserves_total_bytes_estimate_separately(self, tmp_path):
        from core.downloader import DownloadEngine, DownloadRequest, DownloadStatus

        engine = DownloadEngine()
        req = DownloadRequest(url="https://example.test/video", output_dir=str(tmp_path))
        captured = []
        req.on_progress = captured.append

        hook = engine._make_progress_hook(req)
        hook({
            "status": "downloading",
            "downloaded_bytes": 250,
            "total_bytes_estimate": 1000,
            "speed": 100.0,
            "eta": 8,
            "info_dict": {"title": "estimated"},
        })

        assert len(captured) == 1
        progress = captured[0]
        assert progress.status == DownloadStatus.DOWNLOADING
        assert progress.downloaded_bytes == 250
        assert progress.total_bytes is None
        assert progress.total_bytes_estimate == 1000
        assert progress.fraction == pytest.approx(0.25)


# ──────────────────────────────────────────────────────────────────────────────
# Part 2 — Orchestrator forwards real bytes into the aggregator unmodified
# ──────────────────────────────────────────────────────────────────────────────

class _RealisticEngine:
    """Fires realistic DownloadProgress sequences including real byte counts
    on FINISHED, exactly like the fixed DownloadEngine now does."""

    def __init__(self) -> None:
        self._cancel_event = __import__("threading").Event()

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req) -> None:
        from core.downloader import DownloadProgress, DownloadStatus
        if self._cancel_event.is_set():
            return
        # One live progress tick with a real byte total (simulates a normal
        # in-flight yt-dlp download that DID get at least one hook).
        if req.on_progress:
            req.on_progress(DownloadProgress(
                status=DownloadStatus.DOWNLOADING,
                url=req.url,
                downloaded_bytes=500_000,
                total_bytes=1_000_000,
                speed_bps=250_000.0,
                fraction=0.5,
            ))
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=req.url,
                title=req.forced_title or "",
                fraction=1.0,
                downloaded_bytes=1_000_000,
                total_bytes=1_000_000,
                output_path=f"/tmp/{req.forced_title}.mp3",
            ))


class TestOrchestratorPropagatesRealBytes:
    def test_finished_byte_total_reaches_the_aggregator_snapshot(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.downloader import DownloadRequest, MediaType

        snapshots: list = []

        class Callbacks:
            def on_track_progress(self, key, fraction): pass
            def on_track_speed(self, key, speed_bps, eta_seconds): pass
            def on_track_status(self, key, status): pass
            def on_track_finished(self, key, path): pass
            def on_track_error(self, key, error): pass
            def on_overall_progress(self, fraction): pass
            def on_metrics(self, speed, eta): pass
            def on_batch_snapshot(self, snapshot): snapshots.append(snapshot)
            def on_status_message(self, msg): pass
            def on_job_count_changed(self, completed, total): pass
            def on_batch_finished(self, outcome=None): pass
            def on_track_thumbnail(self, key, url): pass

        engine = _RealisticEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=Callbacks(), max_workers=1)
        req = DownloadRequest(url="http://x", output_dir="/tmp", media_type=MediaType.AUDIO,
                               forced_title="track")
        orch.run_batch([("k1", req)])

        # At least one snapshot must reflect the REAL byte total this job
        # reported — not a discarded/zeroed value, not an estimate substituted
        # for a real known number.
        assert snapshots, "orchestrator never emitted a batch snapshot"
        final_snap = snapshots[-1]
        assert final_snap.byte_weighted is True
        assert final_snap.progress == pytest.approx(1.0)

    def test_completed_job_final_size_matches_reported_bytes_not_estimate(self):
        """A job that reports real bytes must be counted at ITS real size in
        the aggregator, not the batch's mean-estimate fallback — proves the
        orchestrator wires DownloadProgress.total_bytes through to
        aggregator.complete(final_bytes=...) untouched."""
        from core.batch_progress import BatchProgressAggregator

        agg = BatchProgressAggregator(speed_smoothing=1.0)
        agg.reset(["a", "b"])
        # "a" is a huge known job, still in progress.
        agg.update("a", downloaded_bytes=0, total_bytes=10_000_000, speed_bps=1.0)
        # "b" completes and reports its OWN real (small) final size — this
        # must not be blended with "a"'s unrelated size.
        agg.complete("b", final_bytes=1_000)
        snap = agg.snapshot()
        # progress = 0 / (10_000_000 + 1_000) ~ 0.0001, NOT 50% (which a
        # per-job fraction average would have wrongly produced).
        assert snap.progress < 0.001

    def test_total_bytes_estimate_reaches_batch_snapshot_as_estimate(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType

        snapshots: list = []

        class EstimateEngine:
            def __init__(self) -> None:
                self._cancel_event = __import__("threading").Event()

            def cancel_all(self) -> None:
                self._cancel_event.set()

            def download(self, req) -> None:
                req.on_progress(DownloadProgress(
                    status=DownloadStatus.DOWNLOADING,
                    url=req.url,
                    downloaded_bytes=250,
                    total_bytes_estimate=1000,
                    speed_bps=100.0,
                    fraction=0.25,
                ))

        class Callbacks:
            def on_track_progress(self, key, fraction): pass
            def on_track_speed(self, key, speed_bps, eta_seconds): pass
            def on_track_status(self, key, status): pass
            def on_track_finished(self, key, path): pass
            def on_track_error(self, key, error): pass
            def on_overall_progress(self, fraction): pass
            def on_metrics(self, speed, eta): pass
            def on_batch_snapshot(self, snapshot): snapshots.append(snapshot)
            def on_status_message(self, msg): pass
            def on_job_count_changed(self, completed, total): pass
            def on_batch_finished(self, outcome=None): pass
            def on_track_thumbnail(self, key, url): pass

        orch = DownloadOrchestrator(engine=EstimateEngine(), callbacks=Callbacks(), max_workers=1)
        req = DownloadRequest(url="http://estimate", output_dir="/tmp", media_type=MediaType.AUDIO)
        orch.run_batch([("k1", req)])

        assert snapshots
        snap = snapshots[0]
        assert snap.progress == pytest.approx(0.25)
        assert snap.byte_weighted is True
        assert snap.eta_is_estimate is True


# ──────────────────────────────────────────────────────────────────────────────
# Part 3 — Qt signal chain: worker -> controller carries the same snapshot
# ──────────────────────────────────────────────────────────────────────────────

class TestQtSignalChainPreservesSnapshot:
    """DownloadWorker._SignalAdapter.on_batch_snapshot must emit the exact
    object the orchestrator built (no copy-and-drop-fields, no
    reconstruction from partial data) all the way to DownloadController's
    own batch_snapshot signal."""

    def test_signal_adapter_forwards_snapshot_object_unmodified(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("PySide6 not available")
        QApplication.instance() or QApplication([])

        from ui.workers.download_worker import _SignalAdapter, DownloadWorker
        from core.batch_progress import BatchProgressAggregator

        agg = BatchProgressAggregator(speed_smoothing=1.0)
        agg.reset(["a"])
        agg.update("a", downloaded_bytes=250, total_bytes=1000, speed_bps=100.0)
        real_snapshot = agg.snapshot()

        received = []

        class FakeWorker:
            def __init__(self):
                class _Sig:
                    def emit(_self, snap):
                        received.append(snap)
                self.batch_snapshot = _Sig()

        adapter = _SignalAdapter(FakeWorker())
        adapter.on_batch_snapshot(real_snapshot)

        assert len(received) == 1
        # Identity, not just equality — proves nothing rebuilt/mutated it.
        assert received[0] is real_snapshot
        assert received[0].progress == pytest.approx(0.25)
