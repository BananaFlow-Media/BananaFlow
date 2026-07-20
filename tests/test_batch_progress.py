"""
tests/test_batch_progress.py  –  BatchProgressAggregator unit tests
=====================================================================
Pure-Python, no Qt. Covers the batch progress / speed / ETA math that the
Downloads-page footer renders:

  * one job, many equal-size jobs, tiny+huge jobs
  * known vs unknown total sizes, and the hybrid fallback
  * parallel active jobs, queued/completed/failed/paused/cancelled states
  * monotonicity, 0..1 clamping
  * aggregate speed == sum of active-job speeds; stale speed removed
  * batch ETA is not the latest per-track ETA; "calculating" fallback

Run:
    pytest tests/test_batch_progress.py -v
"""

from __future__ import annotations

import pytest

from core.batch_progress import BatchProgressAggregator, BatchSnapshot, JobState


# ── Fixtures / helpers ──────────────────────────────────────────────────────

class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _agg(keys=None, smoothing=1.0, clock=None):
    """smoothing=1.0 => no EMA lag, so speed asserts are exact."""
    a = BatchProgressAggregator(speed_smoothing=smoothing, time_fn=clock)
    a.reset(keys or [])
    return a


# ── Single job ──────────────────────────────────────────────────────────────

class TestSingleJob:
    def test_one_job_known_size(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=50, total_bytes=100)
        assert a.snapshot().progress == pytest.approx(0.5)
        a.complete("a")
        assert a.snapshot().progress == pytest.approx(1.0)

    def test_one_job_unknown_size_uses_fraction(self):
        a = _agg(["a"])
        a.update("a", fraction=0.4)
        snap = a.snapshot()
        assert snap.progress == pytest.approx(0.4)
        assert snap.byte_weighted is False


# ── Many jobs ───────────────────────────────────────────────────────────────

class TestManyJobs:
    def test_many_equal_size_jobs(self):
        a = _agg(["a", "b", "c", "d"])
        for k in "abcd":
            a.update(k, downloaded_bytes=25, total_bytes=100)
        # 100 of 400 bytes
        assert a.snapshot().progress == pytest.approx(0.25)

    def test_tiny_and_huge_job_is_byte_weighted_not_averaged(self):
        a = _agg(["tiny", "huge"])
        # tiny fully done (5 MB), huge barely started (1 GB, 0%)
        a.complete("tiny", final_bytes=5_000_000)
        a.update("huge", downloaded_bytes=0, total_bytes=1_000_000_000)
        snap = a.snapshot()
        # A naive fraction average would say 50%. Byte-weighting says ~0.5%.
        assert snap.progress < 0.02
        assert snap.byte_weighted is True

    def test_huge_job_progress_reflects_bytes(self):
        a = _agg(["tiny", "huge"])
        a.complete("tiny", final_bytes=5_000_000)
        a.update("huge", downloaded_bytes=500_000_000, total_bytes=1_000_000_000)
        snap = a.snapshot()
        # (5M + 500M) / (5M + 1000M) ≈ 0.5024
        assert snap.progress == pytest.approx(505_000_000 / 1_005_000_000, abs=1e-3)


# ── Known + unknown hybrid ──────────────────────────────────────────────────

