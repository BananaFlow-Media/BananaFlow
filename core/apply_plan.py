"""Frozen Phase 8 Apply preflight plan built from the authoritative ChangeSet."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.change_sets import ChangeSetSnapshot, FileIdentity, file_identity_status
from core.metadata_processor import plan_renames


class ApplyPlanBlocked(ValueError):
    def __init__(self, blocked: dict[int, str]) -> None:
        super().__init__("apply plan is blocked")
        self.blocked = blocked


@dataclass(frozen=True)
class ApplyPlanItem:
    item_id: int
    source_path: Path
    intended_path: Path
    identity: FileIdentity | None
    fields: tuple[str, ...]
    revisions: tuple[int, ...]
    included: bool
    metadata_editable: bool


@dataclass(frozen=True)
class ApplyPlan:
    operation_id: str
    generation: int
    change_revision: int
    snapshot: ChangeSetSnapshot
    items: tuple[ApplyPlanItem, ...]
    rename_plan: object

    def validate_current(self, workspace) -> dict[int, str]:
        """Reject review-invalidating mutation or stale source before writes."""
        blocked: dict[int, str] = {}
        if workspace.generation != self.generation:
            return {item.item_id: "workspace_replaced" for item in self.items}
        if workspace.change_set.revision != self.change_revision:
            return {item.item_id: "proposal_changed" for item in self.items}
        for item in self.items:
            state = file_identity_status(item.identity, item.source_path)
            if state not in {"current", "unavailable"}:
                blocked[item.item_id] = state
        for path, code in self.rename_plan.blocked.items():
            loaded = workspace.track_for_path(Path(path))
            if loaded is not None:
                blocked[workspace.item_id(loaded)] = code
        return blocked


def build_apply_plan(workspace, *, operation_id: str) -> ApplyPlan:
    snapshot = workspace.change_set.snapshot(workspace.generation)
    records_by_id: dict[int, list] = {}
    for record in snapshot.records:
        records_by_id.setdefault(record.item_id, []).append(record)
    items: list[ApplyPlanItem] = []
    tracks = []
    for identity, records in records_by_id.items():
        track = workspace.track_for_id(identity)
        if track is None or identity in snapshot.excluded_ids:
            continue
        fields = tuple(sorted(record.field for record in records))
        intended = track.path.parent / track.proposed_filename if track.proposed_filename else track.path
        items.append(ApplyPlanItem(identity, track.path, intended, track.baseline_identity, fields,
                                   tuple(record.revision for record in records), True,
                                   track.metadata_editable))
        tracks.append(track)
    rename = plan_renames(tracks)
    plan = ApplyPlan(operation_id, workspace.generation, snapshot.revision, snapshot, tuple(items), rename)
    blocked = plan.validate_current(workspace)
    if blocked:
        raise ApplyPlanBlocked(blocked)
    return plan
