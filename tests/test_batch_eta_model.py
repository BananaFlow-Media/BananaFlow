"""
tests/test_batch_eta_model.py  –  The batch ETA's throughput estimator
=======================================================================
core.batch_progress derives "time remaining for the whole batch" by measuring
how fast the batch is actually finishing tracks:

    cycle = (t_last_completion - anchor) / completions_since_anchor
    eta   = outstanding * cycle - min(now - t_last_completion, cycle)

Everything here is pure Python with an injected clock, no Qt.

Several of these tests exist specifically to fail against estimators that were
tried and rejected on the way here, so a future refactor cannot quietly
reintroduce one:

  * TestParallelBurstArrivals — a median of consecutive completion *intervals*
    scores 0 on clustered completions and would collapse the ETA to nothing.
  * TestGateWaitIsNotMultiplied — multiplying each job's own wall time (which
    includes waiting behind other jobs at the serial gate) by the outstanding
    count inflates by up to max_workers.
  * TestNoModelledOverhead in test_conservative_eta.py — adding a modelled
    average cooldown on top of a measured rate counts it twice.
"""

from __future__ import annotations

import pytest

from core.batch_progress import BatchProgressAggregator, JobState


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _agg(keys, clock=None, smoothing=1.0):
    """smoothing=1.0 => no speed EMA lag, so speed asserts are exact."""
    clock = clock or FakeClock()
    a = BatchProgressAggregator(speed_smoothing=smoothing, time_fn=clock)
    a.reset(list(keys))
    return a, clock


def _keys(n, prefix="k"):
    return [f"{prefix}{i}" for i in range(n)]


# ── Warm-up: no confident number without evidence ────────────────────────────

class TestWarmUp:
    def test_no_eta_before_anything_completes(self):
        a, clock = _agg(_keys(59))
        # This is the moment the old model announced ~22 minutes for a batch
        # that took ~9. With nothing measured, the honest answer is "no answer".
        assert a.snapshot().eta_seconds is None

    def test_no_eta_after_only_one_completion(self):
        a, clock = _agg(_keys(10))
        clock.advance(10.0)
        a.complete("k0")
        assert a.snapshot().eta_seconds is None

    def test_no_eta_after_only_two_completions(self):
        """One interval is a coin flip. Measured across randomised batches, a
        rate from a single interval missed by more than a quarter in a third of
        runs - the "seven minutes, then four, then one and a half" complaint."""
        a, clock = _agg(_keys(10))
        for i in range(2):
            clock.advance(10.0)
            a.complete(f"k{i}")
        assert a.snapshot().eta_seconds is None

    def test_eta_appears_on_the_third_completion(self):
        a, clock = _agg(_keys(10))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        assert a.snapshot().eta_seconds == pytest.approx(70.0, abs=1e-6)

    def test_downloading_bytes_alone_does_not_produce_an_eta(self):
        """Byte totals used to be enough to publish a number. They are not
        evidence about how long the batch takes — the post-processing tail, the
        cooldown and the match wait are all outside the transfer."""
        a, clock = _agg(_keys(20))
        a.update("k0", downloaded_bytes=500, total_bytes=1000, speed_bps=100.0)
        clock.advance(5.0)
        a.update("k0", downloaded_bytes=900, total_bytes=1000, speed_bps=100.0)
        assert a.snapshot().eta_seconds is None


# ── Serialized batch: one track at a time ────────────────────────────────────

