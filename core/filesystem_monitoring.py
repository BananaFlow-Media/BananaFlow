"""Qt-free filesystem monitoring contracts for the Tag Editor.

Watcher strings are advisory.  This module normalizes them against current
filesystem evidence, bounds noisy queues, and correlates expected application
operations without ever mutating a workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Iterable

from core.change_sets import FileIdentity


class FilesystemEventKind(str, Enum):
    CREATED = "created"
    REMOVED = "removed"
    MODIFIED = "modified"
    RELOCATED = "relocated"
    DIRECTORY_CREATED = "directory_created"
    DIRECTORY_REMOVED = "directory_removed"
    OVERFLOW_UNKNOWN = "overflow_unknown"
    ROOT_LOST = "root_lost"
    ROOT_RESTORED = "root_restored"
    MANUAL_REFRESH = "manual_refresh"


class ExternalChangeState(str, Enum):
    CURRENT = "current"
    REFRESHING = "refreshing"
    CHANGED_ON_DISK = "changed_on_disk"
    STALE_WITH_PROPOSALS = "stale_with_proposals"
    MISSING = "missing"
    MOVED = "moved"
    REPLACED = "replaced"
    UNREADABLE = "unreadable"
    CLOUD_PLACEHOLDER = "cloud_placeholder"
    CONFLICT = "conflict"
    IGNORED_OWN_OPERATION = "ignored_own_operation"
    ROOT_UNAVAILABLE = "root_unavailable"


#: States needing no user attention.  Everything else is an external change and
#: must appear in the count, in the filtered rows and in the localized label.
SETTLED_EXTERNAL_STATES = frozenset({
    ExternalChangeState.CURRENT.value,
    ExternalChangeState.IGNORED_OWN_OPERATION.value,
})

#: States that must block an *included* changed item from Apply.  An explicitly
#: excluded item is out of the batch already and never consults this set.
APPLY_BLOCKING_EXTERNAL_STATES = frozenset({
    ExternalChangeState.REFRESHING.value,
    ExternalChangeState.STALE_WITH_PROPOSALS.value,
    ExternalChangeState.MISSING.value,
    ExternalChangeState.MOVED.value,
    ExternalChangeState.REPLACED.value,
    ExternalChangeState.UNREADABLE.value,
    ExternalChangeState.CLOUD_PLACEHOLDER.value,
    ExternalChangeState.CONFLICT.value,
    ExternalChangeState.ROOT_UNAVAILABLE.value,
})


def external_state_value(state) -> str:
    """Normalize an enum or a stored string into one comparable state value."""
    value = getattr(state, "value", state)
    return str(value) if value else ExternalChangeState.CURRENT.value


def is_external_change(state) -> bool:
    """The single 'external change' predicate for count, filter and label.

    Count and filter must describe the same set, so both call this.  A visible
    count the filter then hides is a defect, not a display choice.
    """
    return external_state_value(state) not in SETTLED_EXTERNAL_STATES


def external_state_blocks_apply(state) -> bool:
    """The single Apply-blocker predicate for scope, preflight, review and UI."""
    return external_state_value(state) in APPLY_BLOCKING_EXTERNAL_STATES


class MonitoringState(str, Enum):
    DISABLED = "disabled"
    ACTIVE = "active"
    REFRESHING = "refreshing"
    PAUSED = "paused"
    DEGRADED = "degraded"
    ROOT_LOST = "root_lost"


class OwnOperationMatch(str, Enum):
    NONE = "none"
    WAIT_FOR_RESULT = "wait_for_result"
    EXPECTED = "expected"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class WatchRootSession:
    session_id: str
    root: Path
    generation: int
    content_revision: int
    recursive: bool = True

    @classmethod
    def create(cls, root: Path, generation: int, content_revision: int,
               recursive: bool = True) -> "WatchRootSession":
        return cls(uuid.uuid4().hex, Path(root).resolve(), int(generation),
                   int(content_revision), bool(recursive))


@dataclass(frozen=True)
class FilesystemEvidence:
    path: Path
    exists: bool
    is_directory: bool = False
    identity: FileIdentity | None = None
    placeholder: bool = False
    unreadable: bool = False
    outside_root: bool = False
    is_symlink: bool = False
    diagnostic: str = ""


@dataclass(frozen=True)
class FilesystemEvent:
    session_id: str
    generation: int
    sequence: int
    kind: FilesystemEventKind
    path: Path
    prior_path: Path | None = None
    is_directory_hint: bool = False
    observed_monotonic: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class NormalizedFilesystemEvent:
    session_id: str
    generation: int
    sequence: int
    kind: FilesystemEventKind
    path: Path
    prior_path: Path | None
    evidence: FilesystemEvidence
    prior_evidence: FilesystemEvidence | None = None


@dataclass(frozen=True)
class FilesystemEventBatch:
    session_id: str
    generation: int
    events: tuple[NormalizedFilesystemEvent, ...]
    overflowed: bool = False


@dataclass(frozen=True)
class OwnOperationEvidence:
    """What the application actually did to one path, proven after the fact.

    ``expected_final_identity`` is only trustworthy once ``final_state_recorded``
    is true: it is then the identity observed *after* the canonical terminal
    outcome, not a guess made before the work started.
    """

    operation_id: str
    operation_type: str
    original_path: Path
    final_path: Path
    original_identity: FileIdentity | None
    expected_final_identity: FileIdentity | None
    expected_kinds: frozenset[FilesystemEventKind]
    started_monotonic: float
    terminal_monotonic: float | None = None
    terminal_result: str = "pending"
    expected_absence: bool = False
    final_state_recorded: bool = False


_WINDOWS_PLACEHOLDER_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000


def _canonical(path: Path) -> Path:
    return Path(os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path)))))


def _absolute(path: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(os.fspath(path))))


def path_within_root(root: Path, path: Path) -> bool:
    root_value, path_value = _canonical(root), _canonical(path)
    try:
        return os.path.commonpath((os.fspath(root_value), os.fspath(path_value))) == os.fspath(root_value)
    except ValueError:
        return False


def strong_same_file(left: FileIdentity | None, right: FileIdentity | None) -> bool:
    """Return True only when platform evidence strongly identifies one file."""
    if left is None or right is None:
        return False
    return bool(
        left.device is not None and right.device is not None
        and left.device == right.device and left.inode and right.inode
        and left.inode == right.inode
    )


def equivalent_file_identity(left: FileIdentity | None,
                             right: FileIdentity | None) -> bool:
    """Compare one observation without treating its mutable path as identity."""
    if left is None or right is None:
        return left is right
    if strong_same_file(left, right):
        return left.size == right.size and left.mtime_ns == right.mtime_ns
    return (
        left.size == right.size and left.mtime_ns == right.mtime_ns
        and (not left.device or not right.device or left.device == right.device)
        and (not left.inode or not right.inode or left.inode == right.inode)
    )


def inspect_filesystem_path(root: Path, path: Path) -> FilesystemEvidence:
    """Inspect a path without opening file content or hydrating placeholders."""
    root, path = Path(root).resolve(), _absolute(Path(path))
    if not path_within_root(root, path):
        return FilesystemEvidence(path, False, outside_root=True,
                                  diagnostic="outside_root")
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return FilesystemEvidence(path, False)
    except (OSError, PermissionError):
        return FilesystemEvidence(path, True, unreadable=True,
                                  diagnostic="stat_failed")
    is_symlink = path.is_symlink()
    if is_symlink:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return FilesystemEvidence(path, True, unreadable=True,
                                      is_symlink=True, diagnostic="broken_symlink")
        if not path_within_root(root, resolved):
            return FilesystemEvidence(path, True, outside_root=True,
                                      is_symlink=True, diagnostic="symlink_escape")
    attributes = int(getattr(stat, "st_file_attributes", 0) or 0)
    placeholder = bool(attributes & _WINDOWS_PLACEHOLDER_ATTRIBUTES)
    is_directory = bool(os.path.isdir(path)) if not placeholder else False
    identity = None if is_directory else FileIdentity(
        str(path), int(stat.st_size), int(stat.st_mtime_ns),
        getattr(stat, "st_dev", None), getattr(stat, "st_ino", None),
    )
    return FilesystemEvidence(path, True, is_directory, identity, placeholder,
                              False, False, is_symlink)


def normalize_filesystem_event(root: Path,
                               event: FilesystemEvent) -> NormalizedFilesystemEvent:
    evidence = inspect_filesystem_path(root, event.path)
    prior = (inspect_filesystem_path(root, event.prior_path)
             if event.prior_path is not None else None)
    kind = event.kind
    if _canonical(event.path) == _canonical(root):
        if not evidence.exists:
            kind = FilesystemEventKind.ROOT_LOST
        elif event.kind is FilesystemEventKind.ROOT_LOST:
            kind = FilesystemEventKind.ROOT_RESTORED
    elif evidence.outside_root:
        kind = FilesystemEventKind.OVERFLOW_UNKNOWN
    elif kind in {FilesystemEventKind.MODIFIED, FilesystemEventKind.CREATED} and not evidence.exists:
        kind = FilesystemEventKind.REMOVED
    elif kind is FilesystemEventKind.REMOVED and evidence.exists:
        kind = (FilesystemEventKind.DIRECTORY_CREATED if evidence.is_directory
                else FilesystemEventKind.CREATED)
    if (event.is_directory_hint or evidence.is_directory) and kind in {
            FilesystemEventKind.CREATED, FilesystemEventKind.REMOVED}:
        if kind is FilesystemEventKind.CREATED:
            kind = FilesystemEventKind.DIRECTORY_CREATED
        elif kind is FilesystemEventKind.REMOVED:
            kind = FilesystemEventKind.DIRECTORY_REMOVED
    return NormalizedFilesystemEvent(
        event.session_id, event.generation, event.sequence, kind,
        evidence.path, _absolute(event.prior_path) if event.prior_path else None,
        evidence, prior,
    )


class EventCoalescer:
    """Deterministic bounded path-event coalescer."""

    def __init__(self, maximum_paths: int = 4096) -> None:
        self.maximum_paths = max(1, int(maximum_paths))
        self._events: dict[tuple[str, Path], NormalizedFilesystemEvent] = {}
        self._root_event: NormalizedFilesystemEvent | None = None
        self._overflowed = False

    @property
    def pending_count(self) -> int:
        return 1 if self._root_event is not None else len(self._events)

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def clear(self) -> None:
        self._events.clear()
        self._root_event = None
        self._overflowed = False

    def add(self, event: NormalizedFilesystemEvent) -> None:
        if event.kind in {FilesystemEventKind.ROOT_LOST, FilesystemEventKind.ROOT_RESTORED}:
            self._root_event = event
            return
        if self._root_event is not None:
            return
        key = (event.session_id, event.path)
        prior = self._events.get(key)
        if prior is None and len(self._events) >= self.maximum_paths:
            self._events.clear()
            self._overflowed = True
            self._root_event = replace(event, kind=FilesystemEventKind.OVERFLOW_UNKNOWN)
            return
        self._events[key] = self._merge(prior, event) if prior else event

    def extend(self, events: Iterable[NormalizedFilesystemEvent]) -> None:
        for event in sorted(events, key=lambda value: value.sequence):
            self.add(event)

    def drain(self) -> FilesystemEventBatch | None:
        if self._root_event is not None:
            events = (self._root_event,)
        else:
            events = tuple(sorted(self._events.values(),
                                  key=lambda value: (value.sequence, os.fspath(value.path))))
        if not events:
            return None
        batch = FilesystemEventBatch(events[0].session_id, events[0].generation,
                                     events, self._overflowed)
        self.clear()
        return batch

    @staticmethod
    def _merge(prior: NormalizedFilesystemEvent,
               current: NormalizedFilesystemEvent) -> NormalizedFilesystemEvent:
        if current.kind in {
            FilesystemEventKind.REMOVED, FilesystemEventKind.DIRECTORY_REMOVED,
            FilesystemEventKind.ROOT_LOST,
        }:
            return current
        if prior.kind in {FilesystemEventKind.REMOVED, FilesystemEventKind.DIRECTORY_REMOVED}:
            # A reappearance at one path is a replacement candidate, never a
            # rename or harmless modify without authoritative identity.
            return replace(current, kind=(FilesystemEventKind.DIRECTORY_CREATED
                                          if current.evidence.is_directory
                                          else FilesystemEventKind.CREATED))
        if prior.kind in {FilesystemEventKind.CREATED, FilesystemEventKind.DIRECTORY_CREATED}:
            return replace(prior, sequence=current.sequence, evidence=current.evidence)
        if (prior.kind is FilesystemEventKind.MODIFIED
                and current.kind is FilesystemEventKind.MODIFIED):
            return current
        if current.kind is FilesystemEventKind.OVERFLOW_UNKNOWN:
            return current
        return current


class OwnOperationLedger:
    """Bounded evidence ledger for application-originated filesystem changes.

    Suppression requires proof, never coincidence.  An event is only expected
    when the application recorded the real terminal state of that exact path
    and the event's current filesystem evidence still matches it.  A path and
    event kind that merely resemble a recent operation are reconciled instead.
    """

    def __init__(self, maximum_entries: int = 1024, retention_seconds: float = 120.0) -> None:
        self.maximum_entries = max(1, int(maximum_entries))
        self.retention_seconds = max(1.0, float(retention_seconds))
        self._entries: dict[str, OwnOperationEvidence] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(operation_id: str, original_path: Path) -> str:
        return f"{operation_id}|{os.fspath(_canonical(original_path))}"

    def begin(self, operation_id: str, operation_type: str, original_path: Path,
              final_path: Path, *, original_identity: FileIdentity | None = None,
              expected_final_identity: FileIdentity | None = None,
              expected_kinds: Iterable[FilesystemEventKind] = ()) -> OwnOperationEvidence:
        """Register intent before the disk is touched.  Intent never suppresses."""
        entry = OwnOperationEvidence(
            str(operation_id), str(operation_type), _canonical(original_path),
            _canonical(final_path), original_identity, expected_final_identity,
            frozenset(expected_kinds), time.monotonic(),
        )
        with self._lock:
            self._prune_locked()
            if len(self._entries) >= self.maximum_entries:
                oldest_key = min(self._entries,
                                 key=lambda key: self._entries[key].started_monotonic)
                self._entries.pop(oldest_key, None)
            self._entries[self._key(entry.operation_id, entry.original_path)] = entry
        return entry

    def record_final_state(self, operation_id: str, original_path: Path, *,
                           terminal_result: str, final_path: Path | None = None,
                           final_identity: FileIdentity | None = None,
                           expected_absence: bool = False) -> OwnOperationEvidence | None:
        """Bind one entry to the identity (or proven absence) it really reached.

        Only a successful or partially successful path may present terminal
        evidence; anything else stays unproven so a later event is reconciled.
        """
        with self._lock:
            key = self._key(str(operation_id), Path(original_path))
            entry = self._entries.get(key)
            if entry is None:
                return None
            proven = (str(terminal_result) in {"success", "partial"}
                      and (expected_absence or final_identity is not None))
            updated = replace(
                entry,
                final_path=_canonical(final_path) if final_path is not None else entry.final_path,
                expected_final_identity=final_identity,
                expected_absence=bool(expected_absence),
                final_state_recorded=proven,
                terminal_result=str(terminal_result),
                terminal_monotonic=time.monotonic(),
            )
            self._entries[key] = updated
            return updated

    def complete(self, operation_id: str, terminal_result: str,
                 *, final_identity: FileIdentity | None = None) -> OwnOperationEvidence | None:
        """Record the batch terminal outcome for every entry of one operation.

        This closes the operation but deliberately proves nothing on its own:
        per-path evidence comes from ``record_final_state``.
        """
        with self._lock:
            matches = [(key, value) for key, value in self._entries.items()
                       if value.operation_id == str(operation_id)]
            updated = None
            for key, entry in matches:
                proven = entry.final_state_recorded
                identity = entry.expected_final_identity
                if final_identity is not None and len(matches) == 1:
                    identity = final_identity
                    proven = str(terminal_result) in {"success", "partial"}
                updated = replace(
                    entry, terminal_monotonic=time.monotonic(),
                    terminal_result=str(terminal_result),
                    expected_final_identity=identity,
                    final_state_recorded=proven,
                )
                self._entries[key] = updated
            return updated

    def classify(self, event: NormalizedFilesystemEvent) -> OwnOperationMatch:
        with self._lock:
            self._prune_locked()
            candidates = [entry for entry in self._entries.values()
                          if event.path in {entry.original_path, entry.final_path}
                          or event.prior_path in {entry.original_path, entry.final_path}]
        if not candidates:
            return OwnOperationMatch.NONE
        candidates.sort(key=lambda value: value.started_monotonic, reverse=True)
        entry = candidates[0]
        if entry.expected_kinds and event.kind not in entry.expected_kinds:
            return OwnOperationMatch.UNEXPECTED
        if entry.terminal_result == "pending":
            return OwnOperationMatch.WAIT_FOR_RESULT
        if entry.terminal_result not in {"success", "partial"}:
            return OwnOperationMatch.UNEXPECTED
        if not entry.final_state_recorded:
            # The operation finished but never proved what it left on disk.
            # Reconcile authoritatively rather than assume this event was ours.
            return OwnOperationMatch.UNEXPECTED
        path = _canonical(event.path)
        if path == entry.final_path:
            if entry.expected_absence:
                return (OwnOperationMatch.UNEXPECTED if event.evidence.exists
                        else OwnOperationMatch.EXPECTED)
            if (entry.expected_final_identity is None
                    or event.evidence.identity is None
                    or not equivalent_file_identity(entry.expected_final_identity,
                                                    event.evidence.identity)):
                return OwnOperationMatch.UNEXPECTED
            return OwnOperationMatch.EXPECTED
        if path == entry.original_path:
            # The source of a completed relocation must now be empty; a file
            # present there again is a new external file, not our own work.
            return (OwnOperationMatch.UNEXPECTED if event.evidence.exists
                    else OwnOperationMatch.EXPECTED)
        return OwnOperationMatch.UNEXPECTED

    def entries(self) -> tuple[OwnOperationEvidence, ...]:
        with self._lock:
            self._prune_locked()
            return tuple(sorted(self._entries.values(),
                                key=lambda value: value.started_monotonic))

    def _prune_locked(self) -> None:
        cutoff = time.monotonic() - self.retention_seconds
        stale = [key for key, value in self._entries.items()
                 if value.terminal_monotonic is not None
                 and value.terminal_monotonic < cutoff]
        for key in stale:
            self._entries.pop(key, None)
