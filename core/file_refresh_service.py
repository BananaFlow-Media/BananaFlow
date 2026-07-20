"""Authoritative Qt-free refresh and external-conflict planning."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import os
from pathlib import Path
from typing import Iterable

from core.change_sets import FileIdentity
from core.filesystem_monitoring import (
    ExternalChangeState, FilesystemEventBatch, FilesystemEventKind,
    FilesystemEvidence, NormalizedFilesystemEvent, inspect_filesystem_path,
    path_within_root, strong_same_file,
)
from core.metadata_io import CancellationToken
from core.metadata_models import (
    AudioTrackItem, MetadataField, OriginalTags, ProposedTags,
    metadata_values_equal,
)
from core.metadata_processor import (
    build_track_item, collect_scan_targets, is_audio_candidate,
)


class RefreshAction(str, Enum):
    NO_CHANGE = "no_change"
    ADD = "add"
    REMOVE = "remove"
    UPDATE = "update"
    RELOCATE = "relocate"
    MARK_STATE = "mark_state"
    CONFLICT = "conflict"


class ConflictResolutionAction(str, Enum):
    REVIEW = "review"
    RELOAD = "reload"
    KEEP_LOCAL = "keep_local"
    REMOVE_MISSING = "remove_missing"
    LOCATE_MOVED = "locate_moved"
    CANCEL = "cancel"


@dataclass(frozen=True)
class TrackedFileSnapshot:
    item_id: int
    path: Path
    baseline_identity: FileIdentity | None
    original: OriginalTags
    proposed: ProposedTags
    proposed_filename: str | None
    excluded_from_apply: bool
    format_id: str
    metadata_editable: bool
    external_state: str = "current"

    @property
    def has_proposals(self) -> bool:
        return bool(self.proposed_filename is not None
                    or self.proposed.has_changes(self.original))


@dataclass(frozen=True)
class FieldDifference:
    field: str
    baseline_value: object
    disk_value: object
    local_value: object
    changed_externally: bool
    changed_locally: bool
    overlap: bool


@dataclass(frozen=True)
class StaleFileConflict:
    conflict_id: str
    item_id: int
    path: Path
    observed_path: Path
    state: ExternalChangeState
    baseline_identity: FileIdentity | None
    disk_identity: FileIdentity | None
    disk_item: AudioTrackItem | None
    differences: tuple[FieldDifference, ...]
    safe_rebase: bool
    generation: int
    content_revision: int
    change_revision: int
    diagnostic: str = ""
    session_id: str = ""

    @property
    def overlap_fields(self) -> tuple[str, ...]:
        return tuple(value.field for value in self.differences if value.overlap)


@dataclass(frozen=True)
class FileRefreshResult:
    action: RefreshAction
    state: ExternalChangeState
    path: Path
    item_id: int | None = None
    prior_path: Path | None = None
    evidence: FilesystemEvidence | None = None
    refreshed_item: AudioTrackItem | None = None
    conflict: StaleFileConflict | None = None
    diagnostic: str = ""


@dataclass(frozen=True)
class WorkspaceRefreshPlan:
    session_id: str
    generation: int
    content_revision: int
    change_revision: int
    results: tuple[FileRefreshResult, ...]
    watched_directories: tuple[Path, ...] = ()
    overflow_reconciled: bool = False
    cancelled: bool = False
    root_lost: bool = False

    @property
    def changed_count(self) -> int:
        return sum(result.action is not RefreshAction.NO_CHANGE for result in self.results)


@dataclass(frozen=True)
class ConflictResolutionPlan:
    action: ConflictResolutionAction
    conflict_id: str
    item_id: int
    generation: int
    content_revision: int
    change_revision: int
    disk_identity: FileIdentity | None
    disk_item: AudioTrackItem | None
    target_path: Path | None = None
    accepted: bool = False
    diagnostic: str = ""


def snapshot_workspace_files(workspace) -> tuple[TrackedFileSnapshot, ...]:
    """Detach worker input from the mutable QObject-backed workspace."""
    values = []
    for item in workspace.tracks:
        values.append(TrackedFileSnapshot(
            workspace.item_id(item), Path(item.path), deepcopy(item.baseline_identity),
            deepcopy(item.original), deepcopy(item.proposed), item.proposed_filename,
            bool(item.excluded_from_apply), str(item.format_id), bool(item.metadata_editable),
            str(getattr(item, "external_state", "current")),
        ))
    return tuple(values)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _identities_equal(left: FileIdentity | None, right: FileIdentity | None) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.size == right.size and left.mtime_ns == right.mtime_ns
        and (not left.device or not right.device or left.device == right.device)
        and (not left.inode or not right.inode or left.inode == right.inode)
    )


def _check_cancel(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.raise_if_cancelled()


def _file_in_scope(root: Path, path: Path, recursive: bool) -> bool:
    """Whether a file may enter a session that started with this scan scope."""
    if not path_within_root(root, path):
        return False
    return recursive or _path_key(Path(path).parent) == _path_key(root)


def _directory_in_scope(root: Path, path: Path, recursive: bool) -> bool:
    """A non-recursive session watches and reconciles the root directory only."""
    if not path_within_root(root, path):
        return False
    return recursive or _path_key(path) == _path_key(root)


def _conflict_id(item_id: int, baseline: FileIdentity | None,
                 disk: FileIdentity | None, differences: Iterable[FieldDifference],
                 generation: int, content_revision: int, change_revision: int) -> str:
    payload = repr((item_id, baseline, disk, tuple(differences), generation,
                    content_revision, change_revision))
    return hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()[:24]


def analyze_stale_file(snapshot: TrackedFileSnapshot, disk_item: AudioTrackItem | None,
                       disk_identity: FileIdentity | None, *, observed_path: Path,
                       state: ExternalChangeState, generation: int,
                       content_revision: int, change_revision: int,
                       diagnostic: str = "", session_id: str = "") -> StaleFileConflict:
    fields: list[FieldDifference] = []
    if disk_item is not None:
        effective = snapshot.proposed.effective_tags(snapshot.original)
        changed_local = snapshot.proposed.changed_fields(snapshot.original)
        for metadata_field in MetadataField:
            field_name = metadata_field.value
            baseline_value = snapshot.original.field_value(field_name)
            disk_value = disk_item.original.field_value(field_name)
            local_value = effective.field_value(field_name)
            external = not metadata_values_equal(field_name, baseline_value, disk_value)
            local = field_name in changed_local
            if external or local:
                fields.append(FieldDifference(
                    field_name, deepcopy(baseline_value), deepcopy(disk_value),
                    deepcopy(local_value), external, local, external and local,
                ))
        filename_external = _path_key(snapshot.path) != _path_key(observed_path)
        filename_local = snapshot.proposed_filename is not None
        if filename_external or filename_local:
            fields.append(FieldDifference(
                "filename", snapshot.path.name, observed_path.name,
                snapshot.proposed_filename or observed_path.name,
                filename_external, filename_local,
                filename_external and filename_local
                and snapshot.proposed_filename != observed_path.name,
            ))
    safe = bool(
        disk_item is not None
        and state not in {ExternalChangeState.REPLACED, ExternalChangeState.MISSING,
                          ExternalChangeState.UNREADABLE, ExternalChangeState.CLOUD_PLACEHOLDER}
        and not any(value.overlap for value in fields)
    )
    conflict_id = _conflict_id(snapshot.item_id, snapshot.baseline_identity,
                               disk_identity, fields, generation,
                               content_revision, change_revision)
    return StaleFileConflict(
        conflict_id, snapshot.item_id, snapshot.path, Path(observed_path), state,
        snapshot.baseline_identity, disk_identity, disk_item, tuple(fields), safe,
        generation, content_revision, change_revision, diagnostic,
        session_id,
    )


class FileRefreshService:
    """Build authoritative immutable workspace plans from advisory batches."""

    def build_plan(self, *, root: Path, batch: FilesystemEventBatch,
                   generation: int, content_revision: int, change_revision: int,
                   tracked: Iterable[TrackedFileSnapshot], recursive: bool = True,
                   cancellation: CancellationToken | None = None) -> WorkspaceRefreshPlan:
        root = Path(root).resolve()
        snapshots = tuple(tracked)
        known_by_path = {_path_key(value.path): value for value in snapshots}
        _check_cancel(cancellation)

        if any(event.kind is FilesystemEventKind.ROOT_LOST for event in batch.events):
            return self._root_lost_plan(batch, snapshots, generation,
                                        content_revision, change_revision)

        full = batch.overflowed or any(event.kind in {
            FilesystemEventKind.OVERFLOW_UNKNOWN, FilesystemEventKind.ROOT_RESTORED,
            FilesystemEventKind.MANUAL_REFRESH,
        } and _path_key(event.path) == _path_key(root) for event in batch.events)
        candidate_paths: set[Path] = set()
        watched_directories: set[Path] = set()
        affected_known: set[str] = set()

        if full:
            files, folders, _ = collect_scan_targets(root, recursive)
            candidate_paths.update(files)
            watched_directories.update(folders)
            affected_known.update(known_by_path)
        else:
            for event in batch.events:
                _check_cancel(cancellation)
                path = Path(event.path)
                is_directory = bool(
                    event.evidence.is_directory or event.kind in {
                        FilesystemEventKind.DIRECTORY_CREATED,
                        FilesystemEventKind.DIRECTORY_REMOVED,
                    }
                )
                if is_directory:
                    watched_directories.add(path)
                    if event.kind is FilesystemEventKind.DIRECTORY_REMOVED:
                        prefix = _path_key(path) + os.sep
                        affected_known.update(key for key in known_by_path
                                              if key == _path_key(path) or key.startswith(prefix))
                    elif (event.evidence.exists
                            and _directory_in_scope(root, path, recursive)):
                        # A non-recursive session owns the root directory only,
                        # so enumerating a child folder would only produce
                        # candidates the scope filter below has to throw away.
                        files, folders, _ = collect_scan_targets(
                            path, recursive=(recursive and event.kind
                                             is FilesystemEventKind.DIRECTORY_CREATED))
                        candidate_paths.update(files)
                        watched_directories.update(folders)
                        affected_known.update(
                            key for key, value in known_by_path.items()
                            if value.path.parent == path)
                    continue
                candidate_paths.add(path)
                affected_known.add(_path_key(path))
                if event.prior_path is not None:
                    affected_known.add(_path_key(event.prior_path))

        # The original scan scope is a session property: no later event may
        # widen it, so a non-recursive session admits direct children only.
        candidate_paths = {path for path in candidate_paths
                           if _file_in_scope(root, path, recursive)}
        evidence_by_path: dict[str, FilesystemEvidence] = {}
        for path in sorted(candidate_paths, key=_path_key):
            _check_cancel(cancellation)
            evidence_by_path[_path_key(path)] = inspect_filesystem_path(root, path)

        missing = [known_by_path[key] for key in sorted(affected_known)
                   if key in known_by_path
                   and (key not in evidence_by_path or not evidence_by_path[key].exists)]
        new_paths = [evidence for key, evidence in evidence_by_path.items()
                     if evidence.exists and not evidence.is_directory
                     and is_audio_candidate(evidence.path) and key not in known_by_path]

        relocations: dict[int, FilesystemEvidence] = {}
        consumed_new: set[str] = set()
        for snapshot in missing:
            matches = [evidence for evidence in new_paths
                       if strong_same_file(snapshot.baseline_identity, evidence.identity)]
            if len(matches) == 1:
                relocations[snapshot.item_id] = matches[0]
                consumed_new.add(_path_key(matches[0].path))

        results: list[FileRefreshResult] = []
        relocated_ids = set(relocations)
        for snapshot in snapshots:
            _check_cancel(cancellation)
            key = _path_key(snapshot.path)
            if snapshot.item_id in relocated_ids:
                evidence = relocations[snapshot.item_id]
                disk_item = self.read_stable_item(evidence, cancellation)
                conflict = None
                state = ExternalChangeState.MOVED
                if snapshot.has_proposals:
                    conflict = analyze_stale_file(
                        snapshot, disk_item, evidence.identity, observed_path=evidence.path,
                        state=state, generation=generation,
                        content_revision=content_revision, change_revision=change_revision,
                        session_id=batch.session_id)
                results.append(FileRefreshResult(
                    RefreshAction.RELOCATE, state, evidence.path, snapshot.item_id,
                    snapshot.path, evidence, disk_item, conflict,
                ))
                continue
            if key not in affected_known:
                continue
            evidence = evidence_by_path.get(key) or inspect_filesystem_path(root, snapshot.path)
            if not evidence.exists:
                if snapshot.has_proposals:
                    conflict = analyze_stale_file(
                        snapshot, None, None, observed_path=snapshot.path,
                        state=ExternalChangeState.MISSING, generation=generation,
                        content_revision=content_revision, change_revision=change_revision,
                        diagnostic="file_missing", session_id=batch.session_id)
                    results.append(FileRefreshResult(
                        RefreshAction.CONFLICT, ExternalChangeState.MISSING,
                        snapshot.path, snapshot.item_id, evidence=evidence,
                        conflict=conflict, diagnostic="file_missing"))
                else:
                    results.append(FileRefreshResult(
                        RefreshAction.REMOVE, ExternalChangeState.MISSING,
                        snapshot.path, snapshot.item_id, evidence=evidence,
                        diagnostic="file_missing"))
                continue
            if evidence.placeholder:
                results.append(FileRefreshResult(
                    RefreshAction.MARK_STATE, ExternalChangeState.CLOUD_PLACEHOLDER,
                    snapshot.path, snapshot.item_id, evidence=evidence,
                    diagnostic="cloud_placeholder"))
                continue
            if evidence.unreadable or evidence.outside_root:
                results.append(FileRefreshResult(
                    RefreshAction.MARK_STATE, ExternalChangeState.UNREADABLE,
                    snapshot.path, snapshot.item_id, evidence=evidence,
                    diagnostic=evidence.diagnostic or "unreadable"))
                continue
            if _identities_equal(snapshot.baseline_identity, evidence.identity):
                if snapshot.external_state not in {
                        ExternalChangeState.CURRENT.value,
                        ExternalChangeState.REFRESHING.value}:
                    results.append(FileRefreshResult(
                        RefreshAction.MARK_STATE, ExternalChangeState.CURRENT,
                        snapshot.path, snapshot.item_id, evidence=evidence))
                continue
            replaced = bool(
                snapshot.baseline_identity is not None and evidence.identity is not None
                and snapshot.baseline_identity.device and snapshot.baseline_identity.inode
                and evidence.identity.device and evidence.identity.inode
                and not strong_same_file(snapshot.baseline_identity, evidence.identity)
            )
            disk_item = self.read_stable_item(evidence, cancellation)
            state = (ExternalChangeState.REPLACED if replaced
                     else ExternalChangeState.CHANGED_ON_DISK)
            if snapshot.has_proposals:
                conflict = analyze_stale_file(
                    snapshot, disk_item, evidence.identity, observed_path=evidence.path,
                    state=state, generation=generation,
                    content_revision=content_revision, change_revision=change_revision,
                    diagnostic="file_replaced" if replaced else "external_change",
                    session_id=batch.session_id)
                conflict_state = (ExternalChangeState.CONFLICT
                                  if conflict.overlap_fields or replaced
                                  else ExternalChangeState.STALE_WITH_PROPOSALS)
                conflict = StaleFileConflict(**{
                    **conflict.__dict__, "state": conflict_state,
                })
                results.append(FileRefreshResult(
                    RefreshAction.CONFLICT, conflict_state, snapshot.path,
                    snapshot.item_id, evidence=evidence, refreshed_item=disk_item,
                    conflict=conflict, diagnostic=conflict.diagnostic))
            else:
                results.append(FileRefreshResult(
                    RefreshAction.UPDATE, state, snapshot.path, snapshot.item_id,
                    evidence=evidence, refreshed_item=disk_item,
                    diagnostic="file_replaced" if replaced else "external_change"))

        for evidence in sorted(new_paths, key=lambda value: _path_key(value.path)):
            _check_cancel(cancellation)
            if _path_key(evidence.path) in consumed_new:
                continue
            if evidence.placeholder:
                continue
            disk_item = self.read_stable_item(evidence, cancellation)
            if disk_item is not None:
                results.append(FileRefreshResult(
                    RefreshAction.ADD, ExternalChangeState.CHANGED_ON_DISK,
                    evidence.path, evidence=evidence, refreshed_item=disk_item))

        results.sort(key=lambda value: (
            value.action.value, _path_key(value.prior_path or value.path),
            value.item_id if value.item_id is not None else 2**63,
        ))
        _check_cancel(cancellation)
        return WorkspaceRefreshPlan(
            batch.session_id, generation, content_revision, change_revision,
            tuple(results), tuple(sorted(watched_directories, key=_path_key)), full,
        )

    @staticmethod
    def _root_lost_plan(batch: FilesystemEventBatch,
                        snapshots: tuple[TrackedFileSnapshot, ...], generation: int,
                        content_revision: int, change_revision: int) -> WorkspaceRefreshPlan:
        """Losing the root is a session condition, never per-file evidence.

        A network share, USB volume, cloud mount or drive letter can disappear
        for a moment.  That says nothing about any individual file, so nothing
        is removed, retired or resolved here: every loaded item is marked
        temporarily unavailable and keeps its ID, proposals, exclusions,
        selection and history until the root returns and one authoritative
        bounded reconciliation can state the truth.
        """
        results = tuple(
            FileRefreshResult(
                RefreshAction.MARK_STATE, ExternalChangeState.ROOT_UNAVAILABLE,
                snapshot.path, snapshot.item_id, diagnostic="root_unavailable")
            for snapshot in snapshots
            if snapshot.external_state != ExternalChangeState.ROOT_UNAVAILABLE.value
        )
        return WorkspaceRefreshPlan(
            batch.session_id, generation, content_revision, change_revision,
            results, (), False, False, True,
        )

    @staticmethod
    def read_stable_item(evidence: FilesystemEvidence,
                         cancellation: CancellationToken | None = None) -> AudioTrackItem | None:
        if (not evidence.exists or evidence.is_directory or evidence.placeholder
                or evidence.unreadable or evidence.outside_root):
            return None
        _check_cancel(cancellation)
        item = build_track_item(evidence.path)
        _check_cancel(cancellation)
        after = inspect_filesystem_path(evidence.path.parent, evidence.path)
        if not _identities_equal(evidence.identity, after.identity):
            return None
        return item

    @staticmethod
    def plan_resolution(conflict: StaleFileConflict,
                        action: ConflictResolutionAction, *, generation: int,
                        content_revision: int, change_revision: int,
                        target_path: Path | None = None) -> ConflictResolutionPlan:
        current = (
            conflict.generation == generation
            and conflict.content_revision == content_revision
            and conflict.change_revision == change_revision
        )
        diagnostic = ""
        accepted = current
        if not current:
            diagnostic = "stale_conflict"
        elif action is ConflictResolutionAction.KEEP_LOCAL and not conflict.safe_rebase:
            accepted, diagnostic = False, "overlapping_changes"
        elif action is ConflictResolutionAction.RELOAD and conflict.disk_item is None:
            accepted, diagnostic = False, "disk_item_unavailable"
        elif action is ConflictResolutionAction.REMOVE_MISSING and conflict.state is not ExternalChangeState.MISSING:
            accepted, diagnostic = False, "not_missing"
        elif action is ConflictResolutionAction.LOCATE_MOVED:
            if target_path is None or not strong_same_file(
                    conflict.baseline_identity,
                    inspect_filesystem_path(conflict.path.parent, target_path).identity):
                accepted, diagnostic = False, "moved_identity_not_proven"
        elif action in {ConflictResolutionAction.REVIEW, ConflictResolutionAction.CANCEL}:
            accepted = False
        return ConflictResolutionPlan(
            action, conflict.conflict_id, conflict.item_id, generation,
            content_revision, change_revision, conflict.disk_identity,
            conflict.disk_item, target_path, accepted, diagnostic,
        )