class TestSerializedCadence:
    def test_cycle_equals_the_service_time(self):
        a, clock = _agg(_keys(59))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        assert a._throughput_cycle_locked() == pytest.approx(10.0, abs=1e-6)

    def test_eta_is_outstanding_times_cycle(self):
        a, clock = _agg(_keys(59))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        # 56 left × 10s, nothing elapsed into the current cycle yet.
        assert a.snapshot().eta_seconds == pytest.approx(560.0, abs=1e-6)

    def test_partial_cycle_is_subtracted(self):
        a, clock = _agg(_keys(59))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        clock.advance(5.0)      # halfway through the fourth track
        assert a.snapshot().eta_seconds == pytest.approx(555.0, abs=1e-6)

    def test_countdown_is_continuous_across_a_completion(self):
        """Just before a completion the estimate already reads (n-1) cycles;
        the instant it lands, outstanding drops and the partial-cycle term
        resets, so the number must not jump."""
        a, clock = _agg(_keys(20))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")

        clock.advance(9.99)
        just_before = a.snapshot().eta_seconds
        clock.advance(0.01)
        a.complete("k3")
        just_after = a.snapshot().eta_seconds
        assert just_after == pytest.approx(just_before, abs=0.05)


# ── Parallel batch: completions arrive in bursts ─────────────────────────────

class TestParallelBurstArrivals:
    """Anti-regression for the median-of-intervals estimator.

    Three workers each finishing a track every 10s complete at
    t = 10,10,10,20,20,20. The gaps between consecutive completions are
    10,0,0,10,0,0 — median 0. Dividing the 20s span by the 6 completions in it
    gives the true 3.33s per track.
    """

    @staticmethod
    def _burst_batch(total=59, waves=3, workers=3, wave_s=10.0):
        """`workers` jobs transfer concurrently, then finish together.

        The jobs are marked active before they complete, as a real parallel
        batch does. That is what tells the aggregator the batch runs three at a
        time rather than one, which in turn tells it that the opening three
        completions are the pipeline filling up rather than three cycles of
        steady-state throughput.
        """
        a, clock = _agg(_keys(total))
        done = 0
        for _ in range(waves):
            for w in range(workers):
                a.update(f"k{done + w}", fraction=0.5, speed_bps=1000.0)
            clock.advance(wave_s)
            for _ in range(workers):
                a.complete(f"k{done}")
                done += 1
        return a, clock, done

    def test_cycle_is_wall_time_per_completion_not_a_median_gap(self):
        a, clock, _ = self._burst_batch()
        assert a._throughput_cycle_locked() == pytest.approx(10.0 / 3.0, abs=1e-6)

    def test_eta_does_not_collapse_toward_zero(self):
        a, clock, done = self._burst_batch()
        eta = a.snapshot().eta_seconds
        assert eta is not None
        # A median-of-intervals estimator gives cycle=0 and therefore eta=0.
        assert eta > 100.0
        assert eta == pytest.approx((59 - done) * (10.0 / 3.0), abs=1e-6)

    def test_startup_before_the_first_wave_is_not_charged_per_track(self):
        """A slow opening - plugin warm-up, the first match, the first extractor
        call - must not be divided across the tracks that follow it."""
        a, clock = _agg(_keys(30))
        for w in range(3):
            a.update(f"k{w}", fraction=0.5, speed_bps=1000.0)
        clock.advance(40.0)                 # 30s of one-off startup + 10s of work
        for i in range(3):
            a.complete(f"k{i}")
        for wave in range(1, 3):
            for w in range(3):
                a.update(f"k{wave * 3 + w}", fraction=0.5, speed_bps=1000.0)
            clock.advance(10.0)
            for i in range(wave * 3, wave * 3 + 3):
                a.complete(f"k{i}")
        # Steady state is 3 tracks per 10s. The 30s of startup must be excluded.
        assert a._throughput_cycle_locked() == pytest.approx(10.0 / 3.0, abs=1e-6)

    def test_eta_is_not_inflated_by_the_worker_count(self):
        """Aggregate throughput, not one worker's service time: 53 tracks at
        3-per-10s is ~177s, not 53 × 10s."""
        a, clock, done = self._burst_batch()
        eta = a.snapshot().eta_seconds
        assert eta < (59 - done) * 10.0 / 2.0

    def test_a_parallel_batch_waits_for_a_second_wave(self):
        """The opening wave of a parallel batch is the pipeline filling up, not
        a measurement of it: the time before it contains everything the batch
        pays once, and it produced `workers` completions rather than one. So a
        three-worker batch needs two waves before it can quote a rate, where a
        serialized one needs two completions. Deliberately slower, and honest -
        measuring the first wave alone charges a whole service time to each of
        three completions and trebles the estimate."""
        a, clock = _agg(_keys(20))
        for w in range(3):
            a.update(f"k{w}", fraction=0.5, speed_bps=1000.0)
        clock.advance(10.0)
        for i in range(3):
            a.complete(f"k{i}")
        assert a.snapshot().eta_seconds is None       # one wave proves nothing

        for w in range(3, 6):
            a.update(f"k{w}", fraction=0.5, speed_bps=1000.0)
        clock.advance(10.0)
        for i in range(3, 6):
            a.complete(f"k{i}")
        snap = a.snapshot()
        assert snap.eta_seconds is not None
        assert snap.eta_seconds == pytest.approx(14 * (10.0 / 3.0), abs=1e-6)

    def test_a_serialized_batch_quotes_after_three_completions(self):
        """One job in flight means the first completion ends the startup, so
        the two after it are already steady state - where a parallel batch
        needs two whole waves."""
        a, clock = _agg(_keys(20))
        for i in range(3):
            a.update(f"k{i}", fraction=0.5, speed_bps=1000.0)
            clock.advance(10.0)
            a.complete(f"k{i}")
        assert a.snapshot().eta_seconds is not None


