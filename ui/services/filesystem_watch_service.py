"""Qt-native advisory directory watcher for the active Tag Editor root."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from core.filesystem_monitoring import (
    EventCoalescer, FilesystemEvent, FilesystemEventKind, MonitoringState,
    WatchRootSession, normalize_filesystem_event,
)


@dataclass(frozen=True)
class _DirectoryEntry:
    path: Path
    is_directory: bool
    size: int
    mtime_ns: int
    device: int | None
    inode: int | None


class FilesystemWatchService(QObject):
    """Manage explicit recursive watches and emit bounded normalized batches."""

    batch_ready = Signal(object)
    state_changed = Signal(object, str)  # MonitoringState, diagnostic
    watched_directories_changed = Signal(object)

    def __init__(self, parent=None, *, debounce_ms: int = 250,
                 maximum_delay_ms: int = 1000, maximum_paths: int = 4096,
                 degraded_poll_ms: int = 30000) -> None:
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._debounce = QTimer(self); self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._flush)
        self._poll = QTimer(self); self._poll.timeout.connect(self._poll_all)
        self._debounce_ms = max(1, int(debounce_ms))
        self._maximum_delay_ms = max(self._debounce_ms, int(maximum_delay_ms))
        self._degraded_poll_ms = max(1000, int(degraded_poll_ms))
        self._coalescer = EventCoalescer(maximum_paths)
        self._session: WatchRootSession | None = None
        self._snapshots: dict[Path, dict[str, _DirectoryEntry]] = {}
        self._sequence = 0
        self._burst_started: float | None = None
        self._paused = False
        self._state = MonitoringState.DISABLED

    @property
    def session(self) -> WatchRootSession | None:
        return self._session

    @property
    def state(self) -> MonitoringState:
        return self._state

    @property
    def pending_count(self) -> int:
        return self._coalescer.pending_count

    @property
    def watched_directories(self) -> tuple[Path, ...]:
        return tuple(sorted(self._snapshots, key=lambda value: os.fspath(value).casefold()))

    def start_session(self, session: WatchRootSession,
                      directories=()) -> None:
        self.stop_session()
        self._session = session
        requested = {Path(session.root), *(Path(value) for value in directories)}
        requested = {path for path in requested if path.is_dir()}
        self.update_directories(requested)
        if not self._snapshots:
            self._set_state(MonitoringState.DEGRADED, "watch_registration_failed")
            self._poll.start(self._degraded_poll_ms)
        else:
            self._set_state(MonitoringState.ACTIVE, "")

    def stop_session(self) -> None:
        self._debounce.stop(); self._poll.stop()
        watched = self._watcher.directories() + self._watcher.files()
        if watched:
            self._watcher.removePaths(watched)
        self._coalescer.clear()
        self._snapshots.clear()
        self._session = None
        self._burst_started = None
        self._paused = False
        self._set_state(MonitoringState.DISABLED, "")

    def shutdown(self) -> None:
        self.stop_session()

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if self._session is None:
            return
        self._set_state(
            MonitoringState.PAUSED if self._paused else MonitoringState.ACTIVE, "")
        if not self._paused and self._coalescer.pending_count:
            self._arm_debounce()

    def update_directories(self, directories) -> None:
        if self._session is None:
            return
        requested = {Path(value) for value in directories if Path(value).is_dir()}
        current = set(self._snapshots)
        removed = current - requested
        if removed:
            self._watcher.removePaths([str(value) for value in removed])
            for path in removed:
                self._snapshots.pop(path, None)
        added = sorted(requested - current, key=lambda value: os.fspath(value).casefold())
        failures: list[Path] = []
        if added:
            failed_text = set(self._watcher.addPaths([str(value) for value in added]))
            for path in added:
                if str(path) in failed_text:
                    failures.append(path)
                    self._snapshots[path] = self._snapshot_directory(path)
                else:
                    self._snapshots[path] = self._snapshot_directory(path)
        self.watched_directories_changed.emit(self.watched_directories)
        if failures:
            self._poll.start(self._degraded_poll_ms)
            self._set_state(MonitoringState.DEGRADED, "watch_limit_or_resource_error")

    def manual_refresh(self) -> None:
        session = self._session
        if session is None:
            return
        self._queue(FilesystemEventKind.MANUAL_REFRESH, session.root)
        self._flush()

    def _on_directory_changed(self, raw_path: str) -> None:
        path = Path(raw_path)
        session = self._session
        if session is None:
            return
        if not path.exists():
            kind = (FilesystemEventKind.ROOT_LOST if path == session.root
                    else FilesystemEventKind.DIRECTORY_REMOVED)
            self._queue(kind, path, is_directory=True)
            self._snapshots.pop(path, None)
            if path == session.root:
                self._set_state(MonitoringState.ROOT_LOST, "root_lost")
                self._poll.start(self._degraded_poll_ms)
            return
        old = self._snapshots.get(path, {})
        new = self._snapshot_directory(path)
        self._snapshots[path] = new
        removed = set(old) - set(new)
        created = set(new) - set(old)

        # Strong directory-entry identity pairs become one relocation hint.
        paired_removed: set[str] = set()
        paired_created: set[str] = set()
        for old_name in sorted(removed, key=str.casefold):
            old_entry = old[old_name]
            matches = [name for name in created
                       if self._same_entry_identity(old_entry, new[name])]
            if len(matches) == 1:
                name = matches[0]
                self._queue(FilesystemEventKind.RELOCATED, new[name].path,
                            prior_path=old_entry.path,
                            is_directory=new[name].is_directory)
                paired_removed.add(old_name); paired_created.add(name)
        for name in sorted(removed - paired_removed, key=str.casefold):
            entry = old[name]
            self._queue(FilesystemEventKind.REMOVED, entry.path,
                        is_directory=entry.is_directory)
        for name in sorted(created - paired_created, key=str.casefold):
            entry = new[name]
            self._queue(FilesystemEventKind.CREATED, entry.path,
                        is_directory=entry.is_directory)
        for name in sorted(set(old) & set(new), key=str.casefold):
            if old[name] != new[name]:
                self._queue(FilesystemEventKind.MODIFIED, new[name].path,
                            is_directory=new[name].is_directory)
        if not removed and not created and old == new:
            # The backend may report a directory without observable entry
            # deltas. Reconcile that directory authoritatively, still bounded.
            self._queue(FilesystemEventKind.MODIFIED, path, is_directory=True)

    def _on_file_changed(self, raw_path: str) -> None:
        self._queue(FilesystemEventKind.MODIFIED, Path(raw_path))

    def _poll_all(self) -> None:
        session = self._session
        if session is None:
            return
        if not session.root.exists():
            self._queue(FilesystemEventKind.ROOT_LOST, session.root, is_directory=True)
        else:
            if session.root not in self._snapshots:
                self._queue(FilesystemEventKind.ROOT_RESTORED, session.root,
                            is_directory=True)
                self.update_directories({session.root})
                self._set_state(MonitoringState.ACTIVE, "")
            for path in tuple(self._snapshots):
                self._on_directory_changed(str(path))

    def _queue(self, kind: FilesystemEventKind, path: Path, *,
               prior_path: Path | None = None, is_directory: bool = False) -> None:
        session = self._session
        if session is None:
            return
        self._sequence += 1
        raw = FilesystemEvent(
            session.session_id, session.generation, self._sequence, kind,
            Path(path), Path(prior_path) if prior_path else None, is_directory)
        self._coalescer.add(normalize_filesystem_event(session.root, raw))
        if self._burst_started is None:
            self._burst_started = time.monotonic()
        if not self._paused:
            self._arm_debounce()

    def _arm_debounce(self) -> None:
        elapsed_ms = int((time.monotonic() - (self._burst_started or time.monotonic())) * 1000)
        remaining = max(1, self._maximum_delay_ms - elapsed_ms)
        self._debounce.start(min(self._debounce_ms, remaining))

    def _flush(self) -> None:
        if self._paused:
            return
        batch = self._coalescer.drain()
        self._burst_started = None
        if batch is not None:
            self.batch_ready.emit(batch)

    def _snapshot_directory(self, path: Path) -> dict[str, _DirectoryEntry]:
        values: dict[str, _DirectoryEntry] = {}
        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
            for entry in entries:
                try:
                    stat = entry.stat(follow_symlinks=False)
                    values[entry.name] = _DirectoryEntry(
                        Path(entry.path), entry.is_dir(follow_symlinks=False),
                        int(stat.st_size), int(stat.st_mtime_ns),
                        getattr(stat, "st_dev", None), getattr(stat, "st_ino", None))
                except OSError:
                    continue
        except OSError:
            self._set_state(MonitoringState.DEGRADED, "directory_snapshot_failed")
            self._poll.start(self._degraded_poll_ms)
        return values

    @staticmethod
    def _same_entry_identity(left: _DirectoryEntry, right: _DirectoryEntry) -> bool:
        return bool(left.device is not None and right.device is not None
                    and left.device == right.device and left.inode and right.inode
                    and left.inode == right.inode
                    and left.is_directory == right.is_directory)

    def _set_state(self, state: MonitoringState, diagnostic: str) -> None:
        if self._state == state and not diagnostic:
            return
        self._state = state
        self.state_changed.emit(state, diagnostic)
