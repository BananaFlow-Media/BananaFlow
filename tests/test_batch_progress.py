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

    def test_total_bytes_estimate_still_weights_progress_by_bytes(self):
        a = _agg(["a"])
        a.update("a", downloaded_bytes=100, total_bytes_estimate=1000, speed_bps=100.0)
        snap = a.snapshot()
        assert snap.progress == pytest.approx(0.1)
        assert snap.byte_weighted is True
        # An estimated total is replaced by the real one without disturbing
        # byte-weighting.
        a.update("a", downloaded_bytes=100, total_bytes=1000, speed_bps=100.0)
        snap = a.snapshot()
        assert snap.progress == pytest.approx(0.1)
        assert snap.byte_weighted is True


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
    """The ETA extrapolates measured throughput. See test_batch_eta_model.py
    for the estimator's own exhaustive coverage; these are the contract-level
    properties every consumer relies on."""

    def test_eta_from_measured_completion_rate(self):
        clock = FakeClock()
        a = _agg([f"k{i}" for i in range(10)], clock=clock)
        # Three completions 10s apart. The first ends the batch's startup, so
        # the two intervals after it measure 10s per track.
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        # 7 outstanding × 10s, minus 0s elapsed into the current cycle.
        assert a.snapshot().eta_seconds == pytest.approx(70.0, abs=1e-6)

    def test_eta_is_not_the_latest_per_track_eta(self):
        clock = FakeClock()
        a = _agg([f"k{i}" for i in range(40)], clock=clock)
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        # A track reports a 1-second per-track ETA of its own. The batch still
        # has 38 tracks to get through and must say so.
        a.update("k2", downloaded_bytes=999, total_bytes=1000,
                 speed_bps=1000.0, eta_seconds=1.0)
        snap = a.snapshot()
        assert snap.eta_seconds is not None
        assert snap.eta_seconds > 100.0

    def test_eta_calculating_when_no_data(self):
        a = _agg(["a", "b"])   # queued, nothing known
        assert a.snapshot().eta_seconds is None

    def test_eta_is_always_flagged_an_estimate(self):
        clock = FakeClock()
        a = _agg(["a", "b", "c", "d"], clock=clock)
        for k in ("a", "b", "c"):
            clock.advance(5.0)
            a.complete(k)
        snap = a.snapshot()
        assert snap.eta_seconds is not None
        # Extrapolating a measured rate is never more than a projection, even
        # when every remaining byte total happens to be known.
        assert snap.eta_is_estimate is True

    def test_eta_never_negative(self):
        clock = FakeClock()
        a = _agg(["a", "b", "c"], clock=clock)
        for k in ("a", "b", "c"):
            clock.advance(10.0)
            a.complete(k)
        eta = a.snapshot().eta_seconds
        assert eta is None or eta >= 0.0

    def test_eta_survives_a_cooldown_with_no_active_job(self):
        clock = FakeClock()
        a = _agg(["a", "b", "c", "d"], clock=clock)
        for k in ("a", "b", "c"):
            clock.advance(10.0)
            a.complete(k)
        # Conservative-mode cooldown: nothing active, aggregate speed is zero.
        clock.advance(5.0)
        snap = a.snapshot()
        assert snap.speed_bps == 0.0
        assert snap.eta_seconds is not None


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

    def test_cancel_outstanding_preserves_paused_jobs(self):
        """Issue #61. A whole-batch pause cancels the engine, so every paused
        job lands in this sweep — and used to come out CANCELLED, leaving the
        snapshot saying `paused=0` while every card in the UI read "paused"."""
        a = _agg(["a", "b"])
        a.pause("a")
        changed = a.cancel_outstanding()
        snap = a.snapshot()
        assert changed == ["b"], "a paused job is not outstanding work"
        assert snap.paused == 1
        assert snap.cancelled == 1

    def test_pause_never_resurrects_a_terminal_job(self):
        """Pausing is decided against a state read that the job's own thread
        can invalidate a microsecond later. Moving a COMPLETED job back to
        PAUSED would put finished work into the outstanding set and offer a
        Resume for a file that is already correct on disk."""
        for terminal in ("complete", "fail", "cancel", "mark_preexisting"):
            a = _agg(["a"])
            getattr(a, terminal)("a")
            a.pause("a")
            assert a.job_state("a") != JobState.PAUSED, (
                f"pause overwrote a job already {terminal}d"
            )
            assert a.snapshot().paused == 0

    def test_an_explicit_cancel_still_moves_a_paused_job(self):
        """Cancel All abandons paused work. Only the sweeping
        cancel_outstanding leaves PAUSED alone — a targeted cancel must not."""
        a = _agg(["a"])
        a.pause("a")
        a.cancel("a")
        assert a.job_state("a") == JobState.CANCELLED
        assert a.snapshot().paused == 0


# ── Duplicate-skip ("preexisting") accounting ────────────────────────────────
# Root-cause coverage for the "19/19 instead of 59/59" bug: a batch of 40
# duplicate-skips + 19 real downloads must report 59/59 completed, with the
# 40 skips distinguishable from the 19 real downloads.

class TestPreexistingAccounting:
    def test_preexisting_counts_as_completed(self):
        keys = [f"skip{i}" for i in range(40)] + [f"dl{i}" for i in range(19)]
        a = _agg(keys)
        for i in range(40):
            a.mark_preexisting(f"skip{i}")
        for i in range(19):
            a.complete(f"dl{i}", final_bytes=100)
        snap = a.snapshot()
        assert snap.total == 59
        assert snap.completed == 59
        assert snap.preexisting == 40
        assert snap.downloaded == 19
        assert snap.progress == pytest.approx(1.0)

    def test_preexisting_is_terminal_success(self):
        a = _agg(["a"])
        a.mark_preexisting("a")
        snap = a.snapshot()
        assert snap.completed == 1
        assert snap.preexisting == 1
        assert snap.progress == pytest.approx(1.0)
        assert snap.speed_bps == 0.0
        assert snap.finished == 1

        # A terminal state cannot be reopened by a stray late update.
        a.update("a", downloaded_bytes=1, total_bytes=100, fraction=0.01)
        snap2 = a.snapshot()
        assert snap2.completed == 1
        assert snap2.preexisting == 1

    def test_preexisting_does_not_contend_with_failed_or_cancelled(self):
        a = _agg(["a"])
        a.fail("a")
        a.mark_preexisting("a")  # must not resurrect a terminal failure
        snap = a.snapshot()
        assert snap.failed == 1
        assert snap.preexisting == 0

    def test_mixed_batch_progress_not_diluted_by_preexisting(self):
        """An in-flight real download's progress must be weighted correctly
        alongside instantly-terminal preexisting jobs, not averaged down."""
        a = _agg(["skip", "dl"])
        a.mark_preexisting("skip")
        a.update("dl", downloaded_bytes=50, total_bytes=100)
        snap = a.snapshot()
        # skip contributes 100/100 done, dl contributes 50/100 done.
        assert snap.progress == pytest.approx(0.75)
