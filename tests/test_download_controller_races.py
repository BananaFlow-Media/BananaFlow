from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


class _FakeWorker(QObject):
    batch_snapshot = Signal(object)
    all_finished = Signal(object)

    def isRunning(self) -> bool:
        return True

    def cancel(self) -> None:
        pass


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _controller(tmp_path, monkeypatch, app):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    from config import AppConfig
    from core.downloader import DownloadEngine
    from ui.controllers.download_controller import DownloadController

    return DownloadController(AppConfig(), DownloadEngine())


def _stamped_snapshot(batch_id):
    """A snapshot belonging to `batch_id`, the way a live batch produces one."""
    from core.batch_progress import BatchProgressAggregator

    agg = BatchProgressAggregator(speed_smoothing=1.0)
    agg.reset(["a"], batch_id=batch_id)
    agg.update("a", downloaded_bytes=10, total_bytes=100)
    return agg.snapshot()


def test_stale_worker_snapshot_does_not_forward_to_new_batch(tmp_path, monkeypatch, app):
    ctrl = _controller(tmp_path, monkeypatch, app)
    old_worker = _FakeWorker()
    new_worker = _FakeWorker()
    old_worker.batch_snapshot.connect(ctrl._on_worker_batch_snapshot)
    new_worker.batch_snapshot.connect(ctrl._on_worker_batch_snapshot)

    received = []
    ctrl.batch_snapshot.connect(received.append)
    ctrl._dl_worker = new_worker
    # _build_batch_worker does this in production: mint the identity of the
    # batch now being shown, and hand the same value to the worker.
    ctrl._batch_snapshot_id = "live-batch"

    snapshot = _stamped_snapshot("live-batch")

    old_worker.batch_snapshot.emit(snapshot)
    assert received == []

    new_worker.batch_snapshot.emit(snapshot)
    assert received == [snapshot]


def test_stale_batchs_snapshot_is_rejected_even_from_the_current_worker(
    tmp_path, monkeypatch, app
):
    """Worker identity alone is not enough. A snapshot still in flight from the
    previous batch can arrive on the current worker's connection; it describes
    work the footer is no longer showing and must be dropped on its own id."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    worker = _FakeWorker()
    worker.batch_snapshot.connect(ctrl._on_worker_batch_snapshot)

    received = []
    ctrl.batch_snapshot.connect(received.append)
    ctrl._dl_worker = worker
    ctrl._batch_snapshot_id = "batch-2"

    worker.batch_snapshot.emit(_stamped_snapshot("batch-1"))
    assert received == []

    current = _stamped_snapshot("batch-2")
    worker.batch_snapshot.emit(current)
    assert received == [current]


def test_no_live_batch_means_nothing_repaints_the_footer(tmp_path, monkeypatch, app):
    """With no batch established there is no identity to match, so a snapshot
    from anywhere - notably a single-track resume running its own 1-job
    aggregator - must not reach the footer."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    worker = _FakeWorker()
    worker.batch_snapshot.connect(ctrl._on_worker_batch_snapshot)

    received = []
    ctrl.batch_snapshot.connect(received.append)
    ctrl._dl_worker = worker
    assert ctrl._batch_snapshot_id is None

    worker.batch_snapshot.emit(_stamped_snapshot("some-resume"))
    assert received == []


def test_stale_worker_finish_cannot_end_newer_batch(tmp_path, monkeypatch, app):
    from core.batch_outcome import BatchOutcome

    ctrl = _controller(tmp_path, monkeypatch, app)
    old_worker = _FakeWorker()
    new_worker = _FakeWorker()
    old_worker.all_finished.connect(ctrl._on_batch_done)
    new_worker.all_finished.connect(ctrl._on_batch_done)

    outcomes = []
    ctrl.batch_finished.connect(outcomes.append)
    ctrl._dl_worker = new_worker

    old_worker.all_finished.emit(BatchOutcome.CANCELLED_BY_USER)
    assert outcomes == []
    assert ctrl._dl_worker is new_worker

    new_worker.all_finished.emit(BatchOutcome.COMPLETED)
    assert outcomes == [BatchOutcome.COMPLETED]
    assert ctrl._dl_worker is None


def test_resume_worker_finish_is_allowed_when_no_main_batch(tmp_path, monkeypatch, app):
    from core.batch_outcome import BatchOutcome

    ctrl = _controller(tmp_path, monkeypatch, app)
    resume_worker = _FakeWorker()
    resume_worker.all_finished.connect(ctrl._on_batch_done)

    outcomes = []
    ctrl.batch_finished.connect(outcomes.append)
    ctrl._dl_worker = None
    ctrl._resume_workers.append(resume_worker)

    resume_worker.all_finished.emit(BatchOutcome.COMPLETED)

    assert outcomes == [BatchOutcome.COMPLETED]


