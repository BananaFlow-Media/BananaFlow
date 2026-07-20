from __future__ import annotations

from pathlib import Path

import pytest

from core.apply_plan import ApplyPlanBlocked, build_apply_plan
from core.change_sets import ChangeOrigin, capture_file_identity
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def _workspace(tmp_path: Path):
    path = tmp_path / "song.mp3"; path.write_bytes(b"audio")
    item = AudioTrackItem(path=path, folder=tmp_path, ext=".mp3", original=OriginalTags(title="old"),
                          baseline_identity=capture_file_identity(path))
    workspace = TagEditorWorkspaceState(); workspace.set_tracks([item])
    return workspace, item


def test_apply_plan_is_frozen_against_later_proposal(tmp_path: Path):
    workspace, item = _workspace(tmp_path)
    item.proposed.title = "reviewed"; workspace.capture_proposals([item], ChangeOrigin.MANUAL)
    plan = build_apply_plan(workspace, operation_id="op")
    item.proposed.title = "later"; workspace.capture_proposals([item], ChangeOrigin.MANUAL)
    assert plan.validate_current(workspace) == {workspace.item_id(item): "proposal_changed"}


def test_apply_plan_blocks_stale_source_before_worker(tmp_path: Path):
    workspace, item = _workspace(tmp_path)
    item.proposed.title = "reviewed"; workspace.capture_proposals([item], ChangeOrigin.MANUAL)
    item.path.write_bytes(b"replacement")
    with pytest.raises(ApplyPlanBlocked) as error:
        build_apply_plan(workspace, operation_id="op")
    assert error.value.blocked[workspace.item_id(item)] == "changed"


def test_apply_plan_uses_existing_cycle_planner(tmp_path: Path):
    a = tmp_path / "a.mp3"; b = tmp_path / "b.mp3"; a.write_bytes(b"a"); b.write_bytes(b"b")
    workspace = TagEditorWorkspaceState()
    first = AudioTrackItem(a, tmp_path, ".mp3", baseline_identity=capture_file_identity(a))
    second = AudioTrackItem(b, tmp_path, ".mp3", baseline_identity=capture_file_identity(b))
    workspace.set_tracks([first, second])
    first.proposed_filename = "b.mp3"; second.proposed_filename = "a.mp3"
    workspace.capture_proposals([first, second], ChangeOrigin.FILENAME)
    plan = build_apply_plan(workspace, operation_id="swap")
    assert not plan.rename_plan.blocked
    assert len(plan.rename_plan.components[0].steps) == 3
