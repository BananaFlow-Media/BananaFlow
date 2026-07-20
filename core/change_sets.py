"""Explicit, reversible in-memory change sets for the Tag Editor.

The media model intentionally remains the source of the canonical values.  A
``ChangeSet`` is the companion ledger: it records why a currently-proposed
value exists, makes it reviewable, and supplies safe proposal-level undo/redo.
It has no Qt dependency and never writes a media file.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable
from pathlib import Path
import os


class ChangeOrigin(str, Enum):
    MANUAL = "manual"
    AUTO_ARRANGE = "auto_arrange"
    CLEANUP = "cleanup"
    FILENAME = "filename"
    LYRICS = "lyrics"
    REPLAYGAIN = "replaygain"
    ARTWORK_ADD = "artwork_add"
    ARTWORK_REPLACE = "artwork_replace"
    ARTWORK_REMOVE = "artwork_remove"
    RESTORE = "restore"
    RECOVERY = "recovery"
    TEMPLATE = "template"
    ONLINE_METADATA = "online_metadata"
    IMPORT = "import"


class ChangeOperation(str, Enum):
    SET = "set"
    CLEAR = "clear"
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"
    RENAME = "rename"
    MOVE = "move"


@dataclass(frozen=True)
class ChangeRecord:
    """The active semantic change for a stable workspace item and field."""

    item_id: int
    field: str
    original_value: object
    previous_value: object
    proposed_value: object
    operation: ChangeOperation
    origin: ChangeOrigin
    revision: int
    excluded_from_apply: bool = False
    capability: str = ""
    diagnostic: str = ""
    source_provider: str = ""
    source_attribution: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class ChangeSetSummary:
    changed_files: int = 0
    changed_fields: int = 0
    filename_changes: int = 0
    artwork_changes: int = 0
    lyrics_changes: int = 0
    replaygain_changes: int = 0
    excluded_files: int = 0
    included_files: int = 0
    warnings: int = 0
    by_origin: dict[ChangeOrigin, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangeSetSnapshot:
    generation: int
    revision: int
    records: tuple[ChangeRecord, ...]
    excluded_ids: frozenset[int]


class ChangeSet:
    """Canonical active change ledger keyed by ``(stable item id, field)``."""

    def __init__(self) -> None:
        self._records: dict[tuple[int, str], ChangeRecord] = {}
        self._excluded_ids: set[int] = set()
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def clear(self) -> None:
        self._records.clear()
        self._excluded_ids.clear()
        self._revision += 1

    def records(self, *, item_ids: Iterable[int] | None = None) -> list[ChangeRecord]:
        allowed = set(item_ids) if item_ids is not None else None
        return [record for key, record in self._records.items()
                if allowed is None or key[0] in allowed]

    def record(self, item_id: int, field: str, original_value: object,
               proposed_value: object, *, operation: ChangeOperation,
               origin: ChangeOrigin, equal: Callable[[str, object, object], bool],
               capability: str = "", diagnostic: str = "",
               source_provider: str = "", source_attribution: str = "",
               source_url: str = "") -> ChangeRecord | None:
        """Replace one active record, dropping semantic no-ops.

        ``previous_value`` is deliberately retained for review/history while
        only the latest proposal stays active.  It allows undo to restore the
        previous origin instead of flattening a replacement into a new edit.
        """
        key = (item_id, field)
        prior = self._records.get(key)
        if equal(field, original_value, proposed_value):
            if prior is not None:
                del self._records[key]
                self._revision += 1
            return None
        if (prior is not None
                and equal(field, prior.original_value, original_value)
                and equal(field, prior.proposed_value, proposed_value)):
            # A refresh/model repaint must never fabricate another proposal or
            # overwrite the authoritative origin of an unchanged active edit.
            return prior
        previous = prior.proposed_value if prior is not None else original_value
        self._revision += 1
        record = ChangeRecord(
            item_id=item_id, field=field, original_value=deepcopy(original_value),
            previous_value=deepcopy(previous), proposed_value=deepcopy(proposed_value),
            operation=operation, origin=origin, revision=self._revision,
            excluded_from_apply=item_id in self._excluded_ids,
            capability=capability, diagnostic=diagnostic,
            source_provider=source_provider, source_attribution=source_attribution,
            source_url=source_url,
        )
        self._records[key] = record
        return record

    def remove(self, item_id: int, field: str) -> bool:
        if (item_id, field) not in self._records:
            return False
        del self._records[(item_id, field)]
        self._revision += 1
        return True

    def remove_items(self, item_ids: Iterable[int]) -> int:
        ids = set(item_ids)
        keys = [key for key in self._records if key[0] in ids]
        excluded_before = len(self._excluded_ids)
        for key in keys:
            del self._records[key]
        self._excluded_ids.difference_update(ids)
        if keys or len(self._excluded_ids) != excluded_before:
            self._revision += 1
        return len(keys)

    def set_excluded(self, item_ids: Iterable[int], excluded: bool) -> None:
        ids = set(item_ids)
        if excluded:
            self._excluded_ids.update(ids)
        else:
            self._excluded_ids.difference_update(ids)
        for key, record in list(self._records.items()):
            if key[0] in ids:
                self._records[key] = ChangeRecord(**{
                    **record.__dict__, "excluded_from_apply": key[0] in self._excluded_ids,
                })
        self._revision += 1

    def snapshot(self, generation: int) -> ChangeSetSnapshot:
        return ChangeSetSnapshot(
            generation=generation, revision=self._revision,
            records=tuple(self._records.values()), excluded_ids=frozenset(self._excluded_ids),
        )

    def restore(self, snapshot: ChangeSetSnapshot) -> None:
        self._records = {(record.item_id, record.field): record for record in snapshot.records}
        self._excluded_ids = set(snapshot.excluded_ids)
        self._revision = max(self._revision + 1, snapshot.revision)

    def summary(self) -> ChangeSetSummary:
        records = list(self._records.values())
        ids = {record.item_id for record in records}
        excluded = ids & self._excluded_ids
        origins: dict[ChangeOrigin, int] = {}
        for record in records:
            origins[record.origin] = origins.get(record.origin, 0) + 1
        warning_count = sum(bool(record.diagnostic or record.capability) for record in records)
        return ChangeSetSummary(
            changed_files=len(ids), changed_fields=len(records),
            filename_changes=sum(record.operation in {ChangeOperation.RENAME, ChangeOperation.MOVE} for record in records),
            artwork_changes=sum(record.field == "artwork" for record in records),
            lyrics_changes=sum(record.field == "lyrics" for record in records),
            replaygain_changes=sum(record.field.startswith("replaygain_") for record in records),
            excluded_files=len(excluded), included_files=len(ids - excluded),
            warnings=warning_count, by_origin=origins,
        )


class ApplyReviewPolicy:
    """Small, deterministic policy separating compact and full Apply review."""

    @staticmethod
    def requires_full_review(changes: ChangeSet, *, large_batch_threshold: int = 10) -> bool:
        summary = changes.summary()
        return bool(
            summary.changed_files > 1
            or summary.filename_changes
            or summary.artwork_changes
            or summary.excluded_files
            or summary.warnings
            or summary.changed_files >= large_batch_threshold
        )


@dataclass(frozen=True)
class BaselineSnapshot:
    """The disk-derived state of one item, captured for a reversible transition.

    A Change Set records what the *user* proposed.  This records what the file
    on disk was believed to be, which an external rebase also changes.  Without
    it a rebase whose proposals happen not to move would leave no undoable
    trace, and its baseline transition could never be undone.
    """

    item_id: int
    path: str
    original: object
    baseline_identity: FileIdentity | None
    status: object
    error_msg: str
    format_id: str
    metadata_editable: bool
    external_state: str
    external_detail: str
    external_conflict: object = None


@dataclass(frozen=True)
class ChangeCommand:
    """One intentional action: its proposal delta and any baseline transition."""

    generation: int
    before: ChangeSetSnapshot
    after: ChangeSetSnapshot
    label: str = ""
    baselines_before: tuple[BaselineSnapshot, ...] = ()
    baselines_after: tuple[BaselineSnapshot, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when neither the proposals nor any baseline actually moved."""
        return bool(
            self.before.records == self.after.records
            and self.before.excluded_ids == self.after.excluded_ids
            and self.baselines_before == self.baselines_after
        )

    def baseline_item_ids(self) -> frozenset[int]:
        return frozenset(
            snapshot.item_id
            for snapshot in (*self.baselines_before, *self.baselines_after)
        )