class TestGateWaitIsNotMultiplied:
    """Anti-regression for `outstanding × median(per-job wall time)`.

    With three workers and a serial gate, three jobs are in flight at once: one
    downloading, two blocked at the gate. Each job's own wall time is therefore
    ~3× the service cycle, and multiplying it by the outstanding count would
    treble the estimate.
    """

    def test_estimate_follows_the_service_cycle_not_job_residence_time(self):
        service = 10.0
        workers = 3
        a, clock = _agg(_keys(30))

        # Jobs start in waves of 3 but complete one at a time, `service` apart:
        # each job's own start-to-finish span is up to 3 × service.
        for i in range(6):
            if i % workers == 0:
                for w in range(workers):
                    if i + w < 30:
                        a.update(f"k{i + w}", fraction=0.01, speed_bps=1.0)
            clock.advance(service)
            a.complete(f"k{i}")

        eta = a.snapshot().eta_seconds
        outstanding = 30 - 6
        assert eta == pytest.approx(outstanding * service, abs=1e-6)
        # The rejected model would have produced ~3× this.
        assert eta < outstanding * service * 1.5


# ── Degenerate and stalled inputs ────────────────────────────────────────────

class TestDegenerateSpan:
    def test_all_completions_on_the_same_timestamp_does_not_divide_by_zero(self):
        a, clock = _agg(_keys(10))
        # Clock never advances: span from the anchor is exactly zero.
        for i in range(3):
            a.complete(f"k{i}")
        snap = a.snapshot()          # must not raise ZeroDivisionError
        assert snap.eta_seconds is None

    def test_falls_back_to_elapsed_when_completions_share_a_timestamp(self):
        a, clock = _agg(_keys(10))
        for i in range(3):
            a.complete(f"k{i}")
        clock.advance(6.0)           # time has passed since the burst
        eta = a.snapshot().eta_seconds
        assert eta is not None
        assert eta > 0.0


class TestStallDegradation:
    def test_a_long_silence_raises_the_estimate(self):
        a, clock = _agg(_keys(20))
        for i in range(4):
            clock.advance(5.0)
            a.complete(f"k{i}")
        healthy = a.snapshot().eta_seconds

        # Nothing completes for far longer than a cycle — a wedged download, a
        # very slow match. The estimate must grow, not sit frozen at its last
        # confident value.
        clock.advance(200.0)
        stalled = a.snapshot().eta_seconds
        assert stalled > healthy

    def test_an_ordinary_cooldown_does_not_trigger_degradation(self):
        a, clock = _agg(_keys(20))
        for i in range(4):
            clock.advance(10.0)
            a.complete(f"k{i}")
        before = a.snapshot().eta_seconds
        clock.advance(7.5)           # well inside one cycle
        after = a.snapshot().eta_seconds
        assert after < before        # counting down, not degrading


