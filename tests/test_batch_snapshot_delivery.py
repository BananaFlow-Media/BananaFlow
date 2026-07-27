"""
tests/test_batch_snapshot_delivery.py  –  Getting snapshots to the footer
==========================================================================
Two defects live in the delivery path rather than in the ETA maths:

* **The footer froze whenever no bytes were moving.** Snapshots were emitted
  only from inside a track's yt-dlp progress hook, so the conservative
  cooldown, Spotify match resolution, the wait on the serial gate and the whole
  post-processing tail all passed with the footer showing a stale number. On a
  conservative batch that is most of the wall clock. A heartbeat spanning the
  entire batch fixes it for every phase at once — including the staggered
  submit loop, which with a large lazy batch runs for minutes before the
  completion drain is even reached.

* **A single-track resume could have repainted the whole-batch footer.** It
  runs its own orchestrator with its own 1-job aggregator, and the controller's
  sender check has to stay permissive (``sender() is None`` for direct calls, a
  resume worker accepted while ``_dl_worker is None``). Nothing structural
  stopped a "0 of 1" snapshot reaching the footer — only the convention that
  resume_track() does not connect the signal. Snapshots now carry the id of the
  batch they describe.

Headless; the orchestrator half uses the FakeEngine pattern from
tests/test_orchestrator.py.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.batch_progress import BatchProgressAggregator


# ──────────────────────────────────────────────────────────────────────────────
# Heartbeat: snapshots keep arriving when no bytes are moving
# ──────────────────────────────────────────────────────────────────────────────

class _SlowEngine:
    """Engine whose download() blocks, so the batch spends its time in phases
    that emit no progress ticks at all — exactly the stretches the footer used
    to freeze through."""

    def __init__(self, hold_s: float) -> None:
        self._cancel_event = threading.Event()
        self._hold = hold_s

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req) -> None:
        from core.downloader import DownloadProgress, DownloadStatus
        # No on_progress calls whatsoever: no bytes, no ticks, nothing.
        time.sleep(self._hold)
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=req.url,
                output_path="/tmp/out.mp3",
                fraction=1.0,
            ))


class _TickingEngine:
    """Engine that reports progress rapidly, the way yt-dlp does. Used to show
    that the batch-snapshot rate does not ride on the number of jobs doing it."""

    def __init__(self, hold_s: float) -> None:
        self._cancel_event = threading.Event()
        self._hold = hold_s

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req) -> None:
        from core.downloader import DownloadProgress, DownloadStatus
        deadline = time.monotonic() + self._hold
        done = 0
        while time.monotonic() < deadline:
            done += 1000
            if req.on_progress:
                req.on_progress(DownloadProgress(
                    status=DownloadStatus.DOWNLOADING,
                    url=req.url,
                    downloaded_bytes=done,
                    total_bytes=10_000_000,
                    fraction=min(done / 10_000_000, 1.0),
                    speed_bps=1000.0,
                ))
            time.sleep(0.01)
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=req.url,
                output_path="/tmp/out.mp3",
                fraction=1.0,
            ))


class _RecordingCallbacks:
    def __init__(self) -> None:
        self.snapshots: list = []
        self.snapshot_times: list[float] = []

    def on_track_progress(self, key, fraction): pass
    def on_track_speed(self, key, speed_bps, eta_seconds): pass
    def on_track_status(self, key, status): pass
    def on_track_finished(self, key, path): pass
    def on_track_preexisting(self, key, path): pass
    def on_track_error(self, key, error): pass
    def on_overall_progress(self, fraction): pass
    def on_metrics(self, speed, eta): pass
    def on_batch_snapshot(self, snapshot):
        self.snapshots.append(snapshot)
        self.snapshot_times.append(time.monotonic())
    def on_job_count_changed(self, completed, total): pass
    def on_track_thumbnail(self, key, url): pass
    def on_status_message(self, msg): pass
    def on_batch_finished(self, outcome=None): pass


def _jobs(n):
    from core.downloader import DownloadRequest, MediaType
    return [
        (f"k{i}", DownloadRequest(
            url=f"https://example.com/{i}",
            output_dir="/tmp",
            media_type=MediaType.AUDIO,
            forced_title=f"k{i}",
        ))
        for i in range(n)
    ]


class TestHeartbeatCoversEveryPhase:
    def test_snapshots_arrive_while_no_bytes_are_moving(self):
        from core.download_orchestrator import DownloadOrchestrator

        engine = _SlowEngine(hold_s=1.5)
        cb = _RecordingCallbacks()
        orch = DownloadOrchestrator(
            engine=engine, callbacks=cb, db=None, max_workers=1,
        )
        orch.run_batch(_jobs(1))

        # 1.5s of a download that never reported a single progress tick. With
        # snapshots driven only by the progress hook this would be ~1.
        assert len(cb.snapshots) >= 3, (
            f"only {len(cb.snapshots)} snapshots over a 1.5s tick-free download"
        )

    def test_snapshots_arrive_during_the_staggered_submit_loop(self):
        """The submit loop runs before the completion drain exists. A heartbeat
        attached to the drain would leave the batch opening unattended."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _SlowEngine(hold_s=0.05)
        cb = _RecordingCallbacks()
        orch = DownloadOrchestrator(
            engine=engine, callbacks=cb, db=None, max_workers=1,
        )
        start = time.monotonic()
        # ~1.2s of staggering before the completion drain is reached.
        orch.run_batch(_jobs(4), delay_range=(0.4, 0.4))
        assert time.monotonic() - start > 1.0      # the stagger really happened
        assert len(cb.snapshots) >= 3

    def test_heartbeat_thread_does_not_outlive_the_batch(self):
        from core.download_orchestrator import DownloadOrchestrator

        engine = _SlowEngine(hold_s=0.05)
        cb = _RecordingCallbacks()
        orch = DownloadOrchestrator(
            engine=engine, callbacks=cb, db=None, max_workers=1,
        )
        orch.run_batch(_jobs(2))
        time.sleep(0.3)
        assert not any(
            t.name == "dl-heartbeat" and t.is_alive()
            for t in threading.enumerate()
        )


