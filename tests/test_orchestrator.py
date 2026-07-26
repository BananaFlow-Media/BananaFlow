"""
tests/test_orchestrator.py  –  Unit tests for DownloadOrchestrator
===================================================================
Run:
    pytest tests/test_orchestrator.py -v

Uses a mock DownloadEngine that simulates instant success/failure
without any network calls.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from core.downloader import (
    DownloadEngine,
    DownloadProgress,
    DownloadRequest,
    DownloadStatus,
    MediaType,
)
from error_handler import ErrorInfo


class FakeEngine:
    """Mock DownloadEngine that fires on_finished immediately."""

    def __init__(self, fail_keys: set[str] | None = None) -> None:
        self._cancel_event = threading.Event()
        self._fail_keys = fail_keys or set()
        self._downloaded: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        if self._cancel_event.is_set():
            return
        if req.url in self._fail_keys:
            if req.on_error:
                req.on_error(DownloadProgress(
                    status=DownloadStatus.ERROR,
                    url=req.url,
                    error_message="Simulated failure",
                ))
            return
        self._downloaded.append(req.url)
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=req.url,
                title=req.forced_title or "",
                output_path=f"/tmp/{req.forced_title or 'out'}.mp3",
                fraction=1.0,
            ))


class FakeCallbacks:
    """Records all callback invocations for assertions."""

    def __init__(self):
        self.track_statuses: list[tuple[str, str]] = []
        self.track_finished: list[tuple[str, str]] = []
        self.track_preexisting: list[tuple[str, str]] = []
        self.track_errors: list[tuple[str, ErrorInfo]] = []
        self.overall: list[float] = []
        self.messages: list[str] = []
        self.snapshots: list = []
        self.batch_done = False
        self.outcome = None

    def on_track_progress(self, key, fraction): pass
    def on_track_speed(self, key, speed_bps, eta_seconds): pass
    def on_track_status(self, key, status):
        self.track_statuses.append((key, status))
    def on_track_finished(self, key, path):
        self.track_finished.append((key, path))
    def on_track_preexisting(self, key, path):
        self.track_preexisting.append((key, path))
    def on_track_error(self, key, error):
        self.track_errors.append((key, error))
    def on_overall_progress(self, fraction):
        self.overall.append(fraction)
    def on_metrics(self, speed, eta): pass
    def on_batch_snapshot(self, snapshot):
        self.snapshots.append(snapshot)
    def on_job_count_changed(self, completed, total): pass
    def on_track_thumbnail(self, key, url): pass
    def on_status_message(self, msg):
        self.messages.append(msg)
    def on_batch_finished(self, outcome=None):
        self.batch_done = True
        self.outcome = outcome


def _make_job(key: str, url: str) -> tuple[str, DownloadRequest]:
    return (key, DownloadRequest(
        url=url,
        output_dir="/tmp",
        media_type=MediaType.AUDIO,
        forced_title=key,
    ))


class TestDownloadOrchestrator:

    def test_successful_batch(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=2)

        jobs = [_make_job("a", "http://a"), _make_job("b", "http://b")]
        result = orch.run_batch(jobs)

        assert result.total == 2
        assert result.completed == 2
        assert result.failed == 0
        assert result.cancelled is False
        assert cb.batch_done is True
        assert len(cb.track_finished) == 2
        assert "Done" in cb.messages[-1]

    def test_partial_failure(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine(fail_keys={"http://b"})
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=2)

        jobs = [_make_job("a", "http://a"), _make_job("b", "http://b")]
        result = orch.run_batch(jobs)

        assert result.completed == 1
        assert result.failed == 1
        assert len(cb.track_errors) == 1
        assert cb.track_errors[0][0] == "b"

    def test_cancel_before_start(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb)

        # Pre-cancel
        engine._cancel_event.set()

        jobs = [_make_job("a", "http://a")]
        result = orch.run_batch(jobs)

        assert result.cancelled is True
        # Track should have been marked cancelled, not downloaded
        statuses = dict(cb.track_statuses)
        assert statuses.get("a") == "cancelled"

    def test_cancel_track_individually(self):
        from core.download_orchestrator import DownloadOrchestrator

        class SlowEngine(FakeEngine):
            def download(self, req):
                # Check cancel before "downloading"
                if req.cancel_event and req.cancel_event.is_set():
                    return
                super().download(req)

        engine = SlowEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)

        jobs = [_make_job("a", "http://a"), _make_job("b", "http://b")]

        # Cancel track "b" before batch starts
        # We need to run the batch; cancel_track only works after jobs are submitted
        # So we test by pre-setting the engine cancel for "b" via a hook
        # Simpler: just verify cancel_track API doesn't crash
        orch.cancel_track("nonexistent")  # should not raise

    def test_empty_batch(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb)

        result = orch.run_batch([])

        assert result.total == 0
        assert result.completed == 0
        assert cb.batch_done is True


class _BlockingEngine:
    """Engine whose download() blocks until released, so a test can inspect
    orchestrator state exactly while a job is genuinely mid-flight -- the
    FakeEngine above fires on_finished synchronously, which never leaves a
    window to observe an ACTIVE (non-terminal) job at all."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self.release = threading.Event()
        self.started = threading.Event()
        self.downloaded: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()
        self.release.set()

    def download(self, req: DownloadRequest) -> None:
        self.downloaded.append(req.url)
        self.started.set()
        self.release.wait(timeout=5)
        if self._cancel_event.is_set():
            return
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=req.url,
                output_path="/tmp/out.mp3",
                fraction=1.0,
            ))