class TestHybridUnknownSizes:
    def test_unknown_size_job_estimated_from_known(self):
        a = _agg(["known", "unknown"])
        a.update("known", downloaded_bytes=100, total_bytes=100)   # done-ish
        a.update("unknown", fraction=0.0)                          # no total yet
        snap = a.snapshot()
        # known contributes 100/100; unknown estimated at mean known size (100)
        # progress = 100 / (100 + 100) = 0.5
        assert snap.progress == pytest.approx(0.5)
        assert snap.byte_weighted is True

    def test_progress_does_not_freeze_at_zero_without_totals(self):
        a = _agg(["a", "b"])
        a.update("a", fraction=0.5)
        a.update("b", fraction=0.1)
        assert a.snapshot().progress == pytest.approx(0.3)

    def test_estimate_floor_corrects_when_real_total_is_much_larger(self):
        a = _agg(["a", "b"])
        # a: known small; b: unknown, estimated large at first
        a.update("a", downloaded_bytes=90, total_bytes=100)
        a.update("b", fraction=0.9)
        p1 = a.snapshot().progress
        # Now b reveals it is actually enormous and barely started —
        # holding the old estimate-derived floor would leave the batch stuck
        # near completion for a long time, so the floor must correct downward.
        a.update("b", downloaded_bytes=1, total_bytes=10_000_000)
        p2 = a.snapshot().progress
        assert p1 > 0.8
        assert p2 < 0.01

    def test_real_byte_progress_remains_monotonic_for_minor_jitter(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=50, total_bytes=100)
        p1 = a.snapshot().progress
        # yt-dlp can repeat an older downloaded byte count; do not step back.
        a.update("a", downloaded_bytes=40, total_bytes=100)
        assert a.snapshot().progress == pytest.approx(p1)

    def test_total_bytes_estimate_is_marked_as_estimated(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=100, total_bytes_estimate=1000, speed_bps=100.0)
        snap = a.snapshot()
        assert snap.progress == pytest.approx(0.1)
        assert snap.byte_weighted is True
        assert snap.eta_is_estimate is True
        a.update("a", downloaded_bytes=100, total_bytes=1000, speed_bps=100.0)
        assert a.snapshot().eta_is_estimate is False


# ── Bounds ──────────────────────────────────────────────────────────────────

class TestBounds:
    def test_progress_never_exceeds_one(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=200, total_bytes=100)  # over-report
        assert a.snapshot().progress <= 1.0

    def test_progress_never_negative(self):
        a = _agg(["a"])
        a.update("a", fraction=-5.0)
        assert a.snapshot().progress >= 0.0

    def test_empty_batch_is_zero_not_complete(self):
        a = _agg([])
        snap = a.snapshot()
        assert snap.is_empty
        assert snap.progress == 0.0


# ── Speed aggregation ───────────────────────────────────────────────────────

class TestSpeed:
    def test_aggregate_speed_is_sum_of_active_jobs(self):
        a = _agg(["a", "b", "c"])
        a.update("a", fraction=0.1, speed_bps=1000.0)
        a.update("b", fraction=0.1, speed_bps=2000.0)
        a.update("c", fraction=0.1, speed_bps=3000.0)
        snap = a.snapshot()
        assert snap.raw_speed_bps == pytest.approx(6000.0)
        assert snap.speed_bps == pytest.approx(6000.0)  # smoothing=1.0

    def test_stale_speed_removed_when_job_finishes(self):
        a = _agg(["a", "b"])
        a.update("a", fraction=0.5, speed_bps=1000.0)
        a.update("b", fraction=0.5, speed_bps=2000.0)
        assert a.snapshot().raw_speed_bps == pytest.approx(3000.0)
        a.complete("b")
        # b's speed must no longer count
        assert a.snapshot().raw_speed_bps == pytest.approx(1000.0)

    def test_speed_zero_when_all_paused(self):
        a = _agg(["a"])
        a.update("a", fraction=0.5, speed_bps=1000.0)
        a.pause("a")
        assert a.snapshot().speed_bps == 0.0
        assert a.snapshot().paused == 1


# ── Job-state counting ──────────────────────────────────────────────────────

class TestStateCounts:
    def test_counts_across_states(self):
        a = _agg(["a", "b", "c", "d", "e"])
        a.update("a", fraction=0.5)          # active
        a.complete("b")                      # completed
        a.fail("c")                          # failed
        a.pause("d")                         # paused
        a.cancel("e")                        # cancelled
        snap = a.snapshot()
        assert snap.total == 5
        assert snap.active == 1
        assert snap.completed == 1
        assert snap.failed == 1
        assert snap.paused == 1
        assert snap.cancelled == 1
        assert snap.finished == 3   # completed + failed + cancelled


# ── ETA ─────────────────────────────────────────────────────────────────────

