import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.metadata_io import IORequestIdentity, IOErrorKind
from ui.controllers.metadata_controller import MetadataController


def app():
    return QApplication.instance() or QApplication([])


def process_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return bool(predicate())


def blocking_operation(started: threading.Event, release: threading.Event, value):
    def operation(_cancellation):
        started.set()
        release.wait(5)
        return value
    return operation


def request(name):
    return IORequestIdentity.create(name, 0, 0, ())


def test_superseded_worker_remains_owned_and_only_current_result_is_published():
    app(); controller = MetadataController()
    a_started, a_release = threading.Event(), threading.Event()
    published, errors = [], []
    controller.metadata_io_finished.connect(lambda identity, result: published.append((identity, result)))
    controller.metadata_io_error.connect(lambda identity, error: errors.append((identity, error)))
    worker_a = controller._start_metadata_io_worker(
        request("A"), blocking_operation(a_started, a_release, "stale A"))
    assert a_started.wait(2)
    worker_b = controller._start_metadata_io_worker(request("B"), lambda _token: "current B")

    assert worker_a.cancellation.cancelled
    assert worker_a in controller._metadata_io_workers
    assert process_until(lambda: any(result == "current B" for _, result in published))
    assert all(result != "stale A" for _, result in published)
    a_release.set()
    assert worker_a.wait(5000) and worker_b.wait(5000)
    assert process_until(lambda: not controller._metadata_io_workers)
    assert [result for _, result in published] == ["current B"]
    assert not errors
    controller.deleteLater()


def test_shutdown_cancels_and_waits_for_superseded_and_current_workers():
    app(); controller = MetadataController()
    starts = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    workers = []
    for index in range(2):
        workers.append(controller._start_metadata_io_worker(
            request(str(index)), blocking_operation(starts[index], releases[index], index)))
        assert starts[index].wait(2)
    assert set(workers).issubset(controller._metadata_io_workers)
    assert controller.request_shutdown() is False
    assert all(worker.cancellation.cancelled for worker in workers)
    assert set(workers).issubset(controller._active_shutdown_workers())
    for release in releases:
        release.set()
    assert all(worker.wait(5000) for worker in workers)
    assert process_until(lambda: not controller.has_active_shutdown_work())
    controller.deleteLater()


def test_root_replacement_invalidates_all_living_io_workers(tmp_path):
    app(); controller = MetadataController()
    starts = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]
    workers = []
    for index in range(2):
        workers.append(controller._start_metadata_io_worker(
            request(str(index)), blocking_operation(starts[index], releases[index], index)))
        assert starts[index].wait(2)
    root = tmp_path / "new-root"; root.mkdir()
    controller.scan(root, False)
    assert all(worker.cancellation.cancelled for worker in workers)
    assert controller._metadata_io_worker is None
    assert set(workers).issubset(controller._metadata_io_workers)
    for release in releases:
        release.set()
    assert all(worker.wait(5000) for worker in workers)
    assert process_until(lambda: not controller._metadata_io_workers)
    controller.cancel_scan()
    scan_worker = controller._scan_worker
    if scan_worker is not None:
        scan_worker.wait(5000)
    controller.deleteLater()


def test_cancelled_worker_publishes_structured_cancelled_error_and_no_result():
    app(); controller = MetadataController()
    started, release = threading.Event(), threading.Event()
    published, errors = [], []
    controller.metadata_io_finished.connect(lambda identity, result: published.append(result))
    controller.metadata_io_error.connect(lambda identity, error: errors.append(error))
    worker = controller._start_metadata_io_worker(
        request("dry_run"), blocking_operation(started, release, object()))
    assert started.wait(2)
    controller.cancel_metadata_io(); release.set()
    assert worker.wait(5000)
    assert process_until(lambda: bool(errors))
    assert not published and errors[0].kind is IOErrorKind.CANCELLED
    controller.deleteLater()
