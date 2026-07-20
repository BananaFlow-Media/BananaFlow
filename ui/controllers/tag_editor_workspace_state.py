"""Phase 2 workspace state for the Tag Editor.

Workspace identity is deliberately independent of a track's mutable path.  A
successful Phase 1 rename updates ``AudioTrackItem.path`` in place, but the
same in-memory item must retain its selection, visibility and Apply state.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.change_sets import (
    BaselineSnapshot, ChangeCommand, ChangeOperation, ChangeOrigin, ChangeSet,
    ProposalHistory,
)
from core.filesystem_monitoring import external_state_blocks_apply
from core.metadata_models import (
    ARTWORK_FIELD, LYRICS_FIELD, REPLAYGAIN_FIELDS, AudioTrackItem,
    ArtworkOperation, ChangeAction, FieldChange,
)


class TagEditorWorkspaceState(QObject):
    """Single source of truth for loaded, selected, visible and Apply sets."""

    changed = Signal()
    selection_changed = Signal()
    visibility_changed = Signal()
    apply_scope_changed = Signal()
    change_set_changed = Signal()
    content_changed = Signal(int)
    # (item, prior_path) — emitted only when history moves an item back or
    # forward, so the model's path index follows an undone/redone relocation.
    item_relocated = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tracks: list[AudioTrackItem] = []
        self._by_id: dict[int, AudioTrackItem] = {}
        self._id_by_object: dict[int, int] = {}
        self._path_to_id: dict[Path, int] = {}
        self._selected_ids: set[int] = set()
        self._visible_ids: set[int] = set()
        self._next_item_id = 1
        self._generation = 0
        self._content_revision = 0
        self.change_set = ChangeSet()
        self.proposal_history = ProposalHistory()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def content_revision(self) -> int:
        """Monotonic workspace-content boundary independent of root generation."""
        return self._content_revision

    @property
    def change_revision(self) -> int:
        return self.change_set.revision

    def item_id(self, item: AudioTrackItem) -> int:
        """Return this workspace's immutable ID for ``item``.

        IDs are allocated per scan/workspace and are never derived from table
        rows or paths.  They deliberately are not model fields or serialized
        metadata.
        """
        identity = self._id_by_object.get(id(item))
        if identity is None:
            raise KeyError("AudioTrackItem is not loaded in this workspace")
        return identity

    def track_for_id(self, identity: int) -> AudioTrackItem | None:
        """Resolve an immutable workspace item ID without consulting its path."""
        return self._by_id.get(identity)

    @property
    def tracks(self) -> list[AudioTrackItem]:
        return list(self._tracks)

    def set_tracks(self, tracks: list[AudioTrackItem]) -> None:
        self._generation += 1
        self.change_set.clear()
        self.proposal_history.clear()
        self._tracks = list(tracks)
        self._by_id = {}
        self._id_by_object = {}
        self._next_item_id = 1
        for item in self._tracks:
            self._register_item(item)
        self._selected_ids.clear()
        self._visible_ids = set(self._by_id)
        self._sync_path_lookup()
        self.reconcile(emit=False)
        self._bump_content_revision()
        self.changed.emit()
        self.selection_changed.emit()
        self.visibility_changed.emit()
        self.apply_scope_changed.emit()

    def add_tracks(self, tracks: list[AudioTrackItem]) -> None:
        if not tracks:
            return
        added = False
        for item in tracks:
            if id(item) in self._id_by_object:
                continue
            identity = self._register_item(item)
            self._tracks.append(item)
            self._visible_ids.add(identity)
            added = True
        if not added:
            return
        self._sync_path_lookup()
        self.reconcile(emit=False)
        self._bump_content_revision()
        self.changed.emit()
        self.visibility_changed.emit()
        self.apply_scope_changed.emit()

    def track_for_path(self, path: Path) -> AudioTrackItem | None:
        self._sync_path_lookup()
        identity = self._path_to_id.get(path)
        return self._by_id.get(identity) if identity is not None else None

    def selected_tracks(self) -> list[AudioTrackItem]:
        return [item for item in self._tracks if self.item_id(item) in self._selected_ids]

    def visible_tracks(self) -> list[AudioTrackItem]:
        return [item for item in self._tracks if self.item_id(item) in self._visible_ids]

    def set_selected_paths(self, paths: set[Path] | list[Path]) -> None:
        self.set_selected_items(
            [item for path in paths if (item := self.track_for_path(path)) is not None]
        )

    def set_selected_items(self, items: list[AudioTrackItem]) -> None:
        selected = {self.item_id(item) for item in items if id(item) in self._id_by_object}
        if selected == self._selected_ids:
            return
        self._selected_ids = selected
        self.selection_changed.emit()
        self.changed.emit()

    def set_visible_paths(self, paths: set[Path] | list[Path]) -> None:
        self.set_visible_items(
            [item for path in paths if (item := self.track_for_path(path)) is not None]
        )

    def set_visible_items(self, items: list[AudioTrackItem]) -> None:
        visible = {self.item_id(item) for item in items if id(item) in self._id_by_object}
        if visible == self._visible_ids:
            return
        self._visible_ids = visible
        self.visibility_changed.emit()
        self.changed.emit()

    def edit_scope(self) -> list[AudioTrackItem]:
        """Editing actions affect selected rows only; visibility is never scope."""
        return self.selected_tracks()

    def changed_tracks(self) -> list[AudioTrackItem]:
        self.reconcile()
        return [item for item in self._tracks if item.has_changes]

    def excluded_tracks(self) -> list[AudioTrackItem]:
        self.reconcile()
        return [item for item in self._tracks if item.has_changes and item.excluded_from_apply]

    @staticmethod
    def _has_applicable_change(item: AudioTrackItem) -> bool:
        """A real pending change: a rename, or a tag edit the format accepts."""
        return bool(
            item.proposed_filename is not None
            or (item.metadata_editable and item.proposed.has_changes(item.original))
        )

    def apply_candidates(self) -> list[AudioTrackItem]:
        """Metadata writes require capability; physical renames do not.

        Selection and visibility never affect this set.  A read-only item may
        enter solely for a pending filename operation, never for tag writes.
        """
        self.reconcile()
        return [
            item for item in self._tracks
            if (not item.excluded_from_apply
                and not external_state_blocks_apply(getattr(item, "external_state", "current"))
                and self._has_applicable_change(item))
        ]

    def apply_blockers(self) -> list[AudioTrackItem]:
        """Included changed items whose disk state makes writing them unsafe.

        This is the one canonical blocker calculation.  Apply scope, preflight,
        Review and the toolbar all read it, so a count can never disagree with
        what Apply executes.  An explicitly excluded item is already out of the
        batch, so its disk state blocks nothing and its proposal stays pending.
        """
        self.reconcile()
        return [
            item for item in self._tracks
            if (item.has_changes and not item.excluded_from_apply
                and external_state_blocks_apply(getattr(item, "external_state", "current")))
        ]

    def set_apply_excluded(self, paths: list[Path] | set[Path], excluded: bool) -> None:
        self._sync_path_lookup()
        self.set_apply_excluded_ids(
            [identity for path in paths if (identity := self._path_to_id.get(path)) is not None], excluded,
        )

    def set_apply_excluded_ids(self, identities: list[int] | set[int], excluded: bool) -> None:
        before = self.proposal_checkpoint()
        changed_ids = [identity for identity in set(identities)
                       if (item := self._by_id.get(identity)) is not None
                       and item.has_changes and item.excluded_from_apply != excluded]
        if not changed_ids:
            return
        for identity in changed_ids:
            self._by_id[identity].excluded_from_apply = excluded
        self.change_set.set_excluded(changed_ids, excluded)
        after = self.change_set.snapshot(self._generation)
        self.proposal_history.push(ChangeCommand(self._generation, before, after, "apply inclusion"))
        self.change_set_changed.emit()
        self.changed.emit()
        self.apply_scope_changed.emit()

    # -- Explicit proposal change set -------------------------------------------------

    def baseline_snapshot(self, identity: int) -> BaselineSnapshot | None:
        """Capture one item's disk-derived state for a reversible transition."""
        item = self._by_id.get(identity)
        if item is None:
            return None
        return BaselineSnapshot(
            identity, str(item.path), deepcopy(item.original),
            deepcopy(item.baseline_identity), item.status, item.error_msg,
            str(item.format_id), bool(item.metadata_editable),
            str(getattr(item, "external_state", "current")),
            str(getattr(item, "external_detail", "")),
            getattr(item, "external_conflict", None),
        )

    def _restore_baselines(self, snapshots: tuple[BaselineSnapshot, ...]) -> None:
        """Reinstate captured disk baselines.  Never touches media, only memory.

        A retired ID is skipped rather than recreated, so old history can never
        resurrect an item the workspace has already removed.
        """
        relocated: list[tuple[AudioTrackItem, Path]] = []
        for snapshot in snapshots:
            item = self._by_id.get(snapshot.item_id)
            if item is None:
                continue
            restored_path = Path(snapshot.path)
            if item.path != restored_path:
                relocated.append((item, item.path))
                item.path = restored_path
                item.folder = restored_path.parent
                item.ext = restored_path.suffix.lower()
            item.original = deepcopy(snapshot.original)
            item.baseline_identity = deepcopy(snapshot.baseline_identity)
            item.status = snapshot.status
            item.error_msg = snapshot.error_msg
            item.format_id = snapshot.format_id
            item.metadata_editable = snapshot.metadata_editable
            item.external_state = snapshot.external_state
            item.external_detail = snapshot.external_detail
            item.external_conflict = snapshot.external_conflict
        if not snapshots:
            return
        self._sync_path_lookup()
        self._bump_content_revision()
        for item, prior in relocated:
            self.item_relocated.emit(item, prior)

    def capture_proposals(
        self,
        items: list[AudioTrackItem],
        origin: ChangeOrigin = ChangeOrigin.MANUAL,
        *,
        label: str = "",
        before=None,
        source: dict[str, str] | None = None,
        field_sources: dict[tuple[int, str], dict[str, str] | None] | None = None,
        baselines_before: tuple[BaselineSnapshot, ...] = (),
        baselines_after: tuple[BaselineSnapshot, ...] = (),
    ) -> None:
        """Synchronise an intentional proposal action into the canonical ledger.

        The existing ``ProposedTags`` object remains the format-aware write
        adapter during this migration.  This method is its only bridge into
        the explicit, stable-ID change model and is deliberately called once
        per user action, not once per individual field in a batch.
        """
        before = before or self.change_set.snapshot(self._generation)
        for item in items:
            if id(item) not in self._id_by_object:
                continue
            self._sync_item_changes(item, origin, source=source, field_sources=field_sources)
        after = self.change_set.snapshot(self._generation)
        self.proposal_history.push(ChangeCommand(
            self._generation, before, after, label, baselines_before, baselines_after))
        self.change_set_changed.emit()
        self.changed.emit()
        self.apply_scope_changed.emit()

    def synchronize_proposals(
        self, items: list[AudioTrackItem], origin: ChangeOrigin = ChangeOrigin.MANUAL,
    ) -> None:
        """Refresh active records without manufacturing an undo command.

        Apply reconciliation uses this after verified worker outcomes.  Disk
        state is a history boundary, not an in-memory editing action.
        """
        for item in items:
            if id(item) in self._id_by_object:
                self._sync_item_changes(item, origin)
        self.change_set_changed.emit()
        self.changed.emit()
        self.apply_scope_changed.emit()

    def proposal_checkpoint(self):
        """Capture a pre-action snapshot for a later ``capture_proposals`` call."""
        return self.change_set.snapshot(self._generation)

    def can_undo_proposals(self) -> bool:
        return self.proposal_history.can_undo(self._generation)

    def can_redo_proposals(self) -> bool:
        return self.proposal_history.can_redo(self._generation)

    def undo_proposals(self) -> bool:
        command = self.proposal_history.undo(self.change_set, self._generation)
        if command is None:
            return False
        # Baselines first: the proposal projection reads item.original, so the
        # disk baseline an external rebase installed must be reverted before
        # the Change Set is replayed onto it.
        self._restore_baselines(command.baselines_before)
        self._apply_change_snapshot()
        return True

    def redo_proposals(self) -> bool:
        command = self.proposal_history.redo(self.change_set, self._generation)
        if command is None:
            return False
        self._restore_baselines(command.baselines_after)
        self._apply_change_snapshot()
        return True

    def clear_applied_history(self) -> None:
        """Apply is a hard boundary: it must never be undone as a disk write."""
        self.proposal_history.clear()

    def restore_draft_snapshot(self, snapshot) -> bool:
        """Restore proposals; orphan IDs remain review-visible but never apply."""
        self.change_set.restore(snapshot)
        self.proposal_history.clear()
        self._apply_change_snapshot()
        return True

    def revert_items(self, items: list[AudioTrackItem], fields: set[str] | None = None) -> None:
        before = self.proposal_checkpoint()
        ids = {self.item_id(item) for item in items if id(item) in self._id_by_object}
        for identity in ids:
            for record in list(self.change_set.records(item_ids={identity})):
                if fields is None or record.field in fields:
                    self.change_set.remove(identity, record.field)
        self._apply_change_snapshot()
        after = self.change_set.snapshot(self._generation)
        self.proposal_history.push(ChangeCommand(self._generation, before, after, "revert"))

    def revert_record_targets(self, targets: dict[int, set[str] | None]) -> None:
        """Revert a review-table batch in one undoable user command."""
        before = self.proposal_checkpoint()
        for identity, fields in targets.items():
            for record in list(self.change_set.records(item_ids={identity})):
                if fields is None or record.field in fields:
                    self.change_set.remove(identity, record.field)
        self._apply_change_snapshot()
        after = self.change_set.snapshot(self._generation)
        self.proposal_history.push(ChangeCommand(self._generation, before, after, "review revert"))

    def _sync_item_changes(self, item: AudioTrackItem, origin: ChangeOrigin,
                           *, source: dict[str, str] | None = None,
                           field_sources: dict[tuple[int, str], dict[str, str] | None] | None = None) -> None:
        identity = self.item_id(item)
        active = set(item.proposed.changed_fields(item.original))
        if item.proposed_filename is not None and item.proposed_filename != item.path.name:
            active.add("filename")
        existing_records = {record.field: record for record in self.change_set.records(item_ids={identity})}
        for record in list(existing_records.values()):
            if record.field not in active:
                self.change_set.remove(identity, record.field)
        for field_name in active:
            explicit_key = (identity, field_name)
            if explicit_key in (field_sources or {}):
                field_source = (field_sources or {}).get(explicit_key) or {}
            elif source is not None:
                field_source = source
            else:
                old = existing_records.get(field_name)
                field_source = {} if old is None else {
                    "provider": old.source_provider, "attribution": old.source_attribution,
                    "url": old.source_url,
                }
            field_origin = origin if (explicit_key in (field_sources or {}) or source is not None) else (
                existing_records[field_name].origin if field_name in existing_records else origin)
            original, proposed, operation = self._proposal_values(item, field_name)
            self.change_set.record(
                identity, field_name, original, proposed, operation=operation,
                origin=field_origin, equal=self._values_equal,
                capability="" if item.metadata_editable or field_name == "filename" else "read_only",
                source_provider=str(field_source.get("provider", "")),
                source_attribution=str(field_source.get("attribution", "")),
                source_url=str(field_source.get("url", "")),
            )
        if item.excluded_from_apply:
            self.change_set.set_excluded({identity}, True)

    @staticmethod
    def _values_equal(field_name: str, left, right) -> bool:
        from core.metadata_models import metadata_values_equal
        return metadata_values_equal(field_name, left, right)

    @staticmethod
    def _proposal_values(item: AudioTrackItem, field_name: str):
        if field_name == "filename":
            return item.path.name, item.proposed_filename, ChangeOperation.RENAME
        original = item.original.field_value(field_name)
        proposed = item.proposed.effective_tags(item.original).field_value(field_name)
        operation = ChangeOperation.SET
        if field_name == ARTWORK_FIELD:
            art_op = getattr(proposed, "operation", ArtworkOperation.REPLACE)
            operation = {
                ArtworkOperation.ADD: ChangeOperation.ADD,
                ArtworkOperation.REMOVE: ChangeOperation.REMOVE,
                ArtworkOperation.REPLACE: ChangeOperation.REPLACE,
            }.get(art_op, ChangeOperation.REPLACE)
        elif field_name == LYRICS_FIELD:
            operation = (ChangeOperation.CLEAR if item.proposed.lyrics_change.action == ChangeAction.CLEAR
                         else ChangeOperation.SET)
        elif field_name in REPLAYGAIN_FIELDS:
            change = item.proposed.replay_gain_changes.get(field_name)
            operation = ChangeOperation.CLEAR if change and change.action == ChangeAction.CLEAR else ChangeOperation.SET
        elif proposed in ("", None):
            operation = ChangeOperation.CLEAR
        return original, proposed, operation

    def _apply_change_snapshot(self) -> None:
        """Project the ledger back to the established format-aware proposal API."""
        by_id = {record.item_id: [] for record in self.change_set.records()}
        for record in self.change_set.records():
            by_id.setdefault(record.item_id, []).append(record)
        for identity, item in self._by_id.items():
            item.proposed.clear()
            item.proposed_filename = None
            item.excluded_from_apply = identity in self.change_set.snapshot(self._generation).excluded_ids
            for record in by_id.get(identity, []):
                field_name, value = record.field, record.proposed_value
                if field_name == "filename":
                    item.proposed_filename = str(value)
                elif field_name == LYRICS_FIELD:
                    if record.operation == ChangeOperation.CLEAR:
                        item.proposed.clear_lyrics()
                    else:
                        item.proposed.set_lyrics(value, original=item.original.lyrics)
                elif field_name == ARTWORK_FIELD:
                    action = ChangeAction.CLEAR if record.operation == ChangeOperation.REMOVE else ChangeAction.SET
                    item.proposed.artwork_change = FieldChange(action, value)
                elif field_name in REPLAYGAIN_FIELDS:
                    if record.operation == ChangeOperation.CLEAR:
                        item.proposed.clear_replay_gain({field_name})
                    else:
                        item.proposed.set_replay_gain(field_name, value)
                else:
                    if record.operation == ChangeOperation.CLEAR and value is None:
                        value = -1 if field_name in {"track_num", "track_total", "disc_num", "disc_total", "bpm"} else ""
                    setattr(item.proposed, field_name, value)
        self.reconcile(emit=False)
        self.change_set_changed.emit()
        self.changed.emit()
        self.apply_scope_changed.emit()

    def remove_paths(self, paths: list[Path] | set[Path]) -> None:
        """Remove deleted items without resetting unrelated stable identities."""
        self._sync_path_lookup()
        doomed = {self._path_to_id.get(path) for path in paths}
        doomed.discard(None)
        self.remove_item_ids(doomed)

    def remove_item_ids(self, identities) -> list[AudioTrackItem]:
        """Retire items and their proposal/history state without a root reset."""
        doomed = {int(identity) for identity in identities if identity in self._by_id}
        if not doomed:
            return []
        removed = [item for item in self._tracks if self.item_id(item) in doomed]
        self._tracks = [item for item in self._tracks if self.item_id(item) not in doomed]
        for identity in doomed:
            item = self._by_id.pop(identity)
            self._id_by_object.pop(id(item), None)
        self._selected_ids.difference_update(doomed)
        self._visible_ids.difference_update(doomed)
        self.change_set.remove_items(doomed)
        self.proposal_history.prune_item_ids(doomed)
        self._sync_path_lookup()
        self._bump_content_revision()
        self.change_set_changed.emit()
        self.changed.emit()
        self.selection_changed.emit()
        self.visibility_changed.emit()
        self.apply_scope_changed.emit()
        return removed

    def update_from_disk(self, identity: int, refreshed: AudioTrackItem, *,
                         external_state: str = "changed_on_disk",
                         detail: str = "") -> AudioTrackItem | None:
        """Refresh canonical disk data on the existing stable item identity."""
        item = self._by_id.get(identity)
        if item is None:
            return None
        item.path = Path(refreshed.path)
        item.folder = Path(refreshed.folder)
        item.ext = refreshed.ext
        item.original = deepcopy(refreshed.original)
        item.status = refreshed.status
        item.error_msg = refreshed.error_msg
        item.format_id = refreshed.format_id
        item.metadata_editable = refreshed.metadata_editable
        item.baseline_identity = deepcopy(refreshed.baseline_identity)
        item.external_state = str(external_state)
        item.external_detail = str(detail)
        item.external_conflict = None
        self._sync_path_lookup()
        self._bump_content_revision()
        self.changed.emit()
        return item

    def relocate_item(self, identity: int, destination: Path, *,
                      refreshed: AudioTrackItem | None = None,
                      external_state: str = "moved", detail: str = "") -> AudioTrackItem | None:
        """Move one logical item to a proven path without allocating a new ID."""
        item = self._by_id.get(identity)
        if item is None:
            return None
        if refreshed is not None and not item.has_changes:
            item.original = deepcopy(refreshed.original)
            item.status = refreshed.status
            item.error_msg = refreshed.error_msg
            item.format_id = refreshed.format_id
            item.metadata_editable = refreshed.metadata_editable
            item.baseline_identity = deepcopy(refreshed.baseline_identity)
        item.path = Path(destination)
        item.folder = item.path.parent
        item.ext = item.path.suffix.lower()
        item.external_state = str(external_state)
        item.external_detail = str(detail)
        self._sync_path_lookup()
        self._bump_content_revision()
        self.changed.emit()
        return item

    def set_external_state(self, identity: int, state: str, *, detail: str = "",
                           conflict=None) -> AudioTrackItem | None:
        item = self._by_id.get(identity)
        if item is None:
            return None
        if (item.external_state == str(state) and item.external_detail == str(detail)
                and item.external_conflict == conflict):
            return item
        item.external_state = str(state)
        item.external_detail = str(detail)
        item.external_conflict = conflict
        self._bump_content_revision()
        self.changed.emit()
        self.apply_scope_changed.emit()
        return item

    def reload_item_from_disk(self, identity: int,
                              refreshed: AudioTrackItem) -> AudioTrackItem | None:
        """Explicitly discard one item's proposals and install disk truth."""
        item = self._by_id.get(identity)
        if item is None:
            return None
        self.change_set.remove_items({identity})
        self.proposal_history.prune_item_ids({identity})
        item.proposed.clear()
        item.proposed_filename = None
        item.excluded_from_apply = False
        updated = self.update_from_disk(
            identity, refreshed, external_state="current", detail="")
        self.change_set_changed.emit()
        self.apply_scope_changed.emit()
        return updated

    def safely_rebase_item(self, identity: int, refreshed: AudioTrackItem, *,
                           baseline_before: BaselineSnapshot | None = None,
                           ) -> AudioTrackItem | None:
        """Install a refreshed baseline while preserving local proposal evidence.

        A safe rebase only happens when no field changed both locally and on
        disk, so the proposals themselves usually do not move — but the disk
        baseline, identity, capability and (after a relocation) the path all do.
        Those form the reversible part of this transition and are recorded as
        one command, so Undo restores the old baseline and Redo the new one.
        ``baseline_before`` lets a caller that relocated the item first supply
        the pre-relocation state, keeping move-plus-rebase a single command.
        """
        item = self._by_id.get(identity)
        if item is None:
            return None
        before = self.proposal_checkpoint()
        baselines_before = (baseline_before or self.baseline_snapshot(identity),)
        item.original = deepcopy(refreshed.original)
        item.status = refreshed.status
        item.error_msg = refreshed.error_msg
        item.format_id = refreshed.format_id
        item.metadata_editable = refreshed.metadata_editable
        item.baseline_identity = deepcopy(refreshed.baseline_identity)
        item.external_state = "current"
        item.external_detail = ""
        item.external_conflict = None
        self._sync_path_lookup()
        self.capture_proposals(
            [item], ChangeOrigin.MANUAL, label="external rebase", before=before,
            baselines_before=baselines_before,
            baselines_after=(self.baseline_snapshot(identity),),
        )
        self._bump_content_revision()
        return item

    def reconcile_apply_outcome(self, outcome) -> AudioTrackItem | None:
        """Synchronize current-path lookup after a Phase 1 Apply outcome.

        The worker has already mutated the item's path by the time a completed
        rename outcome reaches this method.  Rebuilding only the path lookup
        preserves all ID-backed state while making the old path immediately
        unresolvable and the final path immediately resolvable.
        """
        self._sync_path_lookup()
        item = self.track_for_path(outcome.final_path)
        if item is None and outcome.final_path == outcome.original_path:
            item = self.track_for_path(outcome.original_path)
        if item is not None:
            self.reconcile(emit=False)
            self.changed.emit()
        return item

    def reconcile(self, emit: bool = True) -> None:
        """Clear exclusions that outlived a proposal, restoring auto-inclusion."""
        cleared = False
        for item in self._tracks:
            if not item.has_changes and item.excluded_from_apply:
                item.excluded_from_apply = False
                cleared = True
        if cleared and emit:
            self.changed.emit()
            self.apply_scope_changed.emit()

    def _register_item(self, item: AudioTrackItem) -> int:
        identity = self._next_item_id
        self._next_item_id += 1
        self._by_id[identity] = item
        self._id_by_object[id(item)] = identity
        return identity

    def _sync_path_lookup(self) -> None:
        self._path_to_id = {item.path: self.item_id(item) for item in self._tracks}

    def _bump_content_revision(self) -> None:
        self._content_revision += 1
        self.content_changed.emit(self._content_revision)