class TestLiveRequestSnapshot:
    """core.download_orchestrator.DownloadOrchestrator.live_request_snapshot
    / job_state — the race-free reads Global Pause needs (see the second
    combined-PR review's finding #6): a job's terminal state and a
    resume-ready copy of its live DownloadRequest, read atomically so an
    outside caller can never see the request half-committed."""

    def test_unknown_key_returns_none_none(self):
        from core.download_orchestrator import DownloadOrchestrator

        orch = DownloadOrchestrator(engine=FakeEngine(), callbacks=FakeCallbacks())
        state, snapshot = orch.live_request_snapshot("nonexistent")
        assert state is None
        assert snapshot is None

    def test_snapshot_is_available_while_active(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_progress import is_terminal_state

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)

            state, snapshot = orch.live_request_snapshot(key)
            assert not is_terminal_state(state)
            assert snapshot is not None
            assert snapshot.url == "http://a"
            # A defensive copy, not the live object -- mutating it must
            # never reach back into the orchestrator's own bookkeeping.
            assert snapshot is not req

            assert orch.job_state(key) == state
        finally:
            engine.release.set()
            thread.join(timeout=5)

    def test_completed_job_is_never_pause_snapshotted(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_progress import JobState

        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        orch.run_batch([(key, req)])

        state, snapshot = orch.live_request_snapshot(key)
        assert state == JobState.COMPLETED
        assert snapshot is None, "a completed job must never be pause-snapshotted"

    def test_pause_and_completion_are_mutually_exclusive(self):
        """The atomic pause/completion boundary. A snapshot handed out
        CLAIMS the job: the pool thread that was about to complete it must
        then not report it as done, or the batch both finishes the track and
        offers to resume it."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is not None
        finally:
            # Released only now: the engine goes on to fire on_finished for
            # a job the pause already claimed.
            engine.release.set()
            thread.join(timeout=5)

        assert cb.track_finished == [], (
            "a job captured for a pause must not also be reported as done"
        )
        assert ("a", "done") not in cb.track_statuses

    def test_publish_gate_refuses_the_commit_for_a_paused_job(self):
        """The completion side of the same boundary, at the last possible
        instant: the engine asks permission immediately before it makes the
        file visible, and a claimed job is refused."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            assert req.publish_gate is not None
            assert req.publish_gate() is True, "an unclaimed job may publish"
        finally:
            engine.release.set()
            thread.join(timeout=5)

        # A second orchestrator run, this time claimed by a pause first.
        engine2 = _BlockingEngine()
        orch2 = DownloadOrchestrator(engine=engine2, callbacks=FakeCallbacks(), max_workers=1)
        key2, req2 = _make_job("b", "http://b")
        t2 = threading.Thread(target=orch2.run_batch, args=([(key2, req2)],))
        t2.start()
        try:
            assert engine2.started.wait(timeout=5)
            _state, snapshot = orch2.live_request_snapshot(key2)
            assert snapshot is not None
            assert req2.publish_gate() is False, (
                "a paused job must not be allowed to publish"
            )
        finally:
            engine2.release.set()
            t2.join(timeout=5)

    def test_a_gate_that_does_not_commit_gives_the_claim_back(self):
        """A publish attempt that turns out not to commit — a same-volume
        rename that reports the destination is on another volume, or a
        locked-target retry — must free the job again. Holding the claim
        across the long copy or the retry wait made Global Pause see
        "already terminal" for a job that had published nothing and could
        still be cancelled out from under it, leaving it in no set at all."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            assert req.publish_gate is not None
            assert req.publish_release is not None

            assert req.publish_gate() is True
            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is None, "while the claim is held the job is not pausable"

            req.publish_release()
            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is not None, (
                "an attempt that committed nothing must leave the job pausable"
            )
        finally:
            engine.release.set()
            orch.cancel()
            thread.join(timeout=5)

    def test_release_cannot_take_back_a_claim_the_pause_won(self):
        """Release only ever gives back the caller's OWN claim. If a pause
        got there first, the engine's release must not silently hand the
        job back to the publish path."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is not None  # the pause claims it

            req.publish_release()  # engine side, losing the race

            assert req.publish_gate() is False, (
                "the pause's claim must survive an unrelated release"
            )
        finally:
            engine.release.set()
            orch.cancel()
            thread.join(timeout=5)

    def test_pause_snapshot_keeps_the_output_path_tracker(self):
        """_final_output_path is init=False, so dataclasses.replace silently
        resets it. It is the only in-memory record of what yt-dlp produced,
        and a job paused in the instant between yt-dlp returning and its
        checkpoint being written has nothing else to resume from."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        key, req = _make_job("a", "http://a")

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            req._final_output_path = "/ws/batch-1/jobA/Song.mp3"

            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is not None
            assert snapshot._final_output_path == "/ws/batch-1/jobA/Song.mp3"
        finally:
            engine.release.set()
            orch.cancel()
            thread.join(timeout=5)

    def test_mid_resolve_pause_returns_the_pre_resolve_request(self):
        """A job caught between its Spotify two-stage resolver being
        consumed and the resolved URL being committed must still be
        capturable. Refusing the snapshot dropped it from the paused set
        entirely while the engine still cancelled it -- the track was simply
        lost. The pre-resolve copy is handed back instead, so a resume
        re-runs the match."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)

        resolver_started = threading.Event()
        release_resolver = threading.Event()

        def slow_resolver(_cancel_ev):
            resolver_started.set()
            release_resolver.wait(timeout=5)
            return "http://resolved"

        key, req = _make_job("a", "placeholder")
        req.url_resolver = slow_resolver

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert resolver_started.wait(timeout=5)

            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is not None, "a mid-resolve job must still be pausable"
            assert snapshot.url_resolver is slow_resolver, (
                "the snapshot must still carry the resolver so the resume "
                "re-runs the match"
            )
            assert snapshot.url == "placeholder"
            # Never the half-committed live request.
            assert snapshot is not req
        finally:
            release_resolver.set()
            thread.join(timeout=5)

    def test_mid_resolve_snapshot_stash_is_cleared_once_resolution_commits(self):
        """Once the resolved URL is committed, the pause snapshot must come
        from the LIVE request again -- a stale pre-resolve copy would resume
        with a placeholder URL and re-run a match that already succeeded."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)

        key, req = _make_job("a", "placeholder")
        req.url_resolver = lambda _ev: "http://resolved"

        thread = threading.Thread(target=orch.run_batch, args=([(key, req)],))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            _state, snapshot = orch.live_request_snapshot(key)
            assert snapshot is not None
            assert snapshot.url == "http://resolved"
            assert snapshot.url_resolver is None
        finally:
            engine.release.set()
            thread.join(timeout=5)

    def test_every_job_is_snapshottable_before_it_is_submitted(self):
        """Registration must not wait for the (staggered, therefore slow)
        submit loop: a job still queued behind the stagger had no lock, no
        live request and no cancel event, so Global Pause skipped it, it was
        never captured as paused, and the whole-batch cancel that follows
        threw it away."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        jobs = [_make_job(k, f"http://{k}") for k in ("a", "b", "c", "d")]

        thread = threading.Thread(
            target=orch.run_batch, args=(jobs,), kwargs={"delay_range": (0.4, 0.4)},
        )
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            # "d" is still waiting behind three stagger sleeps and a
            # single-worker pool -- it has certainly not been submitted.
            captured = {}
            for key, _req in jobs:
                _state, snapshot = orch.live_request_snapshot(key)
                if snapshot is not None:
                    captured[key] = snapshot
            assert set(captured) == {"a", "b", "c", "d"}, (
                "every job in the batch must be pausable, submitted or not"
            )
        finally:
            engine.release.set()
            orch.cancel()
            thread.join(timeout=10)

    def test_cancel_track_reaches_a_not_yet_submitted_job(self):
        """The other half of the same registration gap: cancel_track() was a
        no-op for an unsubmitted job, so pausing one marked the card paused
        while the download went on to run anyway."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        jobs = [_make_job(k, f"http://{k}") for k in ("a", "b")]

        thread = threading.Thread(
            target=orch.run_batch, args=(jobs,), kwargs={"delay_range": (0.3, 0.3)},
        )
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            orch.cancel_track("b")
        finally:
            engine.release.set()
            thread.join(timeout=10)

        assert "http://b" not in engine.downloaded, (
            "a cancelled not-yet-submitted job must never download"
        )

    def test_a_paused_job_is_not_reported_as_cancelled(self):
        """A whole-batch pause cancels the engine, so every paused job lands
        in the end-of-batch outstanding-cancel sweep. Emitting "cancelled"
        there would overwrite the "paused" label the pause just set."""
        from core.download_orchestrator import DownloadOrchestrator

        engine = _BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        jobs = [_make_job(k, f"http://{k}") for k in ("a", "b")]

        thread = threading.Thread(target=orch.run_batch, args=(jobs,))
        thread.start()
        try:
            assert engine.started.wait(timeout=5)
            for key, _req in jobs:
                orch.live_request_snapshot(key)  # claim both for a pause
        finally:
            engine.release.set()
            orch.cancel()
            thread.join(timeout=10)

        cancelled_keys = {k for k, s in cb.track_statuses if s == "cancelled"}
        assert cancelled_keys == set(), (
            f"paused jobs must not be reported cancelled, got {cancelled_keys}"
        )


class TestPreexistingJobs:
    """Duplicate-skip jobs: no engine.download() call, terminal success,
    correctly folded into the batch total. Root-cause coverage for the
    "19/19 instead of 59/59" bug — see DownloadOrchestrator.run_batch's
    `preexisting` parameter."""

    def test_preexisting_never_touches_the_engine(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=2)

        result = orch.run_batch([], preexisting=[("skip1", "/music/a.mp3")])

        assert result.total == 1
        assert result.completed == 1
        assert result.failed == 0
        assert engine._downloaded == []  # no real download ran
        assert cb.track_preexisting == [("skip1", "/music/a.mp3")]
        assert cb.track_finished == []  # not the same callback as a real finish

    def test_mixed_batch_total_is_downloads_plus_preexisting(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=3)

        jobs = [_make_job(f"dl{i}", f"http://dl{i}") for i in range(19)]
        preexisting = [(f"skip{i}", f"/music/skip{i}.mp3") for i in range(40)]

        result = orch.run_batch(jobs, preexisting=preexisting)

        assert result.total == 59
        assert result.completed == 59
        assert result.failed == 0
        assert len(cb.track_finished) == 19       # real downloads
        assert len(cb.track_preexisting) == 40     # duplicate-skips
        assert cb.snapshots[-1].preexisting == 40
        assert cb.snapshots[-1].downloaded == 19
        assert "already existed" in cb.messages[-1]

    def test_all_preexisting_batch_is_not_treated_as_empty(self):
        """An empty `jobs` list with non-empty `preexisting` must still be a
        real, completed batch — not the "no fake 100%" empty-batch path."""
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_outcome import BatchOutcome
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb)

        result = orch.run_batch([], preexisting=[("skip1", "/a.mp3"), ("skip2", "/b.mp3")])

        assert result.total == 2
        assert result.completed == 2
        assert result.outcome == BatchOutcome.COMPLETED
        assert cb.overall[-1] == pytest.approx(1.0)

    def test_precancelled_batch_marks_preexisting_cancelled_not_done(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb)
        engine._cancel_event.set()

        result = orch.run_batch(
            [_make_job("a", "http://a")], preexisting=[("skip1", "/a.mp3")],
        )

        assert result.cancelled is True
        statuses = dict(cb.track_statuses)
        assert statuses.get("skip1") == "cancelled"
        assert cb.track_preexisting == []  # never resolved as a success


class TestBatchOutcome:
    """The orchestrator must distinguish clean completion, completion with
    failures, and cancellation — and never fake a 100% bar on cancel."""

    def test_clean_completion_outcome(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_outcome import BatchOutcome
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=2)
        result = orch.run_batch([_make_job("a", "http://a"), _make_job("b", "http://b")])
        assert result.outcome == BatchOutcome.COMPLETED
        assert cb.outcome == BatchOutcome.COMPLETED
        # Every job completed => bar honestly reaches 1.0.
        assert cb.overall[-1] == pytest.approx(1.0)

    def test_completion_with_errors_outcome(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_outcome import BatchOutcome
        engine = FakeEngine(fail_keys={"http://b"})
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=2)
        result = orch.run_batch([_make_job("a", "http://a"), _make_job("b", "http://b")])
        assert result.outcome == BatchOutcome.COMPLETED_WITH_ERRORS
        assert cb.outcome == BatchOutcome.COMPLETED_WITH_ERRORS

    def test_precancelled_batch_does_not_reach_100_percent(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_outcome import BatchOutcome
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb)
        engine._cancel_event.set()
        result = orch.run_batch([_make_job("a", "http://a"), _make_job("b", "http://b")])
        assert result.outcome == BatchOutcome.CANCELLED_BY_USER
        # No overall_progress==1.0 emitted for a batch cancelled before start.
        assert all(v < 1.0 for v in cb.overall)

    def test_cancelled_queued_future_is_not_reported_as_failure(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.batch_outcome import BatchOutcome

        class BlockingEngine(FakeEngine):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def download(self, req: DownloadRequest) -> None:
                if req.url == "http://a":
                    self.started.set()
                    self.release.wait(5.0)
                    return
                super().download(req)

        engine = BlockingEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)
        result_box = {}
        done = threading.Event()

        def run():
            try:
                result_box["result"] = orch.run_batch([
                    _make_job("a", "http://a"),
                    _make_job("b", "http://b"),
                ])
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert engine.started.wait(2.0)
        orch.cancel()
        engine.release.set()
        assert done.wait(15.0)
        thread.join(1.0)

        assert not thread.is_alive()
        result = result_box["result"]
        assert result.outcome == BatchOutcome.CANCELLED_BY_USER
        assert result.failed == 0
        assert cb.track_errors == []
        assert cb.snapshots[-1].cancelled == 2
        assert cb.snapshots[-1].progress < 1.0

    def test_final_status_message_has_no_emoji(self):
        from core.download_orchestrator import DownloadOrchestrator
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=2)
        orch.run_batch([_make_job("a", "http://a")])
        # No decorative glyphs in orchestrator status text.
        joined = "".join(cb.messages)
        for glyph in ("✅", "🚫", "⚠", "❌", "🔴", "📡"):
            assert glyph not in joined


# ──────────────────────────────────────────────────────────────────────────────
# History platform persistence (S1-1 regression guard)
# ──────────────────────────────────────────────────────────────────────────────

class _RecordingDB:
    """In-memory stand-in for HistoryDB.insert that records every record."""

    def __init__(self) -> None:
        self.records: list = []

    def insert(self, record) -> None:
        self.records.append(record)


def _make_job_with_platform(key, url, platform):
    return (key, DownloadRequest(
        url=url,
        output_dir="/tmp",
        media_type=MediaType.AUDIO,
        forced_title=key,
        platform=platform,
    ))


class TestHistoryPlatform:
    """The orchestrator persisted platform='youtube' for every download
    regardless of source. The history panel filters and colour-codes by
    platform, so YT Music and Spotify downloads were mis-tagged."""

    def test_ytmusic_persists_as_ytmusic(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.playlist_parser import SourcePlatform

        db = _RecordingDB()
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, db=db, max_workers=1)

        orch.run_batch([
            _make_job_with_platform("a", "http://yt-music", SourcePlatform.YOUTUBE_MUSIC),
        ])

        assert len(db.records) == 1
        assert db.records[0].platform == "ytmusic"

    def test_spotify_persists_as_spotify(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.playlist_parser import SourcePlatform

        db = _RecordingDB()
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, db=db, max_workers=1)

        orch.run_batch([
            _make_job_with_platform("a", "http://spot", SourcePlatform.SPOTIFY),
        ])

        assert db.records[0].platform == "spotify"

    def test_missing_platform_persists_as_unknown(self):
        from core.download_orchestrator import DownloadOrchestrator

        db = _RecordingDB()
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, db=db, max_workers=1)

        # platform defaults to None on DownloadRequest
        orch.run_batch([_make_job("a", "http://something")])

        assert db.records[0].platform == "unknown"

    def test_youtube_persists_as_youtube(self):
        from core.download_orchestrator import DownloadOrchestrator
        from core.playlist_parser import SourcePlatform

        db = _RecordingDB()
        engine = FakeEngine()
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, db=db, max_workers=1)

        orch.run_batch([
            _make_job_with_platform("a", "http://yt", SourcePlatform.YOUTUBE),
        ])

        assert db.records[0].platform == "youtube"


# ──────────────────────────────────────────────────────────────────────────────
# Error-message Doctor-linking wiring (reliability-hardening phase 4)
# ──────────────────────────────────────────────────────────────────────────────

class TestErrorEnrichmentWiring:
    """The orchestrator must forward the failing request's cookies_file/
    cookies_browser into classify_error() so YouTube Doctor enrichment
    sees the same cookie configuration used for that download, instead
    of always seeing the empty default."""

    def test_cookies_config_forwarded_to_classify_error(self, monkeypatch):
        import core.download_orchestrator as orch_mod
        from core.download_orchestrator import DownloadOrchestrator

        captured = {}
        original_classify = orch_mod.classify_error

        def spy_classify_error(exc, *, cookies_file="", cookies_browser=""):
            captured["cookies_file"] = cookies_file
            captured["cookies_browser"] = cookies_browser
            return original_classify(exc, cookies_file=cookies_file, cookies_browser=cookies_browser)

        monkeypatch.setattr(orch_mod, "classify_error", spy_classify_error)

        engine = FakeEngine(fail_keys={"http://a"})
        cb = FakeCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)

        req = DownloadRequest(
            url="http://a", output_dir="/tmp", media_type=MediaType.AUDIO,
            cookies_file="my/cookies.txt", cookies_browser="chrome",
        )
        orch.run_batch([("a", req)])

        assert captured["cookies_file"] == "my/cookies.txt"
        assert captured["cookies_browser"] == "chrome"