# ── Window hygiene ───────────────────────────────────────────────────────────

class TestWindowHygiene:
    def test_preexisting_duplicates_do_not_poison_the_rate(self):
        """Duplicate-skips are resolved in a tight loop before any download
        starts. Twenty of them on the same instant would drive a naive rate to
        near-zero and the ETA with it."""
        a, clock = _agg(_keys(40))
        for i in range(20):
            a.mark_preexisting(f"k{i}")
        assert a.snapshot().eta_seconds is None      # still no real evidence

        for i in range(20, 23):
            clock.advance(10.0)
            a.complete(f"k{i}")
        # 17 real jobs left at the measured 10s each — untouched by the skips.
        assert a.snapshot().eta_seconds == pytest.approx(170.0, abs=1e-6)

    def test_failures_that_ran_the_pipeline_count_as_cycles(self):
        """A failed job that reached the pool still paid for resolve, gate wait,
        attempt and retry backoff, so it is real evidence about throughput."""
        a, clock = _agg(_keys(10))
        clock.advance(10.0)
        a.complete("k0")
        clock.advance(10.0)
        a.complete("k1")
        clock.advance(10.0)
        a.mark_submitted("k2")
        a.fail("k2")
        snap = a.snapshot()
        assert snap.failed == 1
        assert snap.eta_seconds == pytest.approx(70.0, abs=1e-6)

    def test_setup_failures_before_submission_are_not_cycles(self):
        """A job whose private workspace cannot be created is failed by the
        orchestrator up front - before registration, before the pool sees it.
        Those failures land together on essentially the same instant, so
        measuring them would satisfy warm-up with a near-zero span and collapse
        the ETA for every healthy job behind them."""
        a, clock = _agg(_keys(10))
        a.fail("k0")        # never submitted: workspace setup failed
        a.fail("k1")
        assert a.snapshot().failed == 2
        assert a.snapshot().eta_seconds is None      # still nothing measured

    def test_mixed_setup_failures_and_real_downloads(self):
        """The batch that would have broken: two instant setup failures, then
        normal downloads. The estimate must reflect the real work, not the
        failures that consumed no pipeline time."""
        a, clock = _agg(_keys(12))
        a.fail("k0")
        a.fail("k1")
        for i in range(2, 5):
            clock.advance(10.0)
            a.mark_submitted(f"k{i}")
            a.complete(f"k{i}")
        # 7 real jobs left at the measured 10s each. Had the two setup failures
        # entered the window, the cycle would have been 30/5 = 6s and the ETA
        # ~42s instead of ~70s.
        assert a._throughput_cycle_locked() == pytest.approx(10.0, abs=1e-6)
        assert a.snapshot().eta_seconds == pytest.approx(70.0, abs=1e-6)

    def test_cancellations_do_not_count_as_cycles(self):
        """A mass-cancel lands as a burst of simultaneous terminal transitions
        that never represented work being done."""
        a, clock = _agg(_keys(10))
        for i in range(3):
            clock.advance(10.0)
            a.cancel(f"k{i}")
        assert a.snapshot().eta_seconds is None

    def test_window_is_bounded_and_tracks_a_rate_change(self):
        a, clock = _agg(_keys(40))
        for i in range(12):          # slow opening, beyond the 10-wide window
            clock.advance(20.0)
            a.complete(f"k{i}")
        slow = a._throughput_cycle_locked()
        assert slow == pytest.approx(20.0, abs=1e-6)

        for i in range(12, 26):      # then the batch speeds up
            clock.advance(2.0)
            a.complete(f"k{i}")
        fast = a._throughput_cycle_locked()
        assert fast == pytest.approx(2.0, abs=1e-6)


