"""
tests/test_track_phases.py  –  Per-track phase progress and remaining time
===========================================================================
A track card used to render the byte transfer and nothing else, which on a real
batch is roughly one second in every nine or ten. The card sat at 0% through the
Spotify match and the wait at the conservative YouTube gate, raced to 95% in
about a second, then sat at 95% through ffmpeg, tagging, MusicBrainz, ReplayGain
and publish. Two long frozen stretches with a blur in between, which users read
as a hung download.

These cover the model that fixes it (core.track_phases) and the orchestrator
wiring that feeds it.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.track_phases import (
    PHASE_ORDER,
    TrackPhase,
    TrackPhaseModel,
)


# ── The bar must never sit still ─────────────────────────────────────────────

class TestNoFrozenStretches:
    """The whole point: every stage advances, including the opaque ones."""

    @staticmethod
    def _trained():
        """A model that has seen one track: 1s match, 7s wait, 1s start,
        2s download, 3s processing."""
        m = TrackPhaseModel()
        for phase, secs in (
            (TrackPhase.MATCHING, 1.0),
            (TrackPhase.WAITING, 7.0),
            (TrackPhase.STARTING, 1.0),
            (TrackPhase.DOWNLOADING, 2.0),
            (TrackPhase.PROCESSING, 3.0),
        ):
            m.observe(phase, secs)
        m.note_track_measured()
        return m

    def test_matching_advances_even_though_it_reports_nothing(self):
        m = self._trained()
        seen = [m.position(TrackPhase.MATCHING, t / 10) for t in range(10)]
        assert seen == sorted(seen)
        assert seen[-1] > seen[0]

    def test_waiting_at_the_gate_advances(self):
        """The longest thing a track does under the conservative gate, and the
        one that previously showed nothing at all."""
        m = self._trained()
        early = m.position(TrackPhase.WAITING, 0.5)
        late = m.position(TrackPhase.WAITING, 6.0)
        assert late > early

    def test_post_processing_advances_instead_of_sitting_at_95(self):
        m = self._trained()
        start = m.position(TrackPhase.PROCESSING, 0.0)
        mid = m.position(TrackPhase.PROCESSING, 1.5)
        late = m.position(TrackPhase.PROCESSING, 2.9)
        assert start < mid < late

    def test_position_never_stalls_across_a_whole_track(self):
        """Walk the full lifecycle at 0.25s resolution and require the bar to
        keep moving through every stage boundary."""
        m = self._trained()
        weights = m.weights()
        seen = []
        for phase in PHASE_ORDER:
            w = weights[phase]
            steps = max(1, int(w / 0.25))
            for i in range(steps):
                byte_fraction = (i / steps) if phase == TrackPhase.DOWNLOADING else None
                seen.append(m.position(phase, i * 0.25, byte_fraction))
        assert seen == sorted(seen), "position went backwards"
        stalls = [i for i in range(1, len(seen)) if seen[i] == seen[i - 1]]
        assert not stalls, f"position held still at samples {stalls}"

    def test_an_overrunning_phase_still_leaves_room_to_finish(self):
        """A phase running far past its average must approach its end without
        claiming the track is complete - sitting at 100% while work continues
        is the same lie as sitting at 95%."""
        m = self._trained()
        pos = m.position(TrackPhase.PROCESSING, 300.0)
        assert pos < 1.0


# ── Weights are measured, not assumed ────────────────────────────────────────

class TestLearnedWeights:
    def test_weights_come_from_observations(self):
        m = TrackPhaseModel()
        m.observe(TrackPhase.WAITING, 8.0)
        m.observe(TrackPhase.WAITING, 12.0)
        assert m.weights()[TrackPhase.WAITING] == pytest.approx(10.0)

    def test_a_slow_batch_gives_the_download_more_of_the_bar(self):
        """The point of measuring: how much of the bar a phase owns should
        follow how long that phase actually takes on this batch.

        A batch of long files spends most of a track's life transferring, so
        the download should own most of the bar. A batch of short ones spends
        most of it matching, waiting and post-processing, so the transfer is a
        thin slice near the end - which is exactly why showing only the
        transfer made short-file batches look frozen."""
        quick, slow = TrackPhaseModel(), TrackPhaseModel()
        for m, dl in ((quick, 2.0), (slow, 60.0)):
            m.observe(TrackPhase.MATCHING, 1.0)
            m.observe(TrackPhase.WAITING, 7.0)
            m.observe(TrackPhase.STARTING, 1.0)
            m.observe(TrackPhase.DOWNLOADING, dl)
            m.observe(TrackPhase.PROCESSING, 3.0)
            m.note_track_measured()

        def download_share(m, weight):
            return (m.position(TrackPhase.DOWNLOADING, weight, 1.0)
                    - m.position(TrackPhase.DOWNLOADING, 0.0, 0.0))

        assert download_share(slow, 60.0) > download_share(quick, 2.0)
        # Short files: the transfer is a minority of the bar, and the stages
        # around it are the majority.
        assert download_share(quick, 2.0) < 0.25

    def test_reset_forgets_the_previous_batch(self):
        m = TrackPhaseModel()
        m.observe(TrackPhase.WAITING, 30.0)
        m.note_track_measured()
        m.reset()
        assert m.remaining(TrackPhase.WAITING, 0.0) is None


# ── Honesty about time ───────────────────────────────────────────────────────

class TestRemainingTime:
    def test_no_time_quoted_before_a_track_has_been_measured(self):
        """Same rule as the batch ETA: default weights shape a bar's pacing,
        but they must never be multiplied out into a quoted duration."""
        m = TrackPhaseModel()
        assert m.remaining(TrackPhase.DOWNLOADING, 0.0, 0.5) is None

    def test_time_is_quoted_once_a_track_has_finished(self):
        m = TestNoFrozenStretches._trained()
        assert m.remaining(TrackPhase.DOWNLOADING, 0.0, 0.0) is not None

    def test_remaining_covers_the_phases_still_to_come(self):
        """The complaint about yt-dlp's own number: it says "0:01" while ffmpeg,
        tagging and publish still have seconds to run."""
        m = TestNoFrozenStretches._trained()
        # Bytes are done, but processing (3s) has not started.
        left = m.remaining(TrackPhase.DOWNLOADING, 2.0, 1.0)
        assert left == pytest.approx(3.0, abs=1e-6)

    def test_remaining_falls_as_the_track_proceeds(self):
        m = TestNoFrozenStretches._trained()
        seen = [
            m.remaining(TrackPhase.MATCHING, 0.0),
            m.remaining(TrackPhase.WAITING, 3.0),
            m.remaining(TrackPhase.DOWNLOADING, 1.0, 0.5),
            m.remaining(TrackPhase.PROCESSING, 1.0),
        ]
        assert seen == sorted(seen, reverse=True)

    def test_a_queued_track_is_quoted_a_whole_track(self):
        m = TestNoFrozenStretches._trained()
        assert m.remaining(TrackPhase.QUEUED, 0.0) == pytest.approx(14.0, abs=1e-6)

    def test_remaining_never_negative(self):
        m = TestNoFrozenStretches._trained()
        assert m.remaining(TrackPhase.PROCESSING, 10_000.0) >= 0.0


# ── Orchestrator wiring ──────────────────────────────────────────────────────

class _PhaseEngine:
    """Engine that walks a job through a real-shaped lifecycle: a silent
    stretch, then bytes, then the 0.95-pinned processing tail."""

    def __init__(self, start_delay=0.15, bytes_delay=0.1, tail_delay=0.2):
        self._cancel_event = threading.Event()
        self._start, self._bytes, self._tail = start_delay, bytes_delay, tail_delay

    def cancel_all(self):
        self._cancel_event.set()

    def download(self, req):
        from core.downloader import DownloadProgress, DownloadStatus
        time.sleep(self._start)                       # extract_info, no ticks
        for i in (1, 2, 3):
            if req.on_progress:
                req.on_progress(DownloadProgress(
                    status=DownloadStatus.DOWNLOADING, url=req.url,
                    downloaded_bytes=i * 1000, total_bytes=3000,
                    fraction=i / 3, speed_bps=1000.0,
                ))
            time.sleep(self._bytes)
        if req.on_progress:                            # the pinned tail
            req.on_progress(DownloadProgress(
                status=DownloadStatus.PROCESSING, url=req.url,
                downloaded_bytes=3000, total_bytes=3000, fraction=0.95,
            ))
        time.sleep(self._tail)
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED, url=req.url,
                output_path="/tmp/out.mp3", fraction=1.0,
            ))


class _PhaseCallbacks:
    def __init__(self):
        self.phases: list = []
        self.positions: list = []
        self.statuses: list = []

    def on_track_progress(self, key, fraction):
        self.positions.append(fraction)

    def on_track_phase(self, key, phase, remaining_seconds):
        self.phases.append(phase)

    def on_track_status(self, key, status):
        self.statuses.append(status)

    def on_track_speed(self, key, speed_bps, eta_seconds): pass
    def on_track_finished(self, key, path): pass
    def on_track_preexisting(self, key, path): pass
    def on_track_error(self, key, error): pass
    def on_overall_progress(self, fraction): pass
    def on_metrics(self, speed, eta): pass
    def on_batch_snapshot(self, snapshot): pass
    def on_job_count_changed(self, completed, total): pass
    def on_track_thumbnail(self, key, url): pass
    def on_status_message(self, msg): pass
    def on_batch_finished(self, outcome=None): pass


def _run(engine, cb, n=1):
    from core.download_orchestrator import DownloadOrchestrator
    from core.downloader import DownloadRequest, MediaType
    jobs = [
        (f"k{i}", DownloadRequest(
            url=f"https://example.com/{i}", output_dir="/tmp",
            media_type=MediaType.AUDIO, forced_title=f"k{i}",
        ))
        for i in range(n)
    ]
    orch = DownloadOrchestrator(engine=engine, callbacks=cb, db=None, max_workers=1)
    orch.run_batch(jobs)
    return orch


class TestOrchestratorEmitsPhases:
    def test_starting_and_processing_both_reach_the_ui(self):
        cb = _PhaseCallbacks()
        _run(_PhaseEngine(), cb)
        assert "starting" in cb.phases
        assert "processing" in cb.phases, (
            "the post-processing tail is still invisible - this is the stretch "
            "that used to sit pinned at 95%"
        )

    def test_phases_arrive_in_lifecycle_order(self):
        cb = _PhaseCallbacks()
        _run(_PhaseEngine(), cb)
        first_seen = []
        for p in cb.phases:
            if p not in first_seen:
                first_seen.append(p)
        expected = [p for p in ("starting", "downloading", "processing")
                    if p in first_seen]
        assert first_seen == expected

    def test_the_card_sees_movement_before_any_byte_arrives(self):
        """The opening stretch used to be a dead bar at zero."""
        cb = _PhaseCallbacks()
        _run(_PhaseEngine(start_delay=0.6), cb)
        assert cb.positions, "no progress reached the card at all"

    def test_card_position_is_not_the_raw_byte_fraction(self):
        """The engine pins its own fraction at 0.95 for the whole tail. The
        card must not inherit that - it is a phase, not a position."""
        cb = _PhaseCallbacks()
        _run(_PhaseEngine(), cb)
        assert cb.positions
        assert not all(p in (0.0, 1.0 / 3, 2.0 / 3, 1.0, 0.95)
                       for p in cb.positions)

    def test_a_second_track_is_measured_from_the_first(self):
        cb = _PhaseCallbacks()
        orch = _run(_PhaseEngine(), cb, n=2)
        weights = orch._phase_model.weights()
        assert weights[TrackPhase.PROCESSING] > 0.0
        assert weights[TrackPhase.DOWNLOADING] > 0.0
