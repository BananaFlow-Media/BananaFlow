"""Focused Phase 8 change-set, history, and stale-identity contracts."""

from pathlib import Path

from core.change_sets import (
    ChangeCommand, ChangeOperation, ChangeOrigin, ChangeSet, FileIdentity,
    ProposalHistory, ApplyReviewPolicy, capture_file_identity, file_identity_status,
)
from core.metadata_models import metadata_values_equal
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def _equal(field, left, right):
    return metadata_values_equal(field, left, right)


def test_change_set_drops_semantic_noops_and_counts_active_records():
    changes = ChangeSet()
    assert changes.record(7, "artist", ("A", "B"), "A; B", operation=ChangeOperation.SET,
                          origin=ChangeOrigin.MANUAL, equal=_equal) is None
    changes.record(7, "title", "old", "new", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal)
    changes.record(8, "filename", "a.mp3", "b.mp3", operation=ChangeOperation.RENAME,
                   origin=ChangeOrigin.FILENAME, equal=_equal)
    changes.set_excluded({8}, True)
    summary = changes.summary()
    assert (summary.changed_files, summary.changed_fields, summary.filename_changes) == (2, 2, 1)
    assert (summary.included_files, summary.excluded_files) == (1, 1)


def test_later_proposal_replaces_active_origin_without_duplicate_record():
    changes = ChangeSet()
    changes.record(1, "title", "before", "auto", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.AUTO_ARRANGE, equal=_equal)
    changes.record(1, "title", "before", "manual", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal)
    record = changes.records()[0]
    assert record.origin is ChangeOrigin.MANUAL
    assert record.previous_value == "auto"
    assert changes.summary().changed_fields == 1


def test_apply_review_policy_keeps_small_single_metadata_edit_compact():
    changes = ChangeSet()
    changes.record(1, "title", "old", "new", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal)
    assert not ApplyReviewPolicy.requires_full_review(changes)
    changes.record(2, "filename", "a.mp3", "b.mp3", operation=ChangeOperation.RENAME,
                   origin=ChangeOrigin.FILENAME, equal=_equal)
    assert ApplyReviewPolicy.requires_full_review(changes)


def test_history_is_generation_bound_and_drops_unsafe_redo():
    changes = ChangeSet()
    history = ProposalHistory()
    before = changes.snapshot(3)
    changes.record(1, "title", "old", "new", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal)
    after = changes.snapshot(3)
    history.push(ChangeCommand(3, before, after, "title"))
    assert history.undo(changes, 3) is not None
    assert not changes.records()
    assert history.redo(changes, 4) is None
    assert not history.can_redo(4)


def test_identity_detects_replacement_and_missing_file(tmp_path: Path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"one")
    expected = capture_file_identity(path)
    assert file_identity_status(expected, path) == "current"
    path.write_bytes(b"different")
    assert file_identity_status(expected, path) == "changed"
    path.unlink()
    assert file_identity_status(expected, path) == "missing"


def test_workspace_undo_redo_projects_records_back_to_canonical_proposals(tmp_path: Path):
    item = AudioTrackItem(path=tmp_path / "track.mp3", folder=tmp_path, ext=".mp3",
                          original=OriginalTags(title="stored"))
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([item])
    item.proposed.title = "proposed"
    workspace.capture_proposals([item], ChangeOrigin.MANUAL, label="title")
    assert workspace.change_set.records()[0].proposed_value == "proposed"
    assert workspace.undo_proposals() and item.proposed.title is None
    assert workspace.redo_proposals() and item.proposed.title == "proposed"