# ── Tail floor ───────────────────────────────────────────────────────────────

class TestTailFloor:
    def test_one_huge_remaining_file_is_not_under_called(self):
        a, clock = _agg(_keys(5))
        for i in range(4):
            clock.advance(1.0)
            a.complete(f"k{i}")
        # Measured cycle is 1s, so the naive estimate for the last track is 1s.
        # But it is a 100 MB file moving at 1 MB/s: 100s of real work left.
        a.update("k4", downloaded_bytes=0, total_bytes=100_000_000,
                 speed_bps=1_000_000.0)
        assert a.snapshot().eta_seconds == pytest.approx(100.0, rel=0.01)

    def test_floor_never_pulls_the_estimate_down(self):
        a, clock = _agg(_keys(40))
        for i in range(4):
            clock.advance(10.0)
            a.complete(f"k{i}")
        # A tiny nearly-finished file must not shrink a 36-track estimate.
        a.update("k4", downloaded_bytes=999, total_bytes=1000, speed_bps=1000.0)
        assert a.snapshot().eta_seconds == pytest.approx(360.0, abs=1e-6)


# ── Per-track values must never become the batch value ───────────────────────

class TestPerTrackNeverBecomesBatch:
    def test_per_track_eta_does_not_reach_the_batch_estimate(self):
        a, clock = _agg(_keys(40))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        baseline = a.snapshot().eta_seconds

        # yt-dlp reports a 5-second ETA for the track currently downloading.
        a.update("k2", fraction=0.9, speed_bps=100.0, eta_seconds=5.0)
        snap = a.snapshot()
        assert snap.eta_seconds == pytest.approx(baseline, abs=1e-6)
        assert snap.eta_seconds > 100.0

    def test_job_eta_seconds_is_stored_but_never_read_by_the_estimator(self):
        a, clock = _agg(_keys(20))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        with_none = a.snapshot().eta_seconds

        a.update("k2", fraction=0.5, speed_bps=50.0, eta_seconds=1.0)
        low = a.snapshot().eta_seconds
        a.update("k2", fraction=0.5, speed_bps=50.0, eta_seconds=9999.0)
        high = a.snapshot().eta_seconds
        # A 10,000× swing in the per-track value must move nothing.
        assert low == pytest.approx(high, abs=1e-6)
        assert low == pytest.approx(with_none, abs=1e-6)


# ── Lifecycle states ─────────────────────────────────────────────────────────

class TestLifecycle:
    def test_single_track_batch_never_claims_a_batch_eta(self):
        """A one-track batch ends at its first completion, so the throughput
        model never gets two samples and the footer says "calculating…" for the
        whole download. That is the right answer, not a gap: the one number
        available is the track's own remaining transfer time, which is a lower
        bound on the batch (post-processing and publish still follow) and would
        read as a confident under-estimate. The track card shows it instead."""
        a, clock = _agg(["only"])
        assert a.snapshot().eta_seconds is None
        a.update("only", downloaded_bytes=10, total_bytes=100, speed_bps=10.0)
        assert a.snapshot().eta_seconds is None      # no divide-by-zero either
        clock.advance(9.0)
        a.complete("only")
        assert a.snapshot().eta_seconds is None      # nothing left to wait for

    def test_paused_jobs_leave_the_outstanding_count(self):
        a, clock = _agg(_keys(10))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        before = a.snapshot().eta_seconds
        a.pause("k9")
        after = a.snapshot().eta_seconds
        assert after == pytest.approx(before - 10.0, abs=1e-6)

    def test_post_processing_tail_does_not_read_as_almost_done(self):
        """A track sits pinned at fraction 0.95 through ffmpeg, tagging and
        publish. That tail is inside the measured cycle, so the batch estimate
        must not shrink toward zero while it runs."""
        a, clock = _agg(_keys(10))
        for i in range(3):
            clock.advance(30.0)
            a.complete(f"k{i}")
        a.update("k2", fraction=0.95, speed_bps=0.0)
        clock.advance(20.0)          # 20s of opaque post-processing
        eta = a.snapshot().eta_seconds
        assert eta > 60.0

    def test_final_completion_clears_the_eta(self):
        a, clock = _agg(_keys(3))
        for i in range(3):
            clock.advance(4.0)
            a.complete(f"k{i}")
        snap = a.snapshot()
        assert snap.eta_seconds is None
        assert snap.completed == 3

    def test_reset_mints_a_new_batch_id_and_clears_history(self):
        a, clock = _agg(_keys(4))
        first_id = a.snapshot().batch_id
        for i in range(3):
            clock.advance(5.0)
            a.complete(f"k{i}")
        assert a.snapshot().eta_seconds is not None

        a.reset(_keys(4))
        snap = a.snapshot()
        assert snap.batch_id != first_id
        assert snap.eta_seconds is None

    def test_eta_is_never_negative_under_a_long_overrun(self):
        a, clock = _agg(_keys(3))
        for i in range(3):
            clock.advance(2.0)
            a.complete(f"k{i}")
        clock.advance(10_000.0)
        eta = a.snapshot().eta_seconds
        assert eta is None or eta >= 0.0


