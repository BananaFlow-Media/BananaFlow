"""Ordered, revision-gated application of Phase 13 refresh plans."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from core.file_refresh_service import (
    ConflictResolutionAction, FileRefreshService, RefreshAction,
    WorkspaceRefreshPlan, analyze_stale_file, snapshot_workspace_files,
)
from core.filesystem_monitoring import (
    equivalent_file_identity, inspect_filesystem_path, path_within_root,
    strong_same_file,
)


@dataclass(frozen=True)
class IncrementalUpdateSummary:
    accepted: bool
    added: int = 0
    removed: int = 0
    refreshed: int = 0
    relocated: int = 0
    conflicted: int = 0
    marked: int = 0
    diagnostic: str = ""


class IncrementalWorkspaceUpdater:
    """Apply one immutable plan to the established workspace/model identity."""

    def __init__(self, workspace, model, navigation=None) -> None:
        self.workspace = workspace
        self.model = model
        self.navigation = navigation

    def apply(self, plan: WorkspaceRefreshPlan, *, root: Path) -> IncrementalUpdateSummary:
        diagnostic = self._validate_plan(plan, Path(root))
        if diagnostic:
            return IncrementalUpdateSummary(False, diagnostic=diagnostic)

        counts = {"added": 0, "removed": 0, "refreshed": 0,
                  "relocated": 0, "conflicted": 0, "marked": 0}
        previous_blocked = self.workspace.blockSignals(True)
        conflict_items = []
        try:
            # Removing high-level model paths first keeps every surviving ID
            # stable. Other results are ID-based and therefore row-independent.
            removals = [result for result in plan.results
                        if result.action is RefreshAction.REMOVE]
            if removals:
                self.model.remove_paths({result.path for result in removals})
                counts["removed"] += len(removals)

            for result in plan.results:
                if result.action in {RefreshAction.NO_CHANGE, RefreshAction.REMOVE}:
                    continue
                if result.action is RefreshAction.ADD:
                    if result.refreshed_item is not None:
                        result.refreshed_item.external_state = result.state.value
                        result.refreshed_item.external_detail = result.diagnostic
                        self.model.add_tracks([result.refreshed_item])
                        counts["added"] += 1
                    continue
                item = self.workspace.track_for_id(result.item_id)
                if item is None:
                    raise RuntimeError("refresh_target_retired")
                if result.action is RefreshAction.UPDATE:
                    updated = self.workspace.update_from_disk(
                        result.item_id, result.refreshed_item,
                        external_state=result.state.value, detail=result.diagnostic)
                    if updated is not None:
                        self.model.refresh_track(updated)
                        counts["refreshed"] += 1
                elif result.action is RefreshAction.RELOCATE:
                    prior = Path(result.prior_path or item.path)
                    updated = self.workspace.relocate_item(
                        result.item_id, result.path, refreshed=result.refreshed_item,
                        external_state=result.state.value, detail=result.diagnostic)
                    if updated is not None:
                        updated.external_conflict = result.conflict
                        if result.conflict is not None:
                            conflict_items.append(updated)
                        self.model.reindex_track_path(updated, prior)
                        counts["relocated"] += 1
                        if result.conflict is not None:
                            counts["conflicted"] += 1
                elif result.action is RefreshAction.CONFLICT:
                    updated = self.workspace.set_external_state(
                        result.item_id, result.state.value,
                        detail=result.diagnostic, conflict=result.conflict)
                    if updated is not None:
                        self.model.refresh_track(updated)
                        counts["conflicted"] += 1
                        conflict_items.append(updated)
                elif result.action is RefreshAction.MARK_STATE:
                    updated = self.workspace.set_external_state(
                        result.item_id, result.state.value,
                        detail=result.diagnostic, conflict=result.conflict)
                    if updated is not None:
                        self.model.refresh_track(updated)
                        counts["marked"] += 1
        finally:
            self.workspace.blockSignals(previous_blocked)

        for item in conflict_items:
            conflict = item.external_conflict
            if conflict is not None:
                item.external_conflict = replace(
                    conflict, content_revision=self.workspace.content_revision,
                    change_revision=self.workspace.change_revision,
                )

        # One coherent visibility/proposal/content publication follows the
        # ordered batch; the proxy therefore performs one explicit refilter.
        self.workspace.content_changed.emit(self.workspace.content_revision)
        self.workspace.change_set_changed.emit()
        self.workspace.selection_changed.emit()
        self.workspace.visibility_changed.emit()
        self.workspace.apply_scope_changed.emit()
        self.workspace.changed.emit()
        if self.navigation is not None:
            self.navigation.reconcile_filesystem()
        return IncrementalUpdateSummary(True, **counts)

    def resolve_conflict(self, conflict, action: ConflictResolutionAction, *,
                         root: Path, session_id: str,
                         target_path: Path | None = None) -> IncrementalUpdateSummary:
        """Revalidate and apply one explicit conflict choice."""
        if conflict.session_id and conflict.session_id != session_id:
            return IncrementalUpdateSummary(False, diagnostic="stale_session")
        if (conflict.generation != self.workspace.generation
                or conflict.content_revision != self.workspace.content_revision
                or conflict.change_revision != self.workspace.change_revision):
            return IncrementalUpdateSummary(False, diagnostic="stale_conflict")
        item = self.workspace.track_for_id(conflict.item_id)
        if item is None or item.external_conflict is None:
            return IncrementalUpdateSummary(False, diagnostic="retired_conflict")
        if action in {ConflictResolutionAction.REVIEW, ConflictResolutionAction.CANCEL}:
            return IncrementalUpdateSummary(False, diagnostic="no_resolution")

        observed = Path(target_path or conflict.observed_path)
        if not path_within_root(root, observed):
            return IncrementalUpdateSummary(False, diagnostic="outside_root")
        evidence = inspect_filesystem_path(root, observed)
        if action is ConflictResolutionAction.REMOVE_MISSING:
            if evidence.exists or conflict.state.value != "missing":
                return IncrementalUpdateSummary(False, diagnostic="not_missing")
            self.model.remove_paths({item.path})
            if self.navigation is not None:
                self.navigation.reconcile_filesystem()
            return IncrementalUpdateSummary(True, removed=1)
        if not evidence.exists or evidence.placeholder or evidence.unreadable:
            return IncrementalUpdateSummary(False, diagnostic="disk_item_unavailable")
        if action is ConflictResolutionAction.LOCATE_MOVED:
            if target_path is None or not strong_same_file(
                    conflict.baseline_identity, evidence.identity):
                return IncrementalUpdateSummary(False, diagnostic="moved_identity_not_proven")
        elif not equivalent_file_identity(conflict.disk_identity, evidence.identity):
            return IncrementalUpdateSummary(False, diagnostic="disk_changed_after_review")

        disk_item = FileRefreshService.read_stable_item(evidence)
        if disk_item is None:
            return IncrementalUpdateSummary(False, diagnostic="unstable_disk_item")
        current_snapshot = next(
            (value for value in snapshot_workspace_files(self.workspace)
             if value.item_id == conflict.item_id), None)
        if current_snapshot is None:
            return IncrementalUpdateSummary(False, diagnostic="retired_conflict")

        if action is ConflictResolutionAction.RELOAD:
            prior = item.path
            if observed != item.path:
                self.workspace.relocate_item(conflict.item_id, observed,
                                             external_state="refreshing")
                self.model.reindex_track_path(item, prior)
            self.workspace.reload_item_from_disk(conflict.item_id, disk_item)
            self.model.refresh_track(item)
            return IncrementalUpdateSummary(True, refreshed=1)

        if action in {ConflictResolutionAction.KEEP_LOCAL,
                      ConflictResolutionAction.LOCATE_MOVED}:
            refreshed_conflict = analyze_stale_file(
                current_snapshot, disk_item, evidence.identity,
                observed_path=observed, state=conflict.state,
                generation=self.workspace.generation,
                content_revision=self.workspace.content_revision,
                change_revision=self.workspace.change_revision,
                session_id=session_id,
            )
            if not refreshed_conflict.safe_rebase:
                return IncrementalUpdateSummary(False, diagnostic="overlapping_changes")
            prior = item.path
            # Captured before the relocation so the move and the rebase undo
            # together as one transition rather than leaving a half-reverted item.
            baseline_before = self.workspace.baseline_snapshot(conflict.item_id)
            if observed != item.path:
                self.workspace.relocate_item(conflict.item_id, observed,
                                             external_state="refreshing")
                self.model.reindex_track_path(item, prior)
            self.workspace.safely_rebase_item(conflict.item_id, disk_item,
                                              baseline_before=baseline_before)
            self.model.refresh_track(item)
            return IncrementalUpdateSummary(
                True, refreshed=1, relocated=int(observed != prior))
        return IncrementalUpdateSummary(False, diagnostic="unsupported_resolution")

    def _validate_plan(self, plan: WorkspaceRefreshPlan, root: Path) -> str:
        if plan.generation != self.workspace.generation:
            return "stale_generation"
        if plan.content_revision != self.workspace.content_revision:
            return "stale_content_revision"
        if plan.change_revision != self.workspace.change_revision:
            return "stale_change_revision"
        for result in plan.results:
            if result.item_id is not None:
                item = self.workspace.track_for_id(result.item_id)
                if item is None:
                    return "retired_target"
                expected_path = Path(result.prior_path or result.path)
                if item.path != expected_path:
                    return "retargeted_item"
            if result.evidence is None:
                continue
            current = inspect_filesystem_path(root, result.path)
            if result.action is RefreshAction.REMOVE:
                if current.exists:
                    return "filesystem_changed_after_plan"
            elif result.evidence.exists != current.exists:
                return "filesystem_changed_after_plan"
            elif result.evidence.identity is not None and not equivalent_file_identity(
                    result.evidence.identity, current.identity):
                return "filesystem_changed_after_plan"
        return ""
