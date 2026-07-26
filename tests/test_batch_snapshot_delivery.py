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


class TestSnapshotEmitThrottle:
    def test_throttle_is_time_based_not_per_job_tick_count(self):
        """The old throttle was a per-job counter, so N parallel jobs emitted N
        times as often. Pin that the constant is a duration in seconds."""
        from core import download_orchestrator as mod
        assert isinstance(mod._HEARTBEAT_INTERVAL, float)
        assert 0.1 <= mod._HEARTBEAT_INTERVAL <= 1.0

    def test_progress_hook_no_longer_uses_a_modulo_counter(self):
        import inspect
        from core.download_orchestrator import DownloadOrchestrator
        src = inspect.getsource(DownloadOrchestrator._download_one_locked)
        assert "% 10" not in src
        assert "_HEARTBEAT_INTERVAL" in src


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

    def new_batch(self):
        self._batch_snapshot_id = None

    def deliver(self, snapshot):
        from ui.controllers.download_controller import DownloadController
        DownloadController._on_worker_batch_snapshot(self, snapshot)


class TestFooterRejectsForeignSnapshots:
    @staticmethod
    def _snap(keys):
        a = BatchProgressAggregator()
        a.reset(keys)
        return a.snapshot()

    def test_first_snapshot_establishes_the_live_batch(self):
        c = _StubController()
        snap = self._snap(["a", "b"])
        c.deliver(snap)
        assert c.forwarded == [snap]
        assert c._batch_snapshot_id == snap.batch_id

    def test_further_snapshots_from_the_same_batch_are_forwarded(self):
        c = _StubController()
        a = BatchProgressAggregator()
        a.reset(["a", "b"])
        c.deliver(a.snapshot())
        a.complete("a")
        c.deliver(a.snapshot())
        assert len(c.forwarded) == 2

    def test_a_single_track_resume_snapshot_is_rejected(self):
        """The concrete failure this guards: a 1-job resume aggregator would
        otherwise repaint the whole-batch footer as "0 of 1"."""
        c = _StubController()
        batch = self._snap([f"k{i}" for i in range(50)])
        c.deliver(batch)

        resume = self._snap(["one-track"])
        assert resume.total == 1
        c.deliver(resume)

        assert c.forwarded == [batch]
        assert all(s.total == 50 for s in c.forwarded)

    def test_direct_call_with_no_sender_is_still_accepted(self):
        """The sender check must stay permissive for direct programmatic and
        test calls; batch_id is what does the real filtering."""
        c = _StubController()
        snap = self._snap(["a"])
        c.deliver(snap)              # sender() is None throughout
        assert c.forwarded == [snap]

    def test_a_new_batch_adopts_the_new_id(self):
        c = _StubController()
        first = self._snap(["a"])
        c.deliver(first)
        c.new_batch()                # _build_batch_worker clears the latch
        second = self._snap(["b", "c"])
        c.deliver(second)
        assert c.forwarded == [first, second]

    def test_snapshot_without_a_batch_id_still_works(self):
        """batch_id is defaulted, so a hand-built BatchSnapshot in some other
        test does not silently stop reaching the footer."""
        import dataclasses
        c = _StubController()
        snap = dataclasses.replace(self._snap(["a"]), batch_id="")
        c.deliver(snap)
        assert c.forwarded == [snap]