class TestSnapshotRateIsIndependentOfWorkerCount:
    """The batch-snapshot rate must be a property of the batch, not of how many
    jobs happen to be running.

    The throttle this replaces lived inside the per-job progress hook, so three
    parallel downloads ran three independent throttles and the combined rate
    scaled with the worker count. Measured behaviourally: reading the source
    cannot show that two throttles are actually one.
    """

    @staticmethod
    def _rate(workers, jobs, hold_s):
        from core.download_orchestrator import DownloadOrchestrator
        engine = _TickingEngine(hold_s=hold_s)
        cb = _RecordingCallbacks()
        orch = DownloadOrchestrator(
            engine=engine, callbacks=cb, db=None, max_workers=workers,
        )
        start = time.monotonic()
        orch.run_batch(_jobs(jobs))
        elapsed = time.monotonic() - start
        # Snapshots emitted by discrete transitions (a job finishing) are not
        # part of the periodic stream, so discount one per job.
        periodic = max(0, len(cb.snapshots) - jobs)
        return periodic / max(elapsed, 1e-6)

    def test_three_workers_do_not_triple_the_snapshot_rate(self):
        one = self._rate(workers=1, jobs=1, hold_s=2.0)
        three = self._rate(workers=3, jobs=3, hold_s=2.0)
        # Same wall time, 3x the concurrent progress hooks. Allow generous
        # slack for scheduling noise; the old per-job throttle produced ~3x.
        assert three < one * 1.8, (
            f"{three:.1f} snapshots/s with 3 workers vs {one:.1f} with 1 — "
            "the rate is still scaling with the worker count"
        )

    def test_rate_is_close_to_the_heartbeat_interval(self):
        from core import download_orchestrator as mod
        observed = self._rate(workers=3, jobs=3, hold_s=2.0)
        expected = 1.0 / mod._HEARTBEAT_INTERVAL
        assert 0.5 * expected <= observed <= 1.8 * expected, (
            f"{observed:.1f} snapshots/s vs an expected ~{expected:.1f}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# batch_id: only the live batch may repaint the footer
# ──────────────────────────────────────────────────────────────────────────────

class TestBatchIdentity:
    def test_every_snapshot_carries_its_batch_id(self):
        a = BatchProgressAggregator()
        a.reset(["x"])
        assert a.snapshot().batch_id == a.batch_id
        assert a.snapshot().batch_id != ""

    def test_two_aggregators_have_different_ids(self):
        a, b = BatchProgressAggregator(), BatchProgressAggregator()
        a.reset(["x"])
        b.reset(["y"])
        assert a.snapshot().batch_id != b.snapshot().batch_id

    def test_reset_mints_a_new_id(self):
        a = BatchProgressAggregator()
        a.reset(["x"])
        first = a.snapshot().batch_id
        a.reset(["x"])
        assert a.snapshot().batch_id != first

    def test_caller_supplied_id_is_used_verbatim(self):
        """The UI mints the id before the worker exists and passes it down, so
        it knows which batch it is showing before any snapshot can arrive."""
        a = BatchProgressAggregator()
        a.reset(["x"], batch_id="chosen-by-the-caller")
        assert a.snapshot().batch_id == "chosen-by-the-caller"


class _StubController:
    """The controller's snapshot gate, isolated from Qt.

    Reuses the real method so the guard under test is production code, with
    only its two collaborators stubbed: the sender check (always permissive
    here, which is the pessimistic case) and the outbound signal.
    """

    def __init__(self) -> None:
        self._batch_snapshot_id = None
        self.forwarded: list = []

        class _Sig:
            def __init__(self, sink): self._sink = sink
            def emit(self, snapshot): self._sink.append(snapshot)

        self.batch_snapshot = _Sig(self.forwarded)

    def _is_current_batch_worker_signal(self):
        return True          # sender() is None — the permissive path

    def start_batch(self, batch_id):
        """Stand-in for _build_batch_worker: mint the id, then start."""
        self._batch_snapshot_id = batch_id

    def deliver(self, snapshot):
        from ui.controllers.download_controller import DownloadController
        DownloadController._on_worker_batch_snapshot(self, snapshot)


def _snap_for(batch_id, keys=("a",)):
    a = BatchProgressAggregator()
    a.reset(list(keys), batch_id=batch_id)
    return a.snapshot()


class TestFooterAcceptsOnlyTheBoundBatch:
    """The identity is chosen by the controller in advance, never learned from
    traffic. An id adopted from "whichever snapshot arrived first" can be
    captured by a stale or foreign one, which would then reject every genuine
    snapshot for the rest of the batch."""

    def test_snapshots_from_the_bound_batch_are_forwarded(self):
        c = _StubController()
        c.start_batch("batch-1")
        snap = _snap_for("batch-1")
        c.deliver(snap)
        assert c.forwarded == [snap]

    def test_a_foreign_snapshot_arriving_first_is_rejected(self):
        """The trust-on-first-use failure mode, pinned."""
        c = _StubController()
        c.start_batch("batch-1")
        foreign = _snap_for("some-other-batch", keys=["z"])
        c.deliver(foreign)
        assert c.forwarded == []
        assert c._batch_snapshot_id == "batch-1"    # not captured

    def test_the_real_snapshot_still_arrives_after_a_foreign_one(self):
        """The consequence that matters: a foreign snapshot must not poison the
        binding and lock the footer out for the rest of the batch."""
        c = _StubController()
        c.start_batch("batch-1")
        c.deliver(_snap_for("some-other-batch", keys=["z"]))
        real = _snap_for("batch-1", keys=["a", "b"])
        c.deliver(real)
        assert c.forwarded == [real]

    def test_a_resume_snapshot_with_no_batch_established_is_rejected(self):
        """No live batch means no identity, and nothing may repaint the footer.
        A single-track resume runs its own orchestrator with its own 1-job
        aggregator; its "0 of 1" must never reach a whole-batch footer."""
        c = _StubController()
        assert c._batch_snapshot_id is None
        resume = _snap_for(None, keys=["one-track"])
        assert resume.total == 1
        c.deliver(resume)
        assert c.forwarded == []

    def test_a_single_track_resume_during_a_live_batch_is_rejected(self):
        c = _StubController()
        c.start_batch("batch-1")
        batch = _snap_for("batch-1", keys=[f"k{i}" for i in range(50)])
        c.deliver(batch)
        c.deliver(_snap_for(None, keys=["one-track"]))   # resume worker
        assert c.forwarded == [batch]
        assert all(s.total == 50 for s in c.forwarded)

    def test_a_new_batch_rebinds(self):
        c = _StubController()
        c.start_batch("batch-1")
        first = _snap_for("batch-1")
        c.deliver(first)

        c.start_batch("batch-2")
        stale = _snap_for("batch-1")            # in-flight from the old batch
        c.deliver(stale)
        second = _snap_for("batch-2", keys=["b", "c"])
        c.deliver(second)

        assert c.forwarded == [first, second]

    def test_an_unstamped_snapshot_is_rejected(self):
        """A snapshot with no id cannot prove it belongs to the live batch."""
        import dataclasses
        c = _StubController()
        c.start_batch("batch-1")
        c.deliver(dataclasses.replace(_snap_for("batch-1"), batch_id=""))
        assert c.forwarded == []


class TestControllerBindsTheIdItMints:
    """Wiring check: the id the controller stores is the one it hands the
    worker, so the two can never drift apart."""

    def test_build_batch_worker_passes_its_id_to_the_worker(self, monkeypatch):
        import ui.workers.download_worker as dw_mod
        from ui.controllers.download_controller import DownloadController

        captured = {}

        class _FakeWorker:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                for name in (
                    "track_progress", "track_first_byte", "track_speed", "track_status", "track_phase",
                    "track_finished", "track_preexisting", "overall_progress",
                    "metrics", "batch_snapshot", "job_count_changed",
                    "job_error", "all_finished", "track_thumbnail",
                ):
                    setattr(self, name, type("S", (), {"connect": lambda *a: None})())

        monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeWorker)

        ctrl = DownloadController.__new__(DownloadController)
        ctrl._batch_snapshot_id = None
        ctrl._engine = None
        ctrl._db = None
        ctrl._cfg = type("C", (), {"max_parallel_downloads": 3})()
        for name in (
            "_on_track_progress", "_on_track_first_byte", "_on_track_speed", "_on_track_status",
            "_on_track_finished", "_on_track_preexisting",
            "_on_worker_overall_progress", "_on_worker_metrics",
            "_on_worker_batch_snapshot", "_on_worker_job_count_changed",
            "_on_track_error", "_on_batch_done", "_on_track_thumbnail",
        ):
            setattr(ctrl, name, lambda *a, **k: None)

        DownloadController._build_batch_worker(ctrl, jobs=[], preexisting_jobs=[])

        assert ctrl._batch_snapshot_id
        assert captured["batch_id"] == ctrl._batch_snapshot_id

    def test_weighted_track_progress_cannot_be_logged_as_a_real_byte(self, monkeypatch):
        import ui.controllers.download_controller as controller_mod
        from ui.controllers.download_controller import DownloadController

        ctrl = DownloadController.__new__(DownloadController)
        ctrl._is_active_worker_signal = lambda: True
        ctrl._first_byte_logged = False
        ctrl._batch_click_ts = 10.0
        ctrl._card_progress = {}
        ctrl._key_to_card = {}
        monkeypatch.setattr(controller_mod.time, "monotonic", lambda: 12.0)

        DownloadController._on_track_progress(ctrl, "k0", 0.2)
        assert not ctrl._first_byte_logged

        DownloadController._on_track_first_byte(ctrl, "k0")
        assert ctrl._first_byte_logged

    def test_weighted_track_progress_does_not_change_the_card_phase(self):
        from ui.controllers.download_controller import DownloadController

        class Card:
            _status = "matching"

            def __init__(self):
                self.progress: list[float] = []
                self.statuses: list[str] = []

            def set_progress(self, fraction):
                self.progress.append(fraction)

            def set_status(self, status):
                self.statuses.append(status)

        card = Card()
        ctrl = DownloadController.__new__(DownloadController)
        ctrl._is_active_worker_signal = lambda: True
        ctrl._card_progress = {}
        ctrl._key_to_card = {"k0": card}

        DownloadController._on_track_progress(ctrl, "k0", 0.2)

        assert card.progress == [0.2]
        assert card.statuses == []