# ── Rate smoothing ───────────────────────────────────────────────────────────

class TestRateSmoothing:
    def test_a_rate_shift_does_not_land_in_one_step(self):
        a, clock = _agg(_keys(40), smoothing=0.3)
        for i in range(4):
            clock.advance(10.0)
            a.complete(f"k{i}")
        settled = a.snapshot().eta_seconds

        # A sudden burst of very fast completions.
        for i in range(4, 8):
            clock.advance(0.25)
            a.complete(f"k{i}")
        stepped = a.snapshot().eta_seconds
        instant_target = (40 - 8) * a._throughput_cycle_locked()
        # Moved toward the new reality, but not all the way in one snapshot.
        assert stepped < settled
        assert stepped > instant_target

    def test_estimate_may_rise_when_evidence_says_so(self):
        """Explicitly NOT monotonic: an honest upward revision must survive."""
        a, clock = _agg(_keys(40))
        for i in range(4):
            clock.advance(2.0)
            a.complete(f"k{i}")
        fast = a.snapshot().eta_seconds

        for i in range(4, 8):
            clock.advance(40.0)
            a.complete(f"k{i}")
        slow = a.snapshot().eta_seconds
        assert slow > fast

    def test_discrete_state_changes_apply_immediately(self):
        """Smoothing is on the measured rate, not the finished number, so a
        cancellation takes effect at once rather than bleeding in."""
        a, clock = _agg(_keys(10))
        for i in range(3):
            clock.advance(10.0)
            a.complete(f"k{i}")
        before = a.snapshot().eta_seconds
        a.cancel("k9")
        assert a.snapshot().eta_seconds == pytest.approx(before - 10.0, abs=1e-6)


# -- The estimate must never sit frozen --------------------------------------

