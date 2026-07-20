"""
ui/workers/metadata_worker.py  –  Background workers for the Tag Editor
========================================================================
MetadataScanWorker    – scans a folder and emits tracks incrementally.
MetadataApplyWorker   – writes proposed tags file-by-file, crash-safely.
MetadataRestoreWorker – writes backed-up tags back onto their files.

All workers use threading.Event for cancellation (same pattern as FetchWorker).

The Apply worker (Phase 1 safety hardening) makes every write honest,
crash-safe, recoverable and preservation-safe:

* it aborts the whole batch with *zero* media modified if the backup
  target preflight or the backup write itself fails (TE-SAFE-01/02);
* it writes each file via temp-copy → verify → atomic replace
  (TE-SAFE-08/12) and only the proposed field delta (TE-SAFE-07);
* it tracks metadata-write and rename outcomes separately, never
  counting a failed/blocked rename as success (TE-SAFE-04/05);
* it persists a durable journal at every state transition so a crash is
  recoverable (TE-SAFE-11);
* it emits a structured per-file `file_outcome` and a batch `finished`
  result (TE-SAFE-06), and captures the controller's op_generation so a
  stale worker's signals can be rejected (TE-SAFE-09);
* it cancels only at declared safe boundaries (TE-SAFE-13).
"""

from __future__ import annotations

import logging
import threading
import uuid

from utils.paths import get_tag_backup_dir
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.metadata_models import (
    ApplyBatchResult,
    ApplyErrorCode,
    ApplyOutcome,
    ApplyStage,
    ApplyStatus,
    AudioTrackItem,
    JournalBatchState,
    JournalFileState,
    LYRICS_FIELD,
    OriginalTags,
    RestoreOutcome,
    REPLAYGAIN_FIELDS,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    ScanResult,
    TrackStatus,
)
from core.metadata_processor import (
    ApplyWriteError,
    BackupTargetError,
    apply_journal_path,
    atomic_write_tags,
    backup_tags,
    build_scan_result,
    build_track_item,
    collect_scan_targets,
    execute_rename_component_txn,
    plan_renames,
    restore_tags,
    validate_backup_target,
    write_journal,
)

logger = logging.getLogger(__name__)


class JournalWriteError(Exception):
    """A durable journal write failed. Journal durability is a hard contract:
    before any media modification this aborts the batch; after modifications
    begin it stops processing at a safe boundary and flags recovery-required."""


class MetadataScanWorker(QThread):
    """
    Walks collect_scan_targets()/build_track_item() in a background thread
    and emits each track as it is discovered so the table can populate live.

    Signals
    -------
    track_found(AudioTrackItem)   One file scanned and ready.
    scan_complete(ScanResult)     All files processed.
    scan_error(str)               Unrecoverable failure (rare).
    """

    track_found   = Signal(object)   # AudioTrackItem
    track_batch_found = Signal(object)   # list[AudioTrackItem]
    scan_progress = Signal(int, int)  # done, total
    scan_complete = Signal(object)   # ScanResult
    scan_error    = Signal(str)

    def __init__(self, root: Path, recursive: bool, parent=None) -> None:
        super().__init__(parent)
        self._root      = root
        self._recursive = recursive
        self._cancel    = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        tracks: list[AudioTrackItem] = []
        skipped = 0
        folders: set[Path] = {self._root}

        try:
            file_paths, folders, skipped = collect_scan_targets(self._root, self._recursive)
            total = len(file_paths)
            self.scan_progress.emit(0, total)

            batch: list[AudioTrackItem] = []
            for done, file_path in enumerate(file_paths, start=1):
                if self._cancel.is_set():
                    break
                item = build_track_item(file_path)
                tracks.append(item)
                batch.append(item)
                if len(batch) >= 200:
                    self.track_batch_found.emit(batch)
                    batch = []
                    self.scan_progress.emit(done, total)

            if batch:
                self.track_batch_found.emit(batch)
                self.scan_progress.emit(len(tracks), total)

        except Exception as exc:
            self.scan_error.emit(str(exc))
            return

        result = build_scan_result(self._root, tracks, skipped, folders,
                                   recursive=self._recursive)
        self.scan_complete.emit(result)