def test_one_of_several_resume_workers_finishing_does_not_end_downloading_mode(
    tmp_path, monkeypatch, app,
):
    """Multiple per-track resume workers can run at once (each an
    independent single-job DownloadWorker started by resume_track). One of
    them finishing must not tell the UI "no longer downloading" -- via
    downloading_changed(False) -- or consume the shared termination intent,
    while a sibling resume is still active; only the truly last active
    worker may do that."""
    from core.batch_outcome import BatchOutcome

    ctrl = _controller(tmp_path, monkeypatch, app)
    first = _FakeWorker()
    second = _FakeWorker()
    # Mirror resume_track()'s real connection order exactly: _on_batch_done
    # first, then a removal lambda -- the removal must NOT have already
    # happened by the time _on_batch_done inspects _resume_workers.
    for worker in (first, second):
        worker.all_finished.connect(ctrl._on_batch_done)
        worker.all_finished.connect(
            lambda _outcome=None, w=worker: (
                ctrl._resume_workers.remove(w) if w in ctrl._resume_workers else None
            )
        )

    outcomes = []
    downloading_states = []
    ctrl.batch_finished.connect(outcomes.append)
    ctrl.downloading_changed.connect(downloading_states.append)
    ctrl._dl_worker = None
    ctrl._resume_workers.extend([first, second])

    first.all_finished.emit(BatchOutcome.COMPLETED)

    assert outcomes == [], "must not report batch_finished while a sibling resume is still active"
    assert downloading_states == [], "must not leave downloading mode while a sibling resume is still active"

    second.all_finished.emit(BatchOutcome.COMPLETED)

    assert outcomes == [BatchOutcome.COMPLETED]
    assert downloading_states == [False]


