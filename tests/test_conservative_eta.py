"""
tests/test_conservative_eta.py  –  Conservative-mode YouTube ETA overhead
==========================================================================
core.batch_progress.BatchProgressAggregator must fold the mandatory,
non-overlappable cooldown the orchestrator's YouTube-conservative gate
enforces (core.youtube_reliability.CONSERVATIVE_DELAY_RANGE) into the
whole-batch ETA — otherwise the footer promises a time that ignores a delay
the application itself is forcing.

These tests are deterministic (a FakeClock stands in for time.monotonic) and
cover: conservative mode off, one/many queued serialized jobs, a partially
completed batch, pause/resume, cancellation, unknown/known byte sizes, and
zero active speed during a cooldown.
"""

from __future__ import annotations

import pytest

from core.batch_progress import BatchProgressAggregator


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


DELAY_RANGE = (5.0, 10.0)   # avg 7.5s — matches core.youtube_reliability shape


def _agg(keys, conservative=True, clock=None, smoothing=1.0):
    a = BatchProgressAggregator(
        speed_smoothing=smoothing,
        time_fn=clock,
        conservative_delay_range=DELAY_RANGE if conservative else None,
    )
    a.reset(keys)
    return a


class TestConservativeModeDisabled:
    def test_no_overhead_when_delay_range_not_configured(self):
        a = _agg(["a", "b"], conservative=False)
        a.mark_serialized("b")   # marking has no effect without a delay range
        a.update("a", downloaded_bytes=50, total_bytes=100, speed_bps=50.0)
        snap = a.snapshot()
        # Pure byte-based ETA: (100-50 + 100) remaining / 50 bps = 3.0s exactly,
        # no conservative overhead added.
        assert snap.eta_seconds == pytest.approx(3.0, abs=1e-6)


class TestOneQueuedSerializedJob:
    def test_overhead_equals_one_average_cooldown(self):
        a = _agg(["a"])
        a.mark_serialized("a")   # still QUEUED — never updated
        snap = a.snapshot()
        assert snap.eta_seconds == pytest.approx(7.5, abs=1e-6)
        assert snap.eta_is_estimate is True

    def test_overhead_added_on_top_of_byte_based_eta(self):
        a = _agg(["a", "b"])
        a.mark_serialized("b")   # b queued behind the gate
        a.update("a", downloaded_bytes=50, total_bytes=100, speed_bps=100.0)
        snap = a.snapshot()
        # network eta = (50 + 100) remaining / 100 bps = 1.5s; + 7.5s cooldown
        assert snap.eta_seconds == pytest.approx(1.5 + 7.5, abs=1e-6)


class TestManyQueuedSerializedJobs:
    def test_overhead_scales_linearly(self):
        a = _agg(["a", "b", "c", "d"])
        for k in ("b", "c", "d"):
            a.mark_serialized(k)
        snap = a.snapshot()
        # 3 queued serialized jobs * 7.5s average = 22.5s (no byte info at all
        # for "a" either, so nothing else contributes).
        assert snap.eta_seconds == pytest.approx(22.5, abs=1e-6)


class TestPartiallyCompletedBatch:
    def test_completed_serialized_jobs_do_not_count(self):
        a = _agg(["a", "b", "c"])
        for k in ("a", "b", "c"):
            a.mark_serialized(k)
        a.complete("a", final_bytes=1000)   # finished — no longer queued
        snap = a.snapshot()
        # Only b, c still queued => 2 * 7.5s
        assert snap.eta_seconds == pytest.approx(15.0, abs=1e-6)

    def test_active_serialized_job_excluded_from_overhead_but_counted_in_bytes(self):
        a = _agg(["a", "b"])
        a.mark_serialized("a")
        a.mark_serialized("b")
        # "a" is now downloading (ACTIVE) — not queued anymore.
        a.update("a", downloaded_bytes=50, total_bytes=100, speed_bps=50.0)
        snap = a.snapshot()
        # network eta for a+b(unknown, mean=100) = (50+100)/50 = 3.0s
        # + 1 remaining queued serialized job (b) * 7.5s
        assert snap.eta_seconds == pytest.approx(3.0 + 7.5, abs=1e-6)


