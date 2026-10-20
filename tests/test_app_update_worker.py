"""Full application preparation is isolated from the UI event loop."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from ui.workers.app_update_worker import AppUpdateWorker


def test_prepare_propagates_progress_and_cancel_callback(monkeypatch):
    release = object()
    prepared = object()
    seen = []

    def prepare(value, *, progress, cancelled):
        seen.append(value)
        assert not cancelled()
        progress(2**33, 2**34)
        return prepared

    backend = SimpleNamespace(
        prepare_app_update=prepare,
        get_prepared_app_update=lambda: None,
        get_app_update_status=lambda: {},
    )
    monkeypatch.setitem(sys.modules, "core.app_updates", backend)
    worker = AppUpdateWorker(release)
    progress, completed = [], []
    worker.progress.connect(lambda a, b: progress.append((a, b)))
    worker.completed.connect(lambda a, b: completed.append((a, b)))
    worker.run()
    assert seen == [release]
    assert progress == [(2**33, 2**34)]
    assert completed == [(prepared, "")]


def test_restore_reads_local_state_only_and_keeps_failed_status(monkeypatch):
    prepared = object()
    backend = SimpleNamespace(
        prepare_app_update=lambda *a, **k: (_ for _ in ()).throw(AssertionError("network prepare")),
        get_prepared_app_update=lambda: prepared,
        get_app_update_status=lambda: {"status": "error"},
    )
    monkeypatch.setitem(sys.modules, "core.app_updates", backend)
    worker = AppUpdateWorker()
    completed = []
    worker.completed.connect(lambda a, b: completed.append((a, b)))
    worker.run()
    assert completed == [(prepared, "")]
    assert worker.update_status == {"status": "error"}


def test_cancelled_preparation_suppresses_late_result(monkeypatch):
    stopped = [False]

    def prepare(*args, **kwargs):
        stopped[0] = True
        return object()

    monkeypatch.setitem(sys.modules, "core.app_updates", SimpleNamespace(
        prepare_app_update=prepare,
        get_prepared_app_update=lambda: None,
        get_app_update_status=lambda: {},
    ))
    worker = AppUpdateWorker(object())
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: stopped[0])
    completed = []
    worker.completed.connect(lambda *a: completed.append(a))
    worker.run()
    assert completed == []


def test_failure_is_reported_without_execution(monkeypatch):
    def prepare(*args, **kwargs):
        raise ValueError("verification failed")

    monkeypatch.setitem(sys.modules, "core.app_updates", SimpleNamespace(
        prepare_app_update=prepare,
        get_prepared_app_update=lambda: None,
        get_app_update_status=lambda: {},
    ))
    worker = AppUpdateWorker(object())
    completed = []
    worker.completed.connect(lambda *a: completed.append(a))
    worker.run()
    assert completed == [(None, "verification failed")]