# ------------------------------------------------------------------------------
# An unhandled failure must terminate the job everywhere, not just in a counter
# ------------------------------------------------------------------------------

class TestUnhandledExceptionCoherence:
    """run_batch's future-result `except Exception` branch incremented the
    scalar `_failed` counter and told the card, but never told the aggregator.

    The job therefore stayed QUEUED or ACTIVE forever: missing from
    BatchSnapshot.failed, still counted as outstanding work by the ETA (so the
    batch could never finish counting down), and still weighed as unfinished by
    the progress bar. Driven through the real orchestrator by making
    _download_one raise, rather than by calling aggregator.fail() directly -
    the point is that this call path reaches it.
    """

    @staticmethod
    def _run_with_raising_job(monkeypatch, n_jobs=3, raising_key="k1"):
        from core.download_orchestrator import DownloadOrchestrator

        engine = _SlowEngine(hold_s=0.01)
        cb = _RecordingCallbacks()
        orch = DownloadOrchestrator(
            engine=engine, callbacks=cb, db=None, max_workers=1,
        )
        real = DownloadOrchestrator._download_one

        def _maybe_raise(self, key, req):
            if key == raising_key:
                raise RuntimeError("simulated unexpected failure")
            return real(self, key, req)

        monkeypatch.setattr(DownloadOrchestrator, "_download_one", _maybe_raise)
        result = orch.run_batch(_jobs(n_jobs))
        return orch, cb, result

    def test_job_reaches_the_failed_state_in_the_snapshot(self, monkeypatch):
        orch, cb, result = self._run_with_raising_job(monkeypatch)
        final = cb.snapshots[-1]
        assert final.failed == 1, (
            f"snapshot reports {final.failed} failed; counters say {result.failed}"
        )

    def test_no_job_is_left_outstanding(self, monkeypatch):
        orch, cb, result = self._run_with_raising_job(monkeypatch)
        final = cb.snapshots[-1]
        assert final.queued == 0
        assert final.active == 0
        assert final.finished == final.total

    def test_the_eta_does_not_keep_counting_a_dead_job(self, monkeypatch):
        orch, cb, result = self._run_with_raising_job(monkeypatch)
        assert cb.snapshots[-1].eta_seconds is None

    def test_scalar_counters_and_snapshot_agree(self, monkeypatch):
        orch, cb, result = self._run_with_raising_job(monkeypatch)
        final = cb.snapshots[-1]
        assert result.failed == final.failed
        assert result.completed == final.completed
        assert result.total == final.total

    def test_a_setup_style_failure_does_not_become_a_throughput_sample(self, monkeypatch):
        """The failure above never ran the pipeline in the aggregator's eyes
        only if it was never submitted. This one WAS submitted, so it is
        legitimate evidence - pinned so the submitted/unsubmitted distinction
        stays deliberate rather than accidental."""
        orch, cb, result = self._run_with_raising_job(monkeypatch, n_jobs=3)
        # Two normal completions plus one submitted failure = three cycles.
        assert cb.snapshots[-1].failed == 1
        assert cb.snapshots[-1].completed == 2
