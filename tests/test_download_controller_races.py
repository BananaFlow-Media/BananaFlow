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


def test_stale_worker_snapshot_does_not_forward_to_new_batch(tmp_path, monkeypatch, app):
    from core.batch_progress import BatchProgressAggregator

    ctrl = _controller(tmp_path, monkeypatch, app)
    old_worker = _FakeWorker()
    new_worker = _FakeWorker()
    old_worker.batch_snapshot.connect(ctrl._on_worker_batch_snapshot)
    new_worker.batch_snapshot.connect(ctrl._on_worker_batch_snapshot)

    received = []
    ctrl.batch_snapshot.connect(received.append)
    ctrl._dl_worker = new_worker

    agg = BatchProgressAggregator(speed_smoothing=1.0)
    agg.reset(["a"])
    agg.update("a", downloaded_bytes=10, total_bytes=100)
    snapshot = agg.snapshot()

    old_worker.batch_snapshot.emit(snapshot)
    assert received == []

    new_worker.batch_snapshot.emit(snapshot)
    assert received == [snapshot]


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
