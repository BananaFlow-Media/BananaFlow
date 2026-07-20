import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from core.filesystem_monitoring import (
    FilesystemEventKind, MonitoringState, WatchRootSession,
)
from ui.services.filesystem_watch_service import (
    FilesystemWatchService, _DirectoryEntry,
)
from ui.controllers.metadata_controller import MetadataController
from core.metadata_models import ScanResult


def app():
    return QApplication.instance() or QApplication([])


def entry(path: Path, *, directory=False, size=1, mtime=1, inode=1):
    return _DirectoryEntry(path, directory, size, mtime, 1, inode)


def test_directory_diffs_emit_create_remove_modify_and_relocation(tmp_path, monkeypatch):
    app(); root = tmp_path / "music"; root.mkdir()
    created = root / "new.mp3"; created.write_bytes(b"new")
    modified = root / "same.mp3"; modified.write_bytes(b"same")
    removed = root / "gone.mp3"
    moved_old = root / "old.mp3"; moved_new = root / "renamed.mp3"
    moved_new.write_bytes(b"move")
    service = FilesystemWatchService(debounce_ms=10000)
    session = WatchRootSession.create(root, 4, 8)
    service.start_session(session, {root})
    service._snapshots[root] = {
        "same.mp3": entry(modified, size=4, mtime=1, inode=10),
        "gone.mp3": entry(removed, inode=11),
        "old.mp3": entry(moved_old, size=4, mtime=1, inode=12),
    }
    new_snapshot = {
        "same.mp3": entry(modified, size=4, mtime=2, inode=10),
        "new.mp3": entry(created, size=3, inode=13),
        "renamed.mp3": entry(moved_new, size=4, mtime=1, inode=12),
    }
    monkeypatch.setattr(service, "_snapshot_directory", lambda _path: new_snapshot)
    batches = []; service.batch_ready.connect(batches.append)
    service._on_directory_changed(str(root)); service._flush()
    kinds = {event.kind for event in batches[0].events}
    assert FilesystemEventKind.CREATED in kinds
    assert FilesystemEventKind.REMOVED in kinds
    assert FilesystemEventKind.MODIFIED in kinds
    assert FilesystemEventKind.RELOCATED in kinds
    service.shutdown()


def test_duplicate_burst_is_debounced_and_bounded(tmp_path):
    app(); root = tmp_path / "music"; root.mkdir(); path = root / "a.mp3"
    path.write_bytes(b"a")
    service = FilesystemWatchService(debounce_ms=10000, maximum_paths=2)
    service.start_session(WatchRootSession.create(root, 1, 1), {root})
    for _ in range(20):
        service._queue(FilesystemEventKind.MODIFIED, path)
    assert service.pending_count == 1
    batches = []; service.batch_ready.connect(batches.append); service._flush()
    assert len(batches) == 1 and len(batches[0].events) == 1
    service.shutdown()


def test_manual_refresh_is_visible_fallback_in_degraded_mode(tmp_path):
    app(); root = tmp_path / "missing-root"
    service = FilesystemWatchService(debounce_ms=10000)
    states = []; batches = []
    service.state_changed.connect(lambda state, detail: states.append((state, detail)))
    service.batch_ready.connect(batches.append)
    session = WatchRootSession("session", root, 2, 3, True)
    service.start_session(session, ())
    assert service.state is MonitoringState.DEGRADED
    service.manual_refresh()
    assert batches and batches[-1].session_id == "session"
    service.shutdown()
    assert service.state is MonitoringState.DISABLED


def test_pause_collects_without_publishing_then_flushes(tmp_path):
    app(); root = tmp_path / "music"; root.mkdir(); path = root / "a.mp3"
    path.write_bytes(b"a")
    service = FilesystemWatchService(debounce_ms=10000)
    service.start_session(WatchRootSession.create(root, 1, 1), {root})
    batches = []; service.batch_ready.connect(batches.append)
    service.set_paused(True)
    service._queue(FilesystemEventKind.MODIFIED, path)
    service._flush(); assert not batches and service.pending_count == 1
    service.set_paused(False); service._flush()
    assert batches and batches[0].events[0].path == path
    service.shutdown()


def test_session_replacement_clears_old_events_and_shutdown_stops_timers(tmp_path):
    app(); root = tmp_path / "music"; root.mkdir(); path = root / "a.mp3"
    path.write_bytes(b"a")
    service = FilesystemWatchService(debounce_ms=10000)
    first = WatchRootSession.create(root, 1, 1)
    second = WatchRootSession.create(root, 2, 2)
    service.start_session(first, {root})
    service._queue(FilesystemEventKind.MODIFIED, path)
    service.start_session(second, {root})
    assert service.pending_count == 0 and service.session == second
    service.shutdown()
    assert not service._debounce.isActive() and not service._poll.isActive()


def test_controller_rejects_old_generation_batch_and_replaces_root_session(tmp_path):
    app(); first_root = tmp_path / "first"; second_root = tmp_path / "second"
    first_root.mkdir(); second_root.mkdir()
    controller = MetadataController()
    controller.workspace_state.set_tracks([])
    controller._start_watch_session(ScanResult(first_root, [], folder_set={first_root}))
    first_session = controller.watch_session
    stale = type("Batch", (), {
        "session_id": first_session.session_id,
        "generation": first_session.generation - 1,
        "events": (), "overflowed": False,
    })()
    controller._on_filesystem_batch(stale)
    assert controller._current_refresh_worker is None
    controller._stop_watch_session()
    controller.workspace_state.set_tracks([])
    controller._start_watch_session(ScanResult(second_root, [], folder_set={second_root}))
    assert controller.watch_session.session_id != first_session.session_id
    assert controller.watch_session.root == second_root.resolve()
    controller._stop_watch_session(); controller.deleteLater()


class _LivingWorker(QObject):
    finished = Signal()

    def __init__(self):
        super().__init__(); self.running = True; self.cancelled = False

    def isRunning(self):
        return self.running

    def cancel(self):
        self.cancelled = True


def test_controller_shutdown_cancels_and_retains_refresh_worker_until_finished():
    app(); controller = MetadataController(); worker = _LivingWorker()
    controller._refresh_workers.add(worker); controller._current_refresh_worker = worker
    ready = []; controller.shutdown_ready.connect(lambda: ready.append(True))
    assert not controller.request_shutdown() and worker.cancelled
    assert worker in controller._refresh_workers
    worker.running = False
    controller._on_filesystem_refresh_finished(worker)
    assert worker not in controller._refresh_workers and ready
    controller.deleteLater()
