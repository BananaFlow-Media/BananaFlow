from __future__ import annotations

from pathlib import Path

from core.change_sets import ChangeOrigin
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def _workspace(tmp_path: Path):
    tracks = [AudioTrackItem(path=tmp_path / f"{name}.mp3", folder=tmp_path, ext=".mp3",
                             original=OriginalTags(title=name, artist="artist"))
              for name in ("one", "two")]
    workspace = TagEditorWorkspaceState(); workspace.set_tracks(tracks)
    return workspace, tracks


def test_review_exclusion_uses_stable_ids_and_is_undoable(tmp_path: Path):
    workspace, tracks = _workspace(tmp_path)
    tracks[0].proposed.title = "new one"; tracks[1].proposed.artist = "new artist"
    workspace.capture_proposals(tracks, ChangeOrigin.MANUAL)
    second_id = workspace.item_id(tracks[1])
    workspace.set_apply_excluded_ids([second_id], True)
    assert workspace.change_set.summary().excluded_files == 1
    assert workspace.undo_proposals()
    assert workspace.change_set.summary().excluded_files == 0


def test_review_field_revert_preserves_other_fields_and_undoes(tmp_path: Path):
    workspace, tracks = _workspace(tmp_path)
    tracks[0].proposed.title = "new title"; tracks[0].proposed.artist = "new artist"
    workspace.capture_proposals(tracks, ChangeOrigin.MANUAL)
    identity = workspace.item_id(tracks[0])
    workspace.revert_record_targets({identity: {"title"}})
    assert tracks[0].proposed.title is None and tracks[0].proposed.artist == "new artist"
    assert workspace.undo_proposals()
    assert tracks[0].proposed.title == "new title"