class ProposalHistory:
    """Generation-bound undo/redo history for in-memory proposals only."""

    def __init__(self) -> None:
        self._undo: list[ChangeCommand] = []
        self._redo: list[ChangeCommand] = []

    @property
    def undo_depth(self) -> int:
        """How many transitions are undoable — one per intentional action."""
        return len(self._undo)

    @property
    def redo_depth(self) -> int:
        return len(self._redo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, command: ChangeCommand) -> None:
        if command.is_empty:
            return
        self._undo.append(command)
        self._redo.clear()

    def prune_item_ids(self, item_ids: Iterable[int]) -> int:
        """Retire commands that could resurrect a removed workspace item."""
        retired = set(item_ids)

        def references(command: ChangeCommand) -> bool:
            snapshots = (command.before, command.after)
            return bool(
                retired & command.baseline_item_ids()
            ) or any(
                bool(retired & snapshot.excluded_ids)
                or any(record.item_id in retired for record in snapshot.records)
                for snapshot in snapshots
            )

        before = len(self._undo) + len(self._redo)
        self._undo = [command for command in self._undo if not references(command)]
        self._redo = [command for command in self._redo if not references(command)]
        return before - len(self._undo) - len(self._redo)

    def can_undo(self, generation: int) -> bool:
        return bool(self._undo and self._undo[-1].generation == generation)

    def can_redo(self, generation: int) -> bool:
        return bool(self._redo and self._redo[-1].generation == generation)

    def undo(self, changes: ChangeSet, generation: int) -> ChangeCommand | None:
        if not self.can_undo(generation):
            self._redo.clear()
            return None
        command = self._undo.pop()
        changes.restore(command.before)
        self._redo.append(command)
        return command

    def redo(self, changes: ChangeSet, generation: int) -> ChangeCommand | None:
        if not self.can_redo(generation):
            self._redo.clear()
            return None
        command = self._redo.pop()
        changes.restore(command.after)
        self._undo.append(command)
        return command


@dataclass(frozen=True)
class FileIdentity:
    """Portable baseline used to reject a replacement at the same path."""

    path: str
    size: int
    mtime_ns: int
    device: int | None = None
    inode: int | None = None


def capture_file_identity(path: Path) -> FileIdentity | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return FileIdentity(
        path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
        device=getattr(stat, "st_dev", None), inode=getattr(stat, "st_ino", None),
    )


def file_identity_status(expected: FileIdentity | None, path: Path) -> str:
    """Return a stable stale diagnostic without ever following a moved file."""
    if expected is None:
        return "unavailable"
    actual = capture_file_identity(path)
    if actual is None:
        return "missing"
    if actual.size != expected.size or actual.mtime_ns != expected.mtime_ns:
        return "changed"
    if expected.device is not None and actual.device != expected.device:
        return "identity_mismatch"
    # Windows does not provide a useful inode for all filesystems; compare it
    # only when both snapshots supplied a non-zero stable value.
    if expected.inode and actual.inode and actual.inode != expected.inode:
        return "identity_mismatch"
    return "current"