class TestNoFrozenPlateau:
    """An earlier revision only degraded the rate once the pipeline had been
    silent for 3x the measured cycle. Between 1x and 3x the `- min(stall,
    cycle)` term had already saturated, so the value was pinned at exactly
    `(outstanding - 1) * cycle` - and the heartbeat republished that identical
    number twice a second, which reads as a broken footer.
    """

    @staticmethod
    def _warm(total=20, cycle=10.0, completions=4):
        a, clock = _agg(_keys(total))
        for i in range(completions):
            clock.advance(cycle)
            a.complete(f"k{i}")
        return a, clock

    def test_estimate_moves_at_one_and_a_half_cycles(self):
        a, clock = self._warm()
        clock.advance(15.0)                 # 1.5x cycle
        first = a.snapshot().eta_seconds
        clock.advance(0.5)                  # one heartbeat later
        assert a.snapshot().eta_seconds != pytest.approx(first, abs=1e-9)

    def test_estimate_moves_at_two_and_a_half_cycles(self):
        a, clock = self._warm()
        clock.advance(25.0)                 # 2.5x cycle
        first = a.snapshot().eta_seconds
        clock.advance(0.5)
        assert a.snapshot().eta_seconds != pytest.approx(first, abs=1e-9)

    def test_never_frozen_across_a_whole_quiet_period(self):
        """Sample at the heartbeat rate right through the old dead zone."""
        a, clock = self._warm()
        seen = []
        for _ in range(60):                 # 30s == 3x cycle, at 2 Hz
            clock.advance(0.5)
            seen.append(a.snapshot().eta_seconds)
        frozen = [
            i for i in range(1, len(seen))
            if seen[i] == pytest.approx(seen[i - 1], abs=1e-9)
        ]
        assert not frozen, (
            f"estimate held still at heartbeats {frozen} - values {seen}"
        )

    def test_overrun_raises_the_estimate(self):
        """Falling behind should read as more time left, not less."""
        a, clock = self._warm()
        clock.advance(10.0)                 # exactly on schedule
        on_time = a.snapshot().eta_seconds
        clock.advance(20.0)                 # badly overdue
        assert a.snapshot().eta_seconds > on_time

    def test_healthy_cycles_are_not_inflated(self):
        """Degradation must not touch a batch that is keeping pace: inside one
        cycle the raw measured span is used untouched."""
        a, clock = self._warm()
        clock.advance(5.0)                  # half a cycle in, on schedule
        # 16 outstanding x 10s, minus 5s elapsed into the current cycle.
        assert a.snapshot().eta_seconds == pytest.approx(155.0, abs=1e-6)


# -- Displayed speed ---------------------------------------------------------

class TestSpeedDecaysOnRead:
    """Speed is a function of elapsed time, but the mutators used to be the only
    things that advanced it - and a conservative-mode cooldown has no mutators
    for five to ten seconds. The footer held whatever the last completion left
    behind, reporting a healthy transfer rate while nothing was transferring.
    """

    @staticmethod
    def _mid_download():
        a, clock = _agg(["a", "b"], smoothing=0.3)
        a.update("a", downloaded_bytes=1_000, total_bytes=10_000_000,
                 speed_bps=1_000_000.0)
        clock.advance(1.0)
        a.update("a", downloaded_bytes=2_000, total_bytes=10_000_000,
                 speed_bps=1_000_000.0)
        return a, clock

    def test_speed_falls_while_nothing_transfers(self):
        a, clock = self._mid_download()
        a.complete("a")
        first = a.snapshot().speed_bps
        clock.advance(6.0)                  # two half-lives, no callbacks
        later = a.snapshot().speed_bps
        assert later < first / 3.0

    def test_speed_reaches_effectively_zero_over_a_long_idle(self):
        a, clock = self._mid_download()
        a.complete("a")
        clock.advance(30.0)                 # ten half-lives of pure silence
        assert a.snapshot().speed_bps < 1_000.0

    def test_repeated_reads_keep_advancing_the_decay(self):
        """Each heartbeat read must move it, not just the first."""
        a, clock = self._mid_download()
        a.complete("a")
        seen = []
        for _ in range(8):
            clock.advance(1.5)
            seen.append(a.snapshot().speed_bps)
        assert seen == sorted(seen, reverse=True)
        assert len(set(seen)) == len(seen)

    def test_reading_does_not_invent_speed_while_idle(self):
        a, clock = _agg(["a", "b"])
        clock.advance(10.0)
        assert a.snapshot().speed_bps == 0.0

    def test_active_transfer_is_unaffected(self):
        a, clock = self._mid_download()
        clock.advance(10.0)
        a.update("a", downloaded_bytes=9_000, total_bytes=10_000_000,
                 speed_bps=1_000_000.0)
        # Still transferring: the decay pulls toward the live rate, not zero.
        assert a.snapshot().speed_bps > 500_000.0
