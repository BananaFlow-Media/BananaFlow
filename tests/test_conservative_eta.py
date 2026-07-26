"""
tests/test_conservative_eta.py  –  Conservative-mode YouTube cooldown in the ETA
================================================================================
The orchestrator's YouTube-conservative gate (core.youtube_reliability) runs
one YouTube job at a time and sleeps CONSERVATIVE_DELAY_RANGE between them. The
whole-batch ETA must account for that delay, because it is wall-clock time the
application itself is forcing on the user.

It used to account for it by *modelling* it: one average cooldown added per job
still queued behind the gate, plus one average start-stagger per unsubmitted
job. On a real 59-track batch that opened at ~22 minutes against a ~9 minute
actual — the stagger term double-counted sleeps that overlap the gate wait
entirely, and neither term shrank as the delays were actually paid.

BatchProgressAggregator now *measures* instead. A job only becomes terminal
after it has published, so the cooldown that preceded it is already inside the
wall time between completions. These tests pin that: the cooldown reaches the
estimate through measurement, no modelled overhead survives on top of it, and
the estimate tracks the real serial rate rather than a projection of it.

Deterministic — a FakeClock stands in for time.monotonic.
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

# A serialized YouTube track costs roughly one download plus one cooldown.
DOWNLOAD_S = 2.0
COOLDOWN_S = 7.5
SERVICE_S = DOWNLOAD_S + COOLDOWN_S     # 9.5s per track, one at a time


def _agg(keys, conservative=True, clock=None, smoothing=1.0):
    a = BatchProgressAggregator(
        speed_smoothing=smoothing,
        time_fn=clock,
        conservative_delay_range=DELAY_RANGE if conservative else None,
    )
    a.reset(keys, conservative_delay_range=DELAY_RANGE if conservative else None)
    if conservative:
        for k in keys:
            a.mark_serialized(k)
    return a


def _run_serial_tracks(a, clock, keys, service=SERVICE_S):
    """Complete `keys` one at a time, each costing `service` wall seconds."""
    for k in keys:
        clock.advance(service)
        a.complete(k)


# ── The cooldown reaches the ETA by measurement ──────────────────────────────

class TestCooldownIsMeasuredNotModelled:
    def test_serial_cooldown_shows_up_in_the_estimate(self):
        clock = FakeClock()
        keys = [f"k{i}" for i in range(20)]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, keys[:4])
        # 4 tracks in 4 × 9.5s => the measured cycle is the full service cost,
        # cooldown included, not just the 2s of transfer.
        snap = a.snapshot()
        assert snap.eta_seconds == pytest.approx(16 * SERVICE_S, abs=1e-6)

    def test_no_modelled_overhead_is_added_on_top(self):
        """The old model added 7.5s per queued serialized job *in addition* to
        the transfer estimate. Nothing may do that any more, or the cooldown
        would be counted twice: once measured, once modelled."""
        clock = FakeClock()
        keys = [f"k{i}" for i in range(20)]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, keys[:4])
        measured_only = 16 * SERVICE_S
        snap = a.snapshot()
        # The old formula would have produced measured_only + 16 × 7.5 = +120s.
        assert snap.eta_seconds < measured_only + 1.0

    def test_conservative_flag_does_not_change_the_estimate(self):
        """mark_serialized is diagnostic now. Two identically-paced batches
        must estimate identically whether or not the gate flag is set."""
        keys = [f"k{i}" for i in range(10)]

        c1 = FakeClock()
        gated = _agg(keys, conservative=True, clock=c1)
        _run_serial_tracks(gated, c1, keys[:3])

        c2 = FakeClock()
        plain = _agg(keys, conservative=False, clock=c2)
        _run_serial_tracks(plain, c2, keys[:3])

        assert gated.snapshot().eta_seconds == pytest.approx(
            plain.snapshot().eta_seconds, abs=1e-6
        )

    def test_estimate_tracks_the_real_batch_duration(self):
        """The regression this whole change exists for: a 59-track serialized
        batch that really takes ~9.3 minutes must not be announced as ~22."""
        clock = FakeClock()
        keys = [f"k{i}" for i in range(59)]
        a = _agg(keys, clock=clock)
        start = clock.t
        true_total = 59 * SERVICE_S            # 560.5s ≈ 9.3 min

        # Warm-up: no number at all until three tracks have actually
        # completed. One interval is too thin a sample to quote from.
        assert a.snapshot().eta_seconds is None

        done = 0
        for target in (3, 15, 30, 45):
            _run_serial_tracks(a, clock, keys[done:target])
            done = target
            snap = a.snapshot()
            true_remaining = true_total - (clock.t - start)
            assert snap.eta_seconds == pytest.approx(true_remaining, rel=0.25), (
                f"after {done} completions: predicted {snap.eta_seconds:.0f}s "
                f"vs true {true_remaining:.0f}s"
            )


# ── Zero speed during a cooldown ─────────────────────────────────────────────

class TestZeroSpeedDuringCooldown:
    def test_estimate_survives_a_cooldown_with_nothing_active(self):
        clock = FakeClock()
        keys = [f"k{i}" for i in range(10)]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, keys[:3])
        before = a.snapshot().eta_seconds

        clock.advance(COOLDOWN_S)          # gate cooldown: no job transferring
        snap = a.snapshot()
        assert snap.raw_speed_bps == 0.0
        assert snap.eta_seconds is not None
        # It counts down through the dead time rather than freezing or resetting.
        assert snap.eta_seconds < before

    def test_speed_decays_instead_of_snapping_to_zero(self):
        """Speed is a display metric and used to flicker to 0 on every
        completion and throughout every cooldown. It now decays."""
        clock = FakeClock()
        a = _agg(["a", "b"], clock=clock, smoothing=0.3)
        a.update("a", downloaded_bytes=1000, total_bytes=10_000, speed_bps=1000.0)
        clock.advance(0.5)
        a.update("a", downloaded_bytes=1500, total_bytes=10_000, speed_bps=1000.0)
        a.complete("a")
        # Immediately after the completion the aggregate is still settling.
        assert a.snapshot().speed_bps > 0.0


# ── Pause / cancel interaction with the outstanding count ────────────────────

class TestPauseResume:
    def test_paused_job_is_not_counted_as_outstanding_work(self):
        clock = FakeClock()
        keys = ["a", "b", "c", "d", "e"]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, ["a", "b", "c"])
        with_two_left = a.snapshot().eta_seconds

        a.pause("d")
        with_one_left = a.snapshot().eta_seconds
        assert with_one_left < with_two_left
        assert a.snapshot().paused == 1

    def test_reset_restarts_the_measurement(self):
        """A resumed batch runs a fresh orchestrator and aggregator; it measures
        its own rate rather than inheriting a stale one."""
        clock = FakeClock()
        keys = ["a", "b", "c", "d"]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, ["a", "b", "c"])
        assert a.snapshot().eta_seconds is not None

        a.reset(keys, conservative_delay_range=DELAY_RANGE)
        assert a.snapshot().eta_seconds is None


class TestCancellation:
    def test_cancelled_jobs_leave_the_outstanding_count(self):
        clock = FakeClock()
        keys = [f"k{i}" for i in range(10)]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, keys[:3])
        before = a.snapshot().eta_seconds

        a.cancel("k9")
        after = a.snapshot().eta_seconds
        assert after == pytest.approx(before - SERVICE_S, abs=1e-6)

    def test_eta_is_none_once_everything_is_terminal(self):
        clock = FakeClock()
        keys = ["a", "b", "c"]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, ["a", "b", "c"])
        a.cancel("c")
        assert a.snapshot().eta_seconds is None


# ── Estimate labelling ───────────────────────────────────────────────────────

class TestEtaAlwaysMarkedEstimate:
    def test_eta_never_negative(self):
        clock = FakeClock()
        keys = [f"k{i}" for i in range(5)]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, keys[:4])
        clock.advance(600.0)      # long stall, way past the measured cycle
        eta = a.snapshot().eta_seconds
        assert eta is None or eta >= 0.0

    def test_eta_is_flagged_an_estimate_whenever_present(self):
        clock = FakeClock()
        keys = [f"k{i}" for i in range(6)]
        a = _agg(keys, clock=clock)
        _run_serial_tracks(a, clock, keys[:3])
        snap = a.snapshot()
        assert snap.eta_seconds is not None
        assert snap.eta_is_estimate is True