class TestETA:
    def test_eta_from_remaining_bytes_and_speed(self):
        a = _agg(["a"])
        # 100 bytes total, 20 done, 40 B/s => remaining 80 / 40 = 2s
        a.update("a", downloaded_bytes=20, total_bytes=100, speed_bps=40.0)
        snap = a.snapshot()
        assert snap.eta_seconds == pytest.approx(2.0, abs=1e-6)
        assert snap.eta_is_estimate is False  # only remaining job is known-size

    def test_eta_is_not_the_latest_per_track_eta(self):
        a = _agg(["a", "b"])
        # Two active jobs; the last one to report has a tiny per-track ETA,
        # but the batch has lots of bytes left => batch ETA must be larger.
        a.update("a", downloaded_bytes=0,   total_bytes=1_000_000, speed_bps=1000.0, eta_seconds=1000.0)
        a.update("b", downloaded_bytes=999, total_bytes=1_000,     speed_bps=1000.0, eta_seconds=1.0)
        snap = a.snapshot()
        # remaining ≈ 1_000_000 + 1 bytes over 2000 B/s ≈ 500s — not 1s.
        assert snap.eta_seconds > 100.0

    def test_eta_calculating_when_no_data(self):
        a = _agg(["a", "b"])   # queued, nothing known
        assert a.snapshot().eta_seconds is None

    def test_eta_estimate_flag_when_unknown_sizes_present(self):
        a = _agg(["a", "b"])
        a.update("a", downloaded_bytes=50, total_bytes=100, speed_bps=50.0)
        a.update("b", fraction=0.0, speed_bps=50.0)   # unknown size
        snap = a.snapshot()
        assert snap.eta_seconds is not None
        assert snap.eta_is_estimate is True

    def test_eta_never_negative(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=150, total_bytes=100, speed_bps=10.0)
        assert a.snapshot().eta_seconds >= 0.0

    def test_eta_duration_fallback_between_conservative_jobs(self):
        clock = FakeClock()
        a = _agg(["a", "b"], clock=clock)
        # a runs 10s then completes; b is queued with no bytes and speed 0
        a.update("a", fraction=0.5, speed_bps=100.0)
        clock.advance(10.0)
        a.complete("a")
        # speed now 0 (cooldown), b still queued — duration history kicks in
        snap = a.snapshot()
        assert snap.eta_seconds is not None
        assert snap.eta_is_estimate is True

    def test_eta_includes_configured_start_stagger_for_unsubmitted_jobs(self):
        a = BatchProgressAggregator(speed_smoothing=1.0)
        a.reset(["a", "b", "c"], stagger_delay_range=(2.0, 4.0))
        a.mark_submitted("a")
        a.update("a", downloaded_bytes=50, total_bytes=100, speed_bps=50.0)
        snap = a.snapshot()
        # Network ETA: (50 + 100 + 100) / 50 = 5s.
        # Start-stagger ETA: b and c are not submitted yet, avg 3s each = 6s.
        assert snap.eta_seconds == pytest.approx(11.0, abs=1e-6)
        assert snap.eta_is_estimate is True


# ── Cancellation / pause preserve progress ──────────────────────────────────

class TestCancellationSemantics:
    def test_cancel_preserves_actual_progress_not_100(self):
        a = _agg(["a", "b"])
        a.update("a", downloaded_bytes=50, total_bytes=100)
        a.update("b", downloaded_bytes=0, total_bytes=100)
        a.cancel("a")
        a.cancel("b")
        # Real progress is 25% — cancellation must NOT jump to 100%.
        assert a.snapshot().progress == pytest.approx(0.25)

    def test_pause_preserves_progress(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=30, total_bytes=100)
        before = a.snapshot().progress
        a.pause("a")
        assert a.snapshot().progress == pytest.approx(before)

    def test_late_terminal_callbacks_do_not_overwrite_cancellation(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=40, total_bytes=100)
        a.cancel("a")
        a.complete("a", final_bytes=100)
        snap = a.snapshot()
        assert snap.cancelled == 1
        assert snap.completed == 0
        assert snap.progress == pytest.approx(0.4)

    def test_cancel_outstanding_preserves_already_completed_jobs(self):
        a = _agg(["a", "b"])
        a.complete("a", final_bytes=100)
        changed = a.cancel_outstanding()
        snap = a.snapshot()
        assert changed == ["b"]
        assert snap.completed == 1
        assert snap.cancelled == 1
        assert snap.progress < 1.0
