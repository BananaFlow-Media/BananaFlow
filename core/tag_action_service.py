"""Proposal-only bridge between declarative actions and the canonical Change Set."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from core.change_sets import ChangeOrigin
from core.metadata_models import TrackStatus
from core.metadata_processor import plan_renames
from core.tag_actions import ActionDelta, ActionResultStatus, TagActionContext, TagActionRegistry


@dataclass(frozen=True)
class ActionPreview:
    generation: int
    revision: int
    action_id: str
    deltas: tuple[ActionDelta, ...]
    scope: str
    target_ids: tuple[int, ...]
    scope_token: tuple[int, ...]
    origin: ChangeOrigin = ChangeOrigin.TEMPLATE
    blocked: tuple[tuple[int, str], ...] = ()
    rename_steps: tuple[tuple[str, str], ...] = ()

    @property
    def collisions(self) -> frozenset[int]:
        return frozenset(identity for identity, code in self.blocked if "collision" in str(code).lower())

    @property
    def changed_count(self) -> int:
        return sum(delta.status is ActionResultStatus.CHANGED for delta in self.deltas)


class TagActionService:
    """Evaluates actions without touching proposals, files or media."""

    def __init__(self, registry: TagActionRegistry) -> None:
        self.registry = registry

    def preview(self, workspace, action_id: str, *, item_ids: Iterable[int] | None = None,
                parameters: dict[str, object] | None = None, scope: str = "selected",
                current_item_id: int | None = None, active_folder: Path | None = None) -> ActionPreview:
        action = self.registry.get(action_id)
        explicit_ids = item_ids is not None
        ids, scope_token = self._resolve_scope(
            workspace, scope, item_ids=item_ids, current_item_id=current_item_id,
            active_folder=active_folder,
        )
        deltas: list[ActionDelta] = []
        for sequence_index, identity in enumerate(ids, 1):
            item = workspace.track_for_id(identity)
            if item is None:
                continue
            effective = item.proposed.effective_tags(item.original)
            values = {field: effective.field_value(field) for field in action.reads
                      if field not in {"filename", "original_stem"}}
            delta = action.evaluate(TagActionContext(identity, item.path.name, item.path.suffix,
                                                      getattr(item, "format_id", item.ext.lstrip(".")), values,
                                                      item.folder.name, item.folder.parent.name,
                                                      item.status != TrackStatus.UNSUPPORTED and item.metadata_editable,
                                                      sequence_index), parameters)
            deltas.append(delta)
        blocked, steps = self._plan_filename_preview(workspace, deltas)
        origin = (ChangeOrigin.TEMPLATE if action.category == "template"
                  else ChangeOrigin.FILENAME if action.renames
                  else ChangeOrigin.AUTO_ARRANGE if action.id == "tag.auto_arrange.v1"
                  else ChangeOrigin.CLEANUP)
        return ActionPreview(
            workspace.generation, workspace.change_set.revision, action.id, tuple(deltas),
            "explicit" if explicit_ids else scope,
            tuple(ids), scope_token, origin, tuple(sorted(blocked.items())), tuple(steps),
        )

    def preview_sequence(self, workspace, steps, *, item_ids: Iterable[int] | None = None,
                         scope: str = "selected", current_item_id: int | None = None,
                         active_folder: Path | None = None) -> ActionPreview:
        """Compose an immutable preset/action sequence without touching proposals."""
        explicit_ids = item_ids is not None
        ids, scope_token = self._resolve_scope(
            workspace, scope, item_ids=item_ids, current_item_id=current_item_id,
            active_folder=active_folder,
        )
        final: list[ActionDelta] = []
        for sequence_index, identity in enumerate(ids, 1):
            item = workspace.track_for_id(identity)
            if item is None:
                continue
            effective = item.proposed.effective_tags(item.original)
            values = {field: effective.field_value(field) for field in (
                "title", "artist", "album", "album_artist", "track_num", "disc_num",
                "year", "genre", "comment", "composer",
            )}
            filename = item.proposed_filename or item.path.name
            diagnostic = ""
            status = ActionResultStatus.NO_OP
            for step in steps:
                action = self.registry.get(step.action_id)
                delta = action.evaluate(TagActionContext(
                    identity, filename, item.path.suffix,
                    getattr(item, "format_id", item.ext.lstrip(".")), values,
                    item.folder.name, item.folder.parent.name,
                    item.status != TrackStatus.UNSUPPORTED and item.metadata_editable,
                    sequence_index,
                ), step.parameters)
                if delta.status in {ActionResultStatus.BLOCKER, ActionResultStatus.UNSUPPORTED}:
                    diagnostic, status = delta.diagnostic, delta.status
                    break
                if delta.status is ActionResultStatus.WARNING:
                    diagnostic, status = delta.diagnostic, delta.status
                    break
                if delta.status is ActionResultStatus.CHANGED:
                    values.update(delta.fields)
                    filename = delta.filename or filename
                    status = ActionResultStatus.CHANGED
            original = item.proposed.effective_tags(item.original)
            changed_fields = {key: value for key, value in values.items()
                              if original.field_value(key) != value}
            final_filename = filename if filename != (item.proposed_filename or item.path.name) else None
            if status is ActionResultStatus.CHANGED and not changed_fields and not final_filename:
                status = ActionResultStatus.NO_OP
            final.append(ActionDelta(identity, changed_fields, final_filename, diagnostic, status))
        blocked, rename_steps = self._plan_filename_preview(workspace, final)
        return ActionPreview(
            workspace.generation, workspace.change_set.revision, "preset.sequence.v1", tuple(final),
            "explicit" if explicit_ids else scope, tuple(ids), scope_token,
            ChangeOrigin.AUTO_ARRANGE, tuple(sorted(blocked.items())), tuple(rename_steps),
        )

    @staticmethod
    def _resolve_scope(workspace, scope: str, *, item_ids=None, current_item_id=None,
                       active_folder: Path | None = None) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if item_ids is not None:
            ids = tuple(dict.fromkeys(int(value) for value in item_ids))
            return ids, ids
        if scope == "current":
            ids = (current_item_id,) if current_item_id is not None else ()
        elif scope == "selected":
            ids = tuple(workspace.item_id(item) for item in workspace.selected_tracks())
        elif scope == "visible":
            ids = tuple(workspace.item_id(item) for item in workspace.visible_tracks())
        elif scope == "active_folder":
            ids = tuple(workspace.item_id(item) for item in workspace.tracks
                        if active_folder is not None and item.folder == active_folder)
        else:
            raise ValueError(f"unsupported_scope:{scope}")
        return ids, ids

    @staticmethod
    def _plan_filename_preview(workspace, deltas: list[ActionDelta]) -> tuple[dict[int, str], list[tuple[str, str]]]:
        targets = {delta.item_id: delta.filename for delta in deltas
                   if delta.status is ActionResultStatus.CHANGED and delta.filename}
        if not targets:
            return {}, []
        clones = []
        path_to_id: dict[str, int] = {}
        for item in workspace.tracks:
            identity = workspace.item_id(item)
            proposed = targets.get(identity, item.proposed_filename)
            clones.append(SimpleNamespace(path=item.path, proposed_filename=proposed))
            path_to_id[str(item.path)] = identity
        plan = plan_renames(clones)
        blocked = {path_to_id[path]: str(code) for path, code in plan.blocked.items()
                   if path in path_to_id and path_to_id[path] in targets}
        steps = [(str(source), str(destination)) for source, destination in plan.steps]
        return blocked, steps

    @staticmethod
    def _current_scope_token(workspace, preview: ActionPreview) -> tuple[int, ...]:
        if preview.scope == "selected":
            return tuple(workspace.item_id(item) for item in workspace.selected_tracks())
        if preview.scope == "visible":
            return tuple(workspace.item_id(item) for item in workspace.visible_tracks())
        # Current/active-folder targets are explicit immutable IDs by preview time.
        return preview.scope_token

    def accept(self, workspace, preview: ActionPreview) -> bool:
        """Apply a fresh preview as one undoable canonical proposal command."""
        if (preview.generation != workspace.generation
                or preview.revision != workspace.change_set.revision
                or self._current_scope_token(workspace, preview) != preview.scope_token):
            return False
        before = workspace.proposal_checkpoint()
        touched = []
        explicit_origins = {}
        blocked_ids = {identity for identity, _code in preview.blocked}
        for delta in preview.deltas:
            if delta.status is not ActionResultStatus.CHANGED or delta.item_id in blocked_ids:
                continue
            item = workspace.track_for_id(delta.item_id)
            if item is None:
                continue
            for field, value in delta.fields.items():
                if hasattr(item.proposed, field):
                    setattr(item.proposed, field, value)
                    explicit_origins[(delta.item_id, field)] = None
            if delta.filename and delta.filename != item.path.name:
                item.proposed_filename = delta.filename
                explicit_origins[(delta.item_id, "filename")] = None
            touched.append(item)
        if touched:
            workspace.capture_proposals(touched, preview.origin,
                                        label=f"action:{preview.action_id}", before=before,
                                        field_sources=explicit_origins)
        return bool(touched)
