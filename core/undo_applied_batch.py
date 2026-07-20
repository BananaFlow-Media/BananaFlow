"""Explicit disk-level Undo Applied Batch; separate from proposal Ctrl+Z."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from core.metadata_models import RestoreOutcome, RestoreStatus
from core.metadata_processor import (
    execute_rename_component_txn, load_tag_backup, plan_renames,
    resolve_owner_current, restore_tags, write_journal,
)
from core.operation_manifest import ManifestError, read_manifest
from core.restore_preview import preview_restore


@dataclass(frozen=True)
class UndoAppliedBatchResult:
    operation_id: str
    metadata_outcomes: tuple[RestoreOutcome, ...]
    physical_outcomes: tuple[RestoreOutcome, ...]
    partial: bool
    journal_path: Path


def undo_applied_batch(manifest_path: Path, *, restore_paths: bool = False,
                       cancel_event=None) -> UndoAppliedBatchResult:
    """Restore verified pre-state, optionally including an explicitly approved path plan.

    The caller must display :func:`preview_restore` first.  This function never
    silently moves files; ``restore_paths`` defaults to ``False``.
    """
    manifest = read_manifest(manifest_path)
    if manifest.get("status") != "completed":
        raise ManifestError("Undo Applied Batch requires a completed verified operation")
    preview = preview_restore(manifest_path)
    backup = load_tag_backup(manifest_path)  # full integrity/compatibility validation
    by_expected = {Path(record.get("final_path") or record["original_path"]): record
                   for record in manifest["records"]}
    records = []
    restore_target_by_current: dict[Path, Path] = {}
    physical: list[RestoreOutcome] = []
    for path, saved in backup:
        record = by_expected.get(path)
        current = next((item.current_path for item in preview.items if item.expected_current_path == path), None)
        if current is None:
            physical.append(RestoreOutcome(path=path, status=RestoreStatus.MISSING))
            continue
        expected = record.get("expected_post_identity") if record else None
        if isinstance(expected, dict):
            try:
                stat = current.stat()
                if stat.st_size != expected.get("size") or stat.st_mtime_ns != expected.get("mtime_ns"):
                    physical.append(RestoreOutcome(path=current, status=RestoreStatus.FAILED,
                                                   error="file_changed_externally"))
                    continue
            except OSError:
                physical.append(RestoreOutcome(path=current, status=RestoreStatus.MISSING)); continue
        records.append((current, saved))
        item = next((item for item in preview.items if item.current_path == current), None)
        if item is not None:
            restore_target_by_current[current] = item.original_path

    op_id = f"undo-{uuid.uuid4().hex}"
    journal_path = manifest_path.parent / f"bananaflow_tag_undo_{op_id}.journal.json"
    from datetime import datetime
    files = {}
    for current, _saved in records:
        try:
            stat = current.stat()
            identity = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except OSError:
            identity = None
        files[str(current)] = {
            "original_path": str(current), "current_path": str(current),
            "original_target": str(restore_target_by_current.get(current, current)),
            "pre_identity": identity, "state": "planned",
        }
    journal = {"schema": 1, "operation_id": op_id, "operation_type": "undo_applied_batch",
               "backup_path": str(manifest_path), "source_operation_id": manifest.get("operation_id"),
               "created": datetime.now().isoformat(timespec="seconds"),
               "batch_state": "metadata_writing", "files": files}
    write_journal(journal_path, journal, durable=True)
    metadata = []
    for current, saved in records:
        entry = files[str(current)]
        if cancel_event is not None and cancel_event.is_set():
            entry["state"] = "cancelled"
            metadata.append(RestoreOutcome(current, RestoreStatus.CANCELLED, "cancelled"))
            write_journal(journal_path, journal, durable=True)
            continue
        entry["state"] = "written"
        write_journal(journal_path, journal, durable=True)
        restored_batch = restore_tags([(current, saved)], cancel_event=cancel_event)
        if not restored_batch:
            continue
        outcome = restored_batch[0]
        metadata.append(outcome)
        entry["state"] = ("verified" if outcome.status in {RestoreStatus.RESTORED, RestoreStatus.UNCHANGED}
                          else "cancelled" if outcome.status == RestoreStatus.CANCELLED else "failed")
        write_journal(journal_path, journal, durable=True)
    journal["batch_state"] = "metadata_verified"; write_journal(journal_path, journal, durable=True)

    if restore_paths:
        journal["batch_state"] = "physical_preparing"; write_journal(journal_path, journal, durable=True)
        for component in preview.rename_plan.components:
            result = execute_rename_component_txn(journal, component,
                                                   lambda: write_journal(journal_path, journal, durable=True))
            for owner in component.members:
                target = component.final.get(owner, Path(owner))
                physical.append(RestoreOutcome(
                    path=target,
                    status=RestoreStatus.RESTORED if result["status"] == "ok" else RestoreStatus.FAILED,
                    error="" if result["status"] == "ok" else str(result["failure"]),
                ))
                entry = files.get(str(owner))
                if entry is not None:
                    entry["state"] = "complete" if result["status"] == "ok" else "partial"
        journal["batch_state"] = "physical_complete"; write_journal(journal_path, journal, durable=True)
    partial = any(outcome.status in {RestoreStatus.FAILED, RestoreStatus.MISSING}
                  for outcome in (*metadata, *physical))
    journal["batch_state"] = "partial" if partial else "completed"
    write_journal(journal_path, journal, durable=True)
    return UndoAppliedBatchResult(op_id, tuple(metadata), tuple(physical), partial, journal_path)


class _RecoveryMove:
    def __init__(self, source: Path, destination: Path) -> None:
        self.path = source
        self.proposed_filename = destination.name


def execute_undo_recovery(journal_path: Path, *, cancel_event=None):
    """Resume an interrupted Undo from disk evidence without repeating work."""
    from core.metadata_processor import execute_restore_recovery, read_journal
    metadata, metadata_ok = execute_restore_recovery(journal_path, cancel_event=cancel_event)
    journal = read_journal(journal_path)
    if not metadata_ok:
        return metadata, False

    moves = []
    owner_by_source: dict[str, str] = {}
    outcomes = list(metadata)
    for key, entry in journal.get("files", {}).items():
        target = Path(entry.get("original_target") or key)
        resolution, value = resolve_owner_current(journal, entry)
        current = value if resolution == "resolved" else None
        if current is None:
            outcomes.append(RestoreOutcome(target, RestoreStatus.MISSING, resolution))
            entry["state"] = "unresolved"
            continue
        if str(current) == str(target):
            entry["state"] = "complete"
            entry["reconciled_from_disk"] = True
            continue
        if target.exists():
            outcomes.append(RestoreOutcome(current, RestoreStatus.FAILED, "path_collision"))
            entry["state"] = "unresolved"
            continue
        moves.append(_RecoveryMove(current, target))
        owner_by_source[str(current)] = key

    plan = plan_renames(moves)
    persist = lambda: write_journal(journal_path, journal, durable=True)
    for source, code in plan.blocked.items():
        outcomes.append(RestoreOutcome(Path(source), RestoreStatus.FAILED, str(code)))
        journal["files"][owner_by_source.get(source, source)]["state"] = "unresolved"
    for component in plan.components:
        for index, (owner, src, dst) in enumerate(component.steps):
            component.steps[index] = (owner_by_source.get(str(owner), str(owner)), src, dst)
        component.members = {owner_by_source.get(str(owner), str(owner)) for owner in component.members}
        component.final = {owner_by_source.get(str(owner), str(owner)): value
                           for owner, value in component.final.items()}
        result = execute_rename_component_txn(journal, component, persist)
        for owner, target in component.final.items():
            ok = result["status"] == "ok" and target.exists()
            journal["files"][owner]["state"] = "complete" if ok else "unresolved"
            outcomes.append(RestoreOutcome(target, RestoreStatus.RESTORED if ok else RestoreStatus.FAILED,
                                           "" if ok else str(result["failure"])))
    all_ok = all(entry.get("state") in {"verified", "complete"}
                 for entry in journal.get("files", {}).values())
    journal["batch_state"] = "completed" if all_ok else "partial"
    persist()
    return outcomes, all_ok