class _FakeResumeDownloadWorker(QObject):
    """Stands in for ui.workers.download_worker.DownloadWorker so
    resume_track() can be exercised end-to-end without a real QThread or
    network activity."""

    track_progress    = Signal(str, float)
    track_speed       = Signal(str, float, float)
    track_status      = Signal(str, str)
    track_finished    = Signal(str, str)
    job_error         = Signal(str, object)
    all_finished      = Signal(object)
    track_thumbnail   = Signal(str, str)

    def __init__(self, jobs, engine, config, db=None, max_workers=1, parent=None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.started = False

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return self.started


def test_resume_track_removal_lambda_actually_removes_the_finished_worker(
    tmp_path, monkeypatch, app,
):
    """Regression for a bug found while testing finding #11's fix: the real
    resume_track() removal lambda used to declare only a defaulted
    `w=resume_worker` parameter, which all_finished's emitted BatchOutcome
    argument silently overrode -- `w` ended up bound to the outcome, not
    the worker, so `w in self._resume_workers` was always False and nothing
    was ever actually removed. _resume_workers would grow forever, one
    stale entry per resumed track, and every resume after the very first
    one would find a stale "still active" sibling and never report
    completion -- exactly the downloading-mode-never-clears failure finding
    #11 described, just from a different root cause than the one that
    finding names."""
    from core.batch_outcome import BatchOutcome
    from core.downloader import DownloadRequest, MediaType

    monkeypatch.setattr(
        "ui.workers.download_worker.DownloadWorker", _FakeResumeDownloadWorker,
    )

    ctrl = _controller(tmp_path, monkeypatch, app)

    class _Card:
        def __init__(self, title: str) -> None:
            self.title = title
            self.artist = "Artist"
            self.track_url = f"https://youtu.be/{title}"

        def set_status(self, *_a, **_k) -> None:
            pass

        def set_progress(self, *_a, **_k) -> None:
            pass

    card_a, card_b = _Card("A"), _Card("B")
    ka, kb = str(id(card_a)), str(id(card_b))
    ctrl._paused_requests[ka] = DownloadRequest(
        url=card_a.track_url, output_dir=str(tmp_path), media_type=MediaType.AUDIO,
    )
    ctrl._paused_requests[kb] = DownloadRequest(
        url=card_b.track_url, output_dir=str(tmp_path), media_type=MediaType.AUDIO,
    )

    ctrl.resume_track(card_a)
    ctrl.resume_track(card_b)
    assert len(ctrl._resume_workers) == 2

    outcomes = []
    ctrl.batch_finished.connect(outcomes.append)
    worker_a, worker_b = ctrl._resume_workers[0], ctrl._resume_workers[1]

    worker_a.all_finished.emit(BatchOutcome.COMPLETED)

    assert worker_a not in ctrl._resume_workers, "the finished worker must actually be removed"
    assert worker_b in ctrl._resume_workers
    assert outcomes == [], "must not report done while the sibling resume is still active"

    worker_b.all_finished.emit(BatchOutcome.COMPLETED)

    assert ctrl._resume_workers == []
    assert outcomes == [BatchOutcome.COMPLETED]


def test_fatal_stop_cleans_up_abandoned_workspace_same_as_cancel(tmp_path, monkeypatch, app):
    """Finding #12: a fatal stop (e.g. a broken cookie jar that would fail
    every remaining job the same way) must trigger the same abandoned-
    workspace cleanup as a deliberate user cancel -- only CANCELLED_BY_USER
    was covered before, so a fatal stop left its workspace stranded on disk
    forever since nothing else ever sweeps it."""
    from core.batch_outcome import BatchOutcome
    from core.downloader import DownloadRequest, MediaType

    from utils.paths import register_output_root

    ctrl = _controller(tmp_path, monkeypatch, app)

    # Workspace removal proves ownership by containment under a RECORDED
    # output root (utils.paths.register_output_root, which the real flow does
    # from make_batch_workspace) — a hand-built lookalike is deliberately
    # refused, so the root has to be recorded here too.
    register_output_root(tmp_path)
    workspace_container = tmp_path / ".bananaflow_tmp" / "batch-1"
    job_dir = workspace_container / "job-a"
    job_dir.mkdir(parents=True)
    (job_dir / "song.part").write_bytes(b"partial")

    req = DownloadRequest(
        url="https://example.com/a", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
        workspace_dir=str(job_dir),
    )
    worker = _FakeWorker()
    worker._jobs = [("k", req)]
    worker.all_finished.connect(ctrl._on_batch_done)

    ctrl._dl_worker = worker
    ctrl._set_termination_intent(BatchOutcome.STOPPED_BY_FATAL_ERROR)

    # The orchestrator's own best guess (it can't tell a fatal stop from a
    # plain cancel) is irrelevant -- the recorded termination intent wins,
    # and cleanup must follow IT, not this raw argument.
    worker.all_finished.emit(BatchOutcome.CANCELLED_BY_USER)

    assert not job_dir.exists(), "abandoned workspace must be swept on a fatal stop"
    assert not workspace_container.exists()


def test_plain_completion_never_triggers_workspace_cleanup(tmp_path, monkeypatch, app):
    """Control case: a normal, non-cancelled/non-fatal finish must leave the
    (already-published-and-cleaned-up-by-the-orchestrator) job list alone
    -- _cleanup_cancelled_batch must not run for an ordinary completion."""
    from core.batch_outcome import BatchOutcome
    from core.downloader import DownloadRequest, MediaType

    from utils.paths import register_output_root

    ctrl = _controller(tmp_path, monkeypatch, app)

    # Workspace removal proves ownership by containment under a RECORDED
    # output root (utils.paths.register_output_root, which the real flow does
    # from make_batch_workspace) — a hand-built lookalike is deliberately
    # refused, so the root has to be recorded here too.
    register_output_root(tmp_path)
    workspace_container = tmp_path / ".bananaflow_tmp" / "batch-1"
    job_dir = workspace_container / "job-a"
    job_dir.mkdir(parents=True)
    (job_dir / "song.part").write_bytes(b"partial")

    req = DownloadRequest(
        url="https://example.com/a", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
        workspace_dir=str(job_dir),
    )
    worker = _FakeWorker()
    worker._jobs = [("k", req)]
    worker.all_finished.connect(ctrl._on_batch_done)
    ctrl._dl_worker = worker

    worker.all_finished.emit(BatchOutcome.COMPLETED)

    assert job_dir.exists()
    assert (job_dir / "song.part").exists()


def test_user_cancel_overrides_rapid_pause_but_not_fatal(tmp_path, monkeypatch, app):
    from core.batch_outcome import BatchOutcome

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl.global_pause()
    ctrl.cancel_all()
    assert ctrl._termination_intent == BatchOutcome.CANCELLED_BY_USER

    ctrl._termination_intent = None
    ctrl._set_termination_intent(BatchOutcome.STOPPED_BY_FATAL_ERROR)
    ctrl.cancel_all()
    assert ctrl._termination_intent == BatchOutcome.STOPPED_BY_FATAL_ERROR
