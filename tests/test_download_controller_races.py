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