class ReplayGainAnalysisWorker(QThread):
    """Cancellation-aware analysis worker that never writes media or tags."""

    progress = Signal(int, int)
    result_ready = Signal(object, object)  # AudioTrackItem, canonical values
    finished = Signal(object)              # structured summary dict

    def __init__(
        self,
        tracks: list[AudioTrackItem],
        *,
        mode: str,
        groups=(),
        operation_id: str,
        op_generation: int,
        selection_ids: tuple[int, ...],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tracks = list(tracks)
        self._mode = mode
        self._groups = tuple(groups)
        self._operation_id = operation_id
        self._op_generation = op_generation
        self._selection_ids = selection_ids
        self._cancel = threading.Event()

    @property
    def op_generation(self) -> int:
        return self._op_generation

    @property
    def selection_ids(self) -> tuple[int, ...]:
        return self._selection_ids

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        from core.replay_gain import (
            ReplayGainAnalysisCancelled,
            _REFERENCE_LUFS,
            analyse_album_program,
            analyse_track,
        )

        total = len(self._tracks)
        completed = 0
        failures: list[dict[str, str]] = []
        cancelled = False

        def analyze_one(item: AudioTrackItem):
            nonlocal completed, cancelled
            if self._cancel.is_set():
                cancelled = True
                return None
            try:
                result = analyse_track(item.path, cancel_event=self._cancel)
            except ReplayGainAnalysisCancelled:
                cancelled = True
                return None
            except Exception as exc:
                failures.append({"path": str(item.path), "detail": str(exc)})
                completed += 1
                self.progress.emit(completed, total)
                return None
            completed += 1
            self.progress.emit(completed, total)
            return result

        if self._mode == "track":
            for item in self._tracks:
                result = analyze_one(item)
                if result is not None:
                    self.result_ready.emit(item, result.proposal_values())
                if cancelled:
                    break
        else:
            for group in self._groups:
                group_results = []
                group_items = []
                for item in group.tracks:
                    result = analyze_one(item)
                    if result is not None:
                        group_results.append(result)
                        group_items.append(item)
                        # Completed per-track analysis remains useful even if
                        # cancellation prevents the album aggregate.
                        self.result_ready.emit(item, result.proposal_values())
                    if cancelled:
                        break
                if (
                    group_results
                    and len(group_results) == len(group.tracks)
                    and not group.ambiguous
                    and not cancelled
                ):
                    try:
                        album_loudness, album_peak = analyse_album_program(
                            (item.path for item in group_items),
                            cancel_event=self._cancel,
                        )
                    except ReplayGainAnalysisCancelled:
                        cancelled = True
                    except Exception as exc:
                        failures.append({
                            "path": "\n".join(str(item.path) for item in group_items),
                            "detail": str(exc),
                        })
                    else:
                        album_values = {
                            REPLAYGAIN_ALBUM_GAIN: _REFERENCE_LUFS - album_loudness,
                            REPLAYGAIN_ALBUM_PEAK: album_peak,
                        }
                        for item in group_items:
                            self.result_ready.emit(item, album_values)
                if cancelled:
                    break

        self.finished.emit({
            "operation_id": self._operation_id,
            "mode": self._mode,
            "total": total,
            "completed": completed,
            "failed": len(failures),
            "failures": failures,
            "cancelled": cancelled,
        })


class MetadataApplyWorker(QThread):
    """
    Crash-safe, preservation-safe apply pipeline (Phase 1).

    Signals
    -------
    progress(int, int)            (done_count, total_count)
    file_outcome(object)          per-file ApplyOutcome (TE-SAFE-06)
    finished(object)              batch ApplyBatchResult (TE-SAFE-06)
    """

    progress     = Signal(int, int)
    file_outcome = Signal(object)   # ApplyOutcome
    finished     = Signal(object)   # ApplyBatchResult

    def __init__(
        self,
        tracks:       list[AudioTrackItem],
        backup_path:  Path,
        *,
        operation_id: str,
        op_generation: int = 0,
        root:         Optional[Path] = None,
        app_version:  str = "",
        review_plan=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Apply owns an immutable snapshot. UI/controller proposals may evolve
        # while this worker is writing, but the worker must never observe or
        # clear those later values.
        self._tracks: list[AudioTrackItem] = []
        self._live_by_key: dict[str, AudioTrackItem] = {}
        self._proposal_tokens: dict[str, dict[str, object]] = {}
        self._filename_tokens: dict[str, str | None] = {}
        for live in tracks:
            snapshot = deepcopy(live)
            key = str(snapshot.path)
            self._tracks.append(snapshot)
            self._live_by_key[key] = live
            fields = snapshot.proposed.changed_fields(snapshot.original)
            self._proposal_tokens[key] = {
                field_name: _proposal_token(snapshot.proposed, field_name)
                for field_name in fields
            }
            self._filename_tokens[key] = snapshot.proposed_filename
        self._backup_path   = backup_path
        self._operation_id  = operation_id
        self._op_generation = op_generation
        self._root          = root
        self._app_version   = app_version
        # The controller's reviewed plan is immutable evidence of the exact
        # scope accepted by the user.  The worker owns independent media
        # snapshots as well, so later UI proposals cannot mutate disk input.
        self._review_plan = review_plan
        self._cancel        = threading.Event()
        self._journal_path  = apply_journal_path(backup_path, operation_id)
        self._journal: dict = {}

    @property
    def op_generation(self) -> int:
        return self._op_generation

    def cancel(self) -> None:
        self._cancel.set()

    # ── Journal helpers ──────────────────────────────────────────────────────

    def _persist_journal(self, *, durable: bool = False) -> None:
        """
        Persist the journal. Journal durability is a HARD contract, not
        best-effort (defect 1): a write failure raises JournalWriteError so the
        caller aborts (before any media modification) or stops at the next safe
        boundary (after modifications begin). Safety-critical transitions pass
        durable=True (flush+fsync).
        """
        try:
            write_journal(self._journal_path, self._journal, durable=durable)
        except Exception as exc:
            raise JournalWriteError(str(exc)) from exc

    def _update_file_state(self, key: str, state: str, **extra) -> None:
        """Update a file's journal record in memory without persisting."""
        rec = self._journal["files"].get(key)
        if rec is None:
            return
        rec["state"] = state
        rec.update(extra)

    def _set_file_state(self, key: str, state: str, *, durable: bool = False, **extra) -> None:
        """Update a file's journal record and persist immediately."""
        self._update_file_state(key, state, **extra)
        self._persist_journal(durable=durable)

    def _set_batch_state(self, state: str, *, durable: bool = False) -> None:
        self._journal["batch_state"] = state
        self._persist_journal(durable=durable)

    # ── Run ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        result = ApplyBatchResult(
            operation_id=self._operation_id,
            backup_path=self._backup_path,
            journal_path=self._journal_path,
        )

        changed = [
            t for t in self._tracks
            if t.proposed_filename is not None
            or (t.metadata_editable and t.proposed.has_changes(t.original))
        ]
        total = len(changed)
        if total == 0:
            self.finished.emit(result)
            return

        # A source may be replaced in the narrow interval after the UI review
        # preflight but before this worker begins.  Do not create a backup or
        # touch any media if that reviewed identity is no longer current.
        from core.change_sets import file_identity_status
        stale = [item for item in changed
                 if file_identity_status(item.baseline_identity, item.path)
                 not in {"current", "unavailable"}]
        if stale:
            result.preflight_ok = False
            result.global_error_key = "meta_apply_blocked_title"
            result.global_error_detail = "reviewed source changed before Apply"
            self.finished.emit(result)
            return

        if self._review_plan is not None:
            planned_paths = {str(item.source_path) for item in self._review_plan.items}
            actual_paths = {str(item.path) for item in changed}
            if (self._review_plan.operation_id != self._operation_id
                    or actual_paths != planned_paths):
                result.preflight_ok = False
                result.global_error_key = "meta_apply_blocked_title"
                result.global_error_detail = "reviewed Apply plan no longer matches execution scope"
                self.finished.emit(result)
                return

        # ── Preflight: backup target must be usable, or abort untouched ──────
        try:
            validate_backup_target(self._backup_path.parent)
        except BackupTargetError as exc:
            result.preflight_ok = False
            result.global_error_key = "meta_backup_target_failed"
            result.global_error_detail = str(exc)
            logger.error("[MetadataApplyWorker] Backup target preflight failed: %s", exc)
            self.finished.emit(result)
            return

        # ── Preflight: rename graph (blocked hazards preserved & reported) ──
        rename_plan = plan_renames(changed)

        # The physical graph is part of the immutable reviewed Apply scope.  A
        # destination can become occupied after review, so the fresh plan is a
        # hard batch gate: do not create an executable backup, journal a write
        # transition, write tags, rename files, or reconcile proposals.
        if rename_plan.blocked:
            result.preflight_ok = False
            result.global_error_key = "meta_apply_blocked_title"
            result.blocked_items = dict(rename_plan.blocked)
            result.global_error_detail = "; ".join(
                f"{path}: {code}" for path, code in sorted(rename_plan.blocked.items())
            )
            self.finished.emit(result)
            return

        # Publish the complete operation intent before backup creation.  A crash
        # during backup is then discoverable without any media having changed.
        self._init_journal(changed, rename_plan)
        try:
            self._persist_journal(durable=True)
        except JournalWriteError as exc:
            result.preflight_ok = False
            result.global_error_key = "meta_journal_init_failed"
            result.global_error_detail = str(exc)
            logger.error("[MetadataApplyWorker] Journal init failed: %s", exc)
            self._remove_journal()
            self.finished.emit(result)
            return

        # ── Backup FIRST; failure aborts the batch with zero media touched ──
        try:
            backup_tags(
                changed, self._backup_path,
                operation_id=self._operation_id, root=self._root,
                app_version=self._app_version,
            )
        except Exception as exc:
            result.backup_ok = False
            result.global_error_key = "meta_backup_write_failed"
            result.global_error_detail = str(exc)
            self._journal["batch_state"] = JournalBatchState.FAILED
            self._journal["failure_stage"] = "backup"
            self._try_persist_final_journal()
            logger.error("[MetadataApplyWorker] Backup write failed — aborting: %s", exc)
            self.finished.emit(result)
            return

        # ── HARD precondition: persist the complete plan (PLANNED, durable)
        #    BEFORE the first media modification. If this fails, abort the batch
        #    with zero media modified and a distinct batch-level error (defect 1).
        try:
            for rec in self._journal["files"].values():
                rec["state"] = JournalFileState.BACKED_UP
            self._set_batch_state(JournalBatchState.APPLYING, durable=True)
        except JournalWriteError as exc:
            result.preflight_ok = False
            result.global_error_key = "meta_journal_init_failed"
            result.global_error_detail = str(exc)
            logger.error("[MetadataApplyWorker] Journal init failed — aborting: %s", exc)
            self.finished.emit(result)
            return

        outcomes: dict[str, ApplyOutcome] = {}
        cancelled_at: Optional[int] = None
        journal_failed: Optional[str] = None

        # ── Phase A: per-file atomic tag write + verify ─────────────────────
        for i, item in enumerate(changed):
            if self._cancel.is_set():          # safe boundary: between files
                cancelled_at = i
                break

            key = str(item.path)
            # Perform the media write + update the in-memory journal record, then
            # persist that transition durably. Capture the outcome BEFORE the
            # persist so a journal failure still records what happened on disk.
            oc = self._write_one(item, key)
            outcomes[key] = oc
            self.file_outcome.emit(oc)
            self.progress.emit(i + 1, total)
            try:
                self._persist_journal(durable=True)
            except JournalWriteError as exc:
                # The media write for this file already happened, but its durable
                # journal transition failed. Stop at this safe boundary and
                # require recovery (defect 1) — do NOT silently continue.
                journal_failed = str(exc)
                logger.error(
                    "[MetadataApplyWorker] Journal transition failed after write "
                    "— stopping for recovery: %s", exc)
                cancelled_at = i + 1   # this file was handled; stop after it
                break

        # Mark any files not reached (cancel/journal-stop) as CANCELLED.
        if cancelled_at is not None:
            for item in changed[cancelled_at:]:
                key = str(item.path)
                oc = ApplyOutcome(
                    original_path=item.path, final_path=item.path,
                    stage=ApplyStage.WRITE, status=ApplyStatus.CANCELLED,
                    error_code=ApplyErrorCode.CANCELLED,
                    message_key="meta_apply_cancelled", retryable=True,
                    rename_pending=bool(item.proposed_filename),
                )
                outcomes[key] = oc
                try:
                    self._set_file_state(key, JournalFileState.CANCELLED)
                except JournalWriteError:
                    journal_failed = journal_failed or "journal write failed"
                self.file_outcome.emit(oc)

        # ── Phase B: execute the validated rename graph (skip if stopped) ───
        if cancelled_at is None and journal_failed is None:
            try:
                self._execute_renames(changed, rename_plan, outcomes)
            except JournalWriteError as exc:
                journal_failed = str(exc)
                logger.error("[MetadataApplyWorker] Journal failed during rename "
                             "phase — recovery required: %s", exc)

        # ── Finalise batch result ───────────────────────────────────────────
        result.outcomes = list(outcomes.values())
        for oc in result.outcomes:
            if oc.status == ApplyStatus.SUCCESS:
                result.success_count += 1
            elif oc.status == ApplyStatus.PARTIAL:
                result.partial_count += 1
            elif oc.status == ApplyStatus.FAILED:
                result.failed_count += 1
            elif oc.status == ApplyStatus.CANCELLED:
                result.cancelled_count += 1
            elif oc.status == ApplyStatus.SKIPPED:
                result.skipped_count += 1

        # A failed rename rollback leaves stranded artifacts → recovery required.
        if any(oc.error_code == ApplyErrorCode.RENAME_ROLLBACK_FAILED
               for oc in result.outcomes):
            result.recovery_required = True
            if not result.global_error_key:
                result.global_error_key = "meta_rename_rollback_failed"

        if journal_failed is not None:
            # Durable journalling failed mid-batch: the on-disk state may be
            # ahead of the last durable record. Keep the journal and flag the
            # batch recovery-required (defect 1).
            result.recovery_required = True
            result.global_error_key = "meta_journal_transition_failed"
            result.global_error_detail = journal_failed
            self._try_persist_final_journal()
            self.finished.emit(result)
            return

        clean = (result.failed_count == 0 and result.partial_count == 0
                 and result.cancelled_count == 0 and not result.recovery_required)
        if clean:
            # Fully-successful batch: mark DONE and leave no recovery journal.
            try:
                self._set_batch_state(JournalBatchState.DONE, durable=True)
                self._remove_journal()
            except JournalWriteError:
                # The batch itself is fine; only the terminal record failed.
                self._remove_journal()
        else:
            # Unresolved files remain — keep the journal at a non-DONE state so
            # startup recovery can find and offer to resolve it (TE-SAFE-11).
            self._try_persist_final_journal()

        self.finished.emit(result)

    # ── Per-file write ───────────────────────────────────────────────────────

    def _write_one(self, item: AudioTrackItem, key: str) -> ApplyOutcome:
        """Atomically write one file's tag delta; original untouched on failure.

        Updates the in-memory journal record only; the caller persists the
        durable transition so the returned outcome is captured even if the
        journal write then fails.
        """
        # A read-only/unsupported container can still safely take part in the
        # Phase-1 rename transaction.  Keep its tag proposal pending and never
        # call a metadata writer; the rename phase receives a successful
        # physical-operation precondition below.
        if not item.metadata_editable:
            self._update_file_state(key, JournalFileState.VERIFIED, fields=[])
            return ApplyOutcome(
                original_path=item.path, final_path=item.path,
                stage=ApplyStage.RENAME, status=ApplyStatus.SUCCESS,
                error_code=ApplyErrorCode.UNSUPPORTED,
                detail="metadata is not editable; filename operation only",
                fields_written=[], rename_pending=bool(item.proposed_filename),
            )
        try:
            fields = atomic_write_tags(item.path, item.proposed, item.original)
            # atomic_write_tags verified exactly these snapshot fields before
            # replacing the source. Reconcile only those fields into the live
            # item and clear only matching snapshot proposals. Any later edit or
            # ReplayGain/Lyrics proposal remains pending and is never reported
            # as part of this Apply.
            live = self._live_by_key[key]
            effective = item.proposed.effective_tags(item.original)
            verified = getattr(fields, "readback", effective)
            live.original = _merge_verified_fields(live.original, verified, fields)
            _clear_matching_snapshot_proposals(
                live, self._proposal_tokens.get(key, {}), fields
            )
            live.status = TrackStatus.CHANGED if live.has_changes else TrackStatus.DONE
            try:
                stat = item.path.stat()
                post_identity = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            except OSError:
                post_identity = None
            self._update_file_state(
                key, JournalFileState.VERIFIED, fields=fields,
                post_write_identity=post_identity,
            )
            return ApplyOutcome(
                original_path=item.path, final_path=item.path,
                stage=ApplyStage.VERIFY, status=ApplyStatus.SUCCESS,
                fields_written=fields,
                rename_pending=bool(item.proposed_filename),
            )
        except ApplyWriteError as exc:
            # original left byte-identical; keep the proposal so the user retries.
            live = self._live_by_key[key]
            live.status = TrackStatus.ERROR
            live.error_msg = str(exc)
            self._update_file_state(
                key, JournalFileState.FAILED,
                error_code=exc.code, detail=str(exc),
            )
            stage = ApplyStage.VERIFY if exc.stage == "verify" else ApplyStage.WRITE
            code = (ApplyErrorCode.VERIFY_FAILED if exc.stage == "verify"
                    else ApplyErrorCode.WRITE_FAILED)
            return ApplyOutcome(
                original_path=item.path, final_path=item.path,
                stage=stage, status=ApplyStatus.FAILED,
                error_code=code, message_key="meta_apply_write_failed",
                detail=str(exc), retryable=True,
                rename_pending=bool(item.proposed_filename),
            )

    # ── Rename execution ─────────────────────────────────────────────────────

    def _execute_renames(
        self,
        changed: list[AudioTrackItem],
        plan,
        outcomes: dict[str, ApplyOutcome],
    ) -> None:
        by_path = {str(t.path): t for t in changed}

        # 1. Blocked renames (hazards): keep the proposal, report PARTIAL.
        for key, code in plan.blocked.items():
            self._mark_rename_partial(by_path, outcomes, key, code, "meta_rename_blocked")

        # 2. Each connected rename component executes transactionally via the
        #    owner-aware ledger; a runtime/journal failure rolls it back.
        for comp in plan.components:
            if self._cancel.is_set():
                for k in comp.members:
                    self._mark_rename_partial(
                        by_path, outcomes, k, ApplyErrorCode.CANCELLED,
                        "meta_apply_cancelled")
                continue
            self._execute_rename_component(comp, by_path, outcomes)

        # 3. Mark plain (no-rename) successful files COMPLETE in the journal.
        member_keys = {k for comp in plan.components for k in comp.members}
        member_keys |= set(plan.blocked)
        for key, oc in outcomes.items():
            if oc.status == ApplyStatus.SUCCESS and key not in member_keys:
                self._update_file_state(key, JournalFileState.COMPLETE)

        # Single durable persist for the whole rename/finalise phase.
        self._persist_journal(durable=True)

    def _mark_rename_partial(self, by_path, outcomes, key, code, message_key,
                             final_path=None) -> None:
        item = by_path.get(key)
        oc = outcomes.get(key)
        if item is None or oc is None:
            return
        if oc.status == ApplyStatus.SUCCESS:
            oc.status = ApplyStatus.PARTIAL
            oc.stage = ApplyStage.RENAME
            oc.error_code = code
            oc.message_key = message_key
            oc.retryable = True
            oc.rename_pending = True
            self._update_file_state(key, JournalFileState.PARTIAL, error_code=code)

    def _execute_rename_component(self, comp, by_path, outcomes) -> None:
        """Execute one rename component via the shared owner-aware ledger txn.

        The ledger persists INTENT before and COMPLETED after each disk move,
        rolls back on any failure, and records UNRESOLVED (with both paths) if a
        rollback fails — so recovery can reconstruct each PHYSICAL file's
        location per owner. Raises JournalWriteError to the batch loop when
        journalling failed so the batch stops recovery-required.
        """
        # Gate: never begin unless EVERY member verified its metadata write.
        members_ok = all(
            outcomes.get(k) is not None and outcomes[k].status == ApplyStatus.SUCCESS
            for k in comp.members
        )
        if not members_ok:
            for k in comp.members:
                self._mark_rename_partial(
                    by_path, outcomes, k,
                    ApplyErrorCode.RENAME_BLOCKED_SIBLING, "meta_rename_blocked")
            return

        result = execute_rename_component_txn(
            self._journal, comp, lambda: self._persist_journal(durable=True))
        status = result["status"]

        if status == "ok":
            # Reconcile members, then persist the component's final-path mapping.
            for key, final in comp.final.items():
                item = by_path.get(key)
                oc = outcomes.get(key)
                if item is None or oc is None:
                    continue
                if final.exists():
                    item.path = final
                    item.folder = final.parent
                    item.proposed_filename = None
                    live = self._live_by_key[key]
                    live.path = final
                    live.folder = final.parent
                    if live.proposed_filename == self._filename_tokens.get(key):
                        live.proposed_filename = None
                    live.status = TrackStatus.CHANGED if live.has_changes else TrackStatus.DONE
                    oc.final_path = final
                    oc.rename_pending = False
                    self._update_file_state(key, JournalFileState.COMPLETE, final_path=str(final))
                else:
                    self._update_file_state(key, JournalFileState.COMPLETE)
            self._persist_journal(durable=True)
            return

        kind, code = result["failure"]
        if status == "unresolved":
            for k in comp.members:
                self._mark_component_unresolved(by_path, outcomes, k)
        else:  # rolled_back
            for k in comp.members:
                self._mark_rename_partial(by_path, outcomes, k, code, "meta_rename_failed")
        if kind == "journal":
            # Journalling is unreliable → stop the batch recovery-required.
            raise JournalWriteError("journal failed during rename component")

    def _mark_component_unresolved(self, by_path, outcomes, key) -> None:
        """A rollback failed: the ledger holds the exact UNRESOLVED path mapping."""
        oc = outcomes.get(key)
        if oc is not None:
            oc.status = ApplyStatus.PARTIAL
            oc.stage = ApplyStage.RENAME
            oc.error_code = ApplyErrorCode.RENAME_ROLLBACK_FAILED
            oc.message_key = "meta_rename_rollback_failed"
            oc.retryable = False
            oc.rename_pending = True
        self._update_file_state(
            key, JournalFileState.UNRESOLVED,
            error_code=ApplyErrorCode.RENAME_ROLLBACK_FAILED,
        )

    # ── Journal init / cleanup ───────────────────────────────────────────────

    def _init_journal(self, changed: list[AudioTrackItem], plan) -> None:
        from dataclasses import asdict, is_dataclass
        files: dict[str, dict] = {}
        for item in changed:
            key = str(item.path)
            intended = (
                str(item.path.parent / item.proposed_filename)
                if item.proposed_filename else key
            )
            files[key] = {
                "original_path": key,
                "intended_path": intended,
                "final_path": None,
                "changed_fields": sorted(item.proposed.changed_fields(item.original)),
                "baseline_identity": (asdict(item.baseline_identity)
                                      if is_dataclass(item.baseline_identity)
                                      else item.baseline_identity),
                "expected_metadata": item.proposed.effective_tags(item.original).to_dict(),
                "state": JournalFileState.PLANNED,
                "error_code": "",
                "detail": "",
            }
        self._journal = {
            "schema": 1,
            "operation_id": self._operation_id,
            "operation_type": "apply",
            "backup_path": str(self._backup_path),
            "root": str(self._root) if self._root else None,
            "created": datetime.now().isoformat(timespec="seconds"),
            "batch_state": JournalBatchState.BACKING_UP,
            "rename_steps": [[str(s), str(d)] for s, d in plan.steps],
            "rename_blocked": dict(plan.blocked),
            "files": files,
        }
        # Persistence is done (durably) by the caller as a hard precondition.

    def _try_persist_final_journal(self) -> None:
        """Best-effort durable persist of a terminal/retained journal.

        Used only when the batch has already stopped for a non-clean/
        recovery-required reason, where a further journal failure cannot make
        things worse (the journal is retained for recovery either way)."""
        try:
            self._persist_journal(durable=True)
        except JournalWriteError as exc:
            logger.error("[MetadataApplyWorker] Final journal persist failed: %s", exc)

    def _remove_journal(self) -> None:
        try:
            if self._journal_path.exists():
                self._journal_path.unlink()
        except OSError:
            logger.warning("[MetadataApplyWorker] Could not remove journal: %s",
                           self._journal_path)


def _proposal_token(proposed, field_name: str):
    """Return the raw immutable token representing one proposed field."""
    if field_name == LYRICS_FIELD:
        return deepcopy(proposed.lyrics_change)
    if field_name == "artwork":
        return deepcopy(proposed.artwork_change)
    if field_name in REPLAYGAIN_FIELDS:
        return deepcopy(proposed.replay_gain_changes.get(field_name))
    return deepcopy(getattr(proposed, field_name))


def _clear_matching_snapshot_proposals(
    item: AudioTrackItem,
    snapshot_tokens: dict[str, object],
    verified_fields: list[str],
) -> None:
    """Clear only verified proposals still equal to the attempted snapshot."""
    for field_name in verified_fields:
        if field_name not in snapshot_tokens:
            continue
        if _proposal_token(item.proposed, field_name) != snapshot_tokens[field_name]:
            continue
        if field_name == LYRICS_FIELD:
            item.proposed.revert_lyrics()
        elif field_name == "artwork":
            item.proposed.revert_artwork()
        elif field_name in REPLAYGAIN_FIELDS:
            item.proposed.revert_replay_gain({field_name})
        else:
            setattr(item.proposed, field_name, None)


def _merge_verified_fields(
    current: OriginalTags,
    verified: OriginalTags,
    fields: list[str],
) -> OriginalTags:
    """Adopt only fields that atomic_write_tags actually verified and wrote."""
    merged = deepcopy(current)
    for field_name in fields:
        if field_name == LYRICS_FIELD:
            merged.lyrics = verified.lyrics
        elif field_name == "artwork":
            merged.artwork = verified.artwork
        elif field_name in REPLAYGAIN_FIELDS:
            merged.replay_gain = merged.replay_gain.with_field(
                field_name, verified.replay_gain.field_value(field_name)
            )
        else:
            setattr(merged, field_name, getattr(verified, field_name))
    return merged


def _rename_error_code(exc: OSError) -> str:
    import errno
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return ApplyErrorCode.RENAME_LOCKED
    if getattr(exc, "errno", None) == errno.EEXIST:
        return ApplyErrorCode.RENAME_COLLISION
    return ApplyErrorCode.RENAME_FAILED


class MetadataRestoreWorker(QThread):
    """
    Writes backed-up original tags onto their files via
    core.metadata_processor.restore_tags. The records are parsed and the
    user has confirmed the file list *before* this worker is created, so
    run() only performs the writes.

    Signals
    -------
    progress(int, int)   (done_count, total_count)
    finished(object)     list[RestoreOutcome]
    """

    progress = Signal(int, int)
    finished = Signal(object)   # list[RestoreOutcome]

    def __init__(
        self,
        records: list[tuple[Path, OriginalTags]],
        *,
        backup_path: Path | None = None,
        operation_id: str | None = None,
        op_generation: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._records = records
        self._backup_path = backup_path
        self._operation_id = operation_id or f"restore-{uuid.uuid4().hex}"
        base = backup_path.parent if backup_path else get_tag_backup_dir()
        self._journal_path = base / f"bananaflow_tag_restore_{self._operation_id}.journal.json"
        self._op_generation = op_generation
        self._cancel  = threading.Event()

    @property
    def op_generation(self) -> int:
        return self._op_generation

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        from core.metadata_models import JournalBatchState, JournalFileState, RestoreStatus
        from core.metadata_processor import write_journal
        files = {}
        for path, _tags in self._records:
            identity = None
            try:
                stat = path.stat()
                identity = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            except OSError:
                pass
            files[str(path)] = {
                "original_path": str(path), "current_path": str(path),
                "pre_identity": identity, "state": JournalFileState.PLANNED,
            }
        source_operation_id = None
        if self._backup_path:
            try:
                import json
                raw = json.loads(self._backup_path.read_text(encoding="utf-8"))
                source_operation_id = raw.get("operation_id") if isinstance(raw, dict) else None
            except Exception:
                pass
        journal = {"schema": 1, "operation_id": self._operation_id, "operation_type": "restore",
                   "backup_path": str(self._backup_path) if self._backup_path else None,
                   "source_operation_id": source_operation_id,
                   "created": datetime.now().isoformat(timespec="seconds"),
                   "batch_state": JournalBatchState.PREPARING, "files": files}
        write_journal(self._journal_path, journal, durable=True)
        journal["batch_state"] = JournalBatchState.PREFLIGHT_COMPLETE
        write_journal(self._journal_path, journal, durable=True)
        journal["batch_state"] = JournalBatchState.METADATA_WRITING
        write_journal(self._journal_path, journal, durable=True)
        outcomes = []
        total = len(self._records)
        for index, record in enumerate(self._records):
            path, _tags = record
            entry = files[str(path)]
            if self._cancel.is_set():
                entry["state"] = JournalFileState.CANCELLED
                outcomes.append(RestoreOutcome(path, RestoreStatus.CANCELLED, "cancelled"))
                continue
            # Persist intent before each individual write.  Recovery can inspect
            # the media if the process dies before the VERIFIED transition.
            entry["state"] = JournalFileState.WRITTEN
            write_journal(self._journal_path, journal, durable=True)
            outcome = restore_tags([record], cancel_event=self._cancel)[0]
            outcomes.append(outcome)
            entry = files.get(str(outcome.path))
            if entry is not None:
                entry["state"] = (JournalFileState.VERIFIED if outcome.status in {RestoreStatus.RESTORED, RestoreStatus.UNCHANGED}
                                  else JournalFileState.CANCELLED if outcome.status == RestoreStatus.CANCELLED
                                  else JournalFileState.FAILED)
                entry["status"] = str(outcome.status)
            write_journal(self._journal_path, journal, durable=True)
            self.progress.emit(index + 1, total)
        journal["batch_state"] = JournalBatchState.METADATA_VERIFIED
        write_journal(self._journal_path, journal, durable=True)
        failed = any(getattr(item, "status", None) not in {RestoreStatus.RESTORED, RestoreStatus.UNCHANGED}
                     for item in outcomes)
        journal["batch_state"] = (JournalBatchState.CANCELLED if self._cancel.is_set()
                                  else JournalBatchState.PARTIAL if failed else JournalBatchState.COMPLETED)
        write_journal(self._journal_path, journal, durable=True)
        self.finished.emit(outcomes)


class MetadataRecoveryWorker(QThread):
    """
    Executes crash recovery from an incomplete Apply journal (option 1): rename
    each file back to its original path (never overwriting) and restore its
    original tags. Emits finished(outcomes, all_ok, journal_path); the journal
    is retired by the caller only when all_ok is True (defect 2).
    """

    # (outcomes, all_ok, journal_path, preflight_error_code)
    finished = Signal(object, bool, str, str)

    def __init__(self, journal_path: Path, *, op_generation: int = 0, parent=None) -> None:
        super().__init__(parent)
        self._journal_path = journal_path
        self._op_generation = op_generation
        self._cancel = threading.Event()

    @property
    def op_generation(self) -> int:
        return self._op_generation

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        from core.metadata_processor import execute_recovery, execute_restore_recovery, read_journal, RecoveryPreflightError
        try:
            journal = read_journal(self._journal_path)
            operation_type = journal.get("operation_type")
            if operation_type == "restore":
                execute = execute_restore_recovery
            elif operation_type == "undo_applied_batch":
                from core.undo_applied_batch import execute_undo_recovery
                execute = execute_undo_recovery
            else:
                execute = execute_recovery
            outcomes, all_ok = execute(self._journal_path, cancel_event=self._cancel)
        except RecoveryPreflightError as exc:
            # Backup invalid — NOTHING was touched on disk (blocker 4).
            logger.error("[MetadataRecoveryWorker] Recovery preflight failed: %s", exc)
            self.finished.emit([], False, str(self._journal_path), exc.code)
            return
        except Exception as exc:   # recovery must never crash the app
            logger.error("[MetadataRecoveryWorker] Recovery failed: %s", exc)
            self.finished.emit([], False, str(self._journal_path), "error")
            return
        self.finished.emit(outcomes, all_ok, str(self._journal_path), "")


class UndoAppliedBatchWorker(QThread):
    """Runs explicit disk-level Undo Applied Batch off the UI thread."""

    finished = Signal(object)

    def __init__(self, manifest_path: Path, *, restore_paths: bool = False,
                 op_generation: int = 0, parent=None) -> None:
        super().__init__(parent)
        self._manifest_path = manifest_path
        self._restore_paths = restore_paths
        self._op_generation = op_generation
        self._cancel = threading.Event()

    @property
    def op_generation(self) -> int:
        return self._op_generation

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        from core.undo_applied_batch import undo_applied_batch
        try:
            self.finished.emit(undo_applied_batch(
                self._manifest_path, restore_paths=self._restore_paths, cancel_event=self._cancel))
        except Exception as exc:
            logger.error("[UndoAppliedBatchWorker] Undo Applied Batch failed: %s", exc)
            self.finished.emit(exc)