class TestPauseResume:
    def test_paused_serialized_job_does_not_count_as_queued(self):
        a = _agg(["a", "b"])
        a.mark_serialized("b")
        a.update("a", downloaded_bytes=10, total_bytes=100, speed_bps=10.0)
        a.pause("b")
        snap = a.snapshot()
        # b is PAUSED, not QUEUED -> no cooldown overhead while paused.
        assert snap.paused == 1
        assert snap.eta_seconds == pytest.approx((90 + 100) / 10.0, abs=1e-6)

    def test_resume_restores_overhead_once_queued_again(self):
        a = _agg(["a", "b"])
        a.mark_serialized("b")
        a.pause("b")
        # While paused, "b" is neither queued nor active — no cooldown
        # overhead accrues and there's no other data, so ETA is unknown.
        assert a.snapshot().eta_seconds is None
        # Resuming a paused job re-enters it as ACTIVE via update(); it should
        # no longer contribute cooldown overhead (it's now transferring).
        a.update("b", downloaded_bytes=0, total_bytes=100, speed_bps=10.0)
        snap = a.snapshot()
        assert snap.active == 1
        assert snap.paused == 0


class TestCancellation:
    def test_cancelled_serialized_job_excluded(self):
        a = _agg(["a", "b", "c"])
        for k in ("a", "b", "c"):
            a.mark_serialized(k)
        a.cancel("a")
        a.cancel("b")
        snap = a.snapshot()
        # Only "c" remains queued and serialized => 1 * 7.5s.
        assert snap.eta_seconds == pytest.approx(7.5, abs=1e-6)


class TestUnknownAndKnownSizes:
    def test_unknown_sizes_still_produce_an_eta_with_overhead(self):
        a = _agg(["a", "b"])
        a.mark_serialized("b")
        a.update("a", fraction=0.5, speed_bps=10.0)   # no byte totals at all
        snap = a.snapshot()
        # No byte info anywhere -> falls through to duration/overhead path.
        # duration history is empty and speed condition requires mean_known,
        # which is None here, so only the overhead should surface.
        assert snap.eta_seconds == pytest.approx(7.5, abs=1e-6)
        assert snap.eta_is_estimate is True

    def test_known_byte_totals_combine_additively_with_overhead(self):
        a = _agg(["a", "b"])
        a.mark_serialized("b")
        a.update("a", downloaded_bytes=0, total_bytes=1000, speed_bps=100.0)
        snap = a.snapshot()
        # network eta = (1000 + mean_known(1000)) / 100 = 20s; + 7.5s overhead
        assert snap.eta_seconds == pytest.approx(20.0 + 7.5, abs=1e-6)


class TestZeroSpeedDuringCooldown:
    def test_zero_active_speed_falls_back_to_overhead_plus_duration_history(self):
        clock = FakeClock()
        a = _agg(["a", "b"], clock=clock)
        a.mark_serialized("b")
        # "a" runs for 10s then completes (this is the job whose cooldown is
        # currently elapsing — no job is ACTIVE right now, speed is 0).
        a.update("a", fraction=0.5, speed_bps=50.0)
        clock.advance(10.0)
        a.complete("a", final_bytes=500)
        snap = a.snapshot()
        assert snap.active == 0
        assert snap.speed_bps == 0.0
        # duration-history fallback (10s * 1 outstanding / 1 "active-or-1")
        # plus the mandatory cooldown for the still-queued "b".
        assert snap.eta_seconds == pytest.approx(10.0 + 7.5, abs=1e-6)
        assert snap.eta_is_estimate is True

    def test_zero_speed_no_history_yet_returns_overhead_only(self):
        a = _agg(["a"])
        a.mark_serialized("a")
        snap = a.snapshot()
        assert snap.speed_bps == 0.0
        assert snap.eta_seconds == pytest.approx(7.5, abs=1e-6)


class TestEtaAlwaysMarkedEstimate:
    def test_eta_is_estimate_true_whenever_overhead_present(self):
        a = _agg(["a", "b"])
        a.mark_serialized("b")
        a.update("a", downloaded_bytes=100, total_bytes=100, speed_bps=100.0)
        a.complete("a")
        snap = a.snapshot()
        assert snap.eta_is_estimate is True

    def test_eta_never_negative_with_overhead(self):
        a = _agg(["a"])
        a.mark_serialized("a")
        a.update("a", downloaded_bytes=150, total_bytes=100, speed_bps=10.0)  # over-reported
        assert a.snapshot().eta_seconds >= 0.0
