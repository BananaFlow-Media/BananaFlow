import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.change_sets import ChangeOrigin, FileIdentity, capture_file_identity
from core.file_refresh_service import (
    ConflictResolutionAction, FileRefreshResult, FileRefreshService, RefreshAction,
    WorkspaceRefreshPlan, analyze_stale_file, snapshot_workspace_files,
)
from core.filesystem_monitoring import (
    ExternalChangeState, FilesystemEvent, FilesystemEventBatch,
    FilesystemEventKind, FilesystemEvidence, normalize_filesystem_event,
)
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.incremental_workspace_updater import IncrementalWorkspaceUpdater
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_table_model import MetadataTableModel


def app():
    return QApplication.instance() or QApplication([])


def identity(path, size=10, mtime=20, inode=1):
    return FileIdentity(str(path), size, mtime, 1, inode)


def item(path, *, title="Base", artist="Artist", file_identity=None):
    return AudioTrackItem(
        path, path.parent, ".mp3", original=OriginalTags(title=title, artist=artist),
        format_id="mp3", metadata_editable=True,
        baseline_identity=file_identity or identity(path))


def workspace_with(local_field="artist"):
    app(); path = Path("C:/phase13/song.mp3")
    track = item(path)
    workspace = TagEditorWorkspaceState(); model = MetadataTableModel(workspace=workspace)
    model.load_tracks([track])
    if local_field == "artist":
        track.proposed.artist = "Local Artist"
    else:
        track.proposed.title = "Local Title"
    workspace.capture_proposals([track], ChangeOrigin.MANUAL, label="local")
    return workspace, model, track


def make_conflict(workspace, track, disk, *, state=ExternalChangeState.CHANGED_ON_DISK):
    snapshot = snapshot_workspace_files(workspace)[0]
    return analyze_stale_file(
        snapshot, disk, disk.baseline_identity, observed_path=disk.path,
        state=state, generation=workspace.generation,
        content_revision=workspace.content_revision,
        change_revision=workspace.change_revision, session_id="session")


def apply_conflict(workspace, model, track, conflict):
    updater = IncrementalWorkspaceUpdater(workspace, model)
    result = FileRefreshResult(
        RefreshAction.CONFLICT,
        ExternalChangeState.CONFLICT if conflict.overlap_fields
        else ExternalChangeState.STALE_WITH_PROPOSALS,
        track.path, workspace.item_id(track), refreshed_item=conflict.disk_item,
        conflict=conflict, diagnostic="external_change")
    plan = WorkspaceRefreshPlan(
        "session", workspace.generation, workspace.content_revision,
        workspace.change_revision, (result,))
    assert updater.apply(plan, root=Path("C:/phase13")).accepted
    return updater, track.external_conflict


def test_three_way_comparison_allows_unrelated_rebase_and_detects_overlap():
    workspace, _model, track = workspace_with("artist")
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=identity(track.path, mtime=21))
    safe = make_conflict(workspace, track, disk)
    assert safe.safe_rebase and safe.overlap_fields == ()
    assert {value.field for value in safe.differences} == {"title", "artist"}

    overlap_workspace, _model, overlap_track = workspace_with("title")
    overlap = make_conflict(overlap_workspace, overlap_track, disk)
    assert not overlap.safe_rebase and overlap.overlap_fields == ("title",)


def test_stale_item_preserves_proposals_exclusion_and_blocks_apply():
    workspace, model, track = workspace_with("artist")
    workspace.set_apply_excluded_ids({workspace.item_id(track)}, True)
    disk = item(track.path, title="Disk Title",
                file_identity=identity(track.path, mtime=21))
    _updater, conflict = apply_conflict(workspace, model, track,
                                        make_conflict(workspace, track, disk))
    assert track.proposed.artist == "Local Artist"
    assert track.excluded_from_apply
    assert track.external_conflict == conflict
    assert workspace.apply_candidates() == []
    assert workspace.change_set.records()


def test_safe_rebase_rereads_exact_identity_and_preserves_sources_and_history(monkeypatch):
    workspace, model, track = workspace_with("artist")
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=identity(track.path, mtime=21))
    updater, conflict = apply_conflict(workspace, model, track,
                                       make_conflict(workspace, track, disk))
    evidence = FilesystemEvidence(track.path, True, identity=disk.baseline_identity)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.inspect_filesystem_path",
        lambda _root, _path: evidence)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.FileRefreshService.read_stable_item",
        lambda _evidence: disk)
    summary = updater.resolve_conflict(
        conflict, ConflictResolutionAction.KEEP_LOCAL,
        root=Path("C:/phase13"), session_id="session")
    assert summary.accepted
    assert track.original.title == "Disk Title"
    assert track.proposed.artist == "Local Artist"
    assert track.external_state == "current" and track.external_conflict is None
    assert workspace.can_undo_proposals()
    assert workspace.undo_proposals() and workspace.can_redo_proposals()
    assert workspace.redo_proposals()


def test_unsafe_same_field_rebase_is_refused_without_mutation(monkeypatch):
    workspace, model, track = workspace_with("title")
    disk = item(track.path, title="Disk Title",
                file_identity=identity(track.path, mtime=21))
    updater, conflict = apply_conflict(workspace, model, track,
                                       make_conflict(workspace, track, disk))
    evidence = FilesystemEvidence(track.path, True, identity=disk.baseline_identity)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.inspect_filesystem_path",
        lambda _root, _path: evidence)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.FileRefreshService.read_stable_item",
        lambda _evidence: disk)
    summary = updater.resolve_conflict(
        conflict, ConflictResolutionAction.KEEP_LOCAL,
        root=Path("C:/phase13"), session_id="session")
    assert not summary.accepted and summary.diagnostic == "overlapping_changes"
    assert track.original.title == "Base" and track.proposed.title == "Local Title"
    assert track.external_conflict is conflict


def test_reload_discards_only_chosen_item_proposals(monkeypatch):
    workspace, model, track = workspace_with("artist")
    other = item(Path("C:/phase13/other.mp3"), file_identity=identity("other", inode=2))
    model.add_tracks([other]); other.proposed.title = "Other Local"
    workspace.capture_proposals([other], ChangeOrigin.MANUAL, label="other")
    disk = item(track.path, title="Disk Title",
                file_identity=identity(track.path, mtime=21))
    updater, conflict = apply_conflict(workspace, model, track,
                                       make_conflict(workspace, track, disk))
    evidence = FilesystemEvidence(track.path, True, identity=disk.baseline_identity)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.inspect_filesystem_path",
        lambda _root, _path: evidence)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.FileRefreshService.read_stable_item",
        lambda _evidence: disk)
    summary = updater.resolve_conflict(
        conflict, ConflictResolutionAction.RELOAD,
        root=Path("C:/phase13"), session_id="session")
    assert summary.accepted and track.original.title == "Disk Title"
    assert not track.has_changes
    assert other.proposed.title == "Other Local"


def test_stale_resolution_identity_is_rejected():
    workspace, model, track = workspace_with("artist")
    disk = item(track.path, title="Disk Title",
                file_identity=identity(track.path, mtime=21))
    updater, conflict = apply_conflict(workspace, model, track,
                                       make_conflict(workspace, track, disk))
    workspace.set_external_state(workspace.item_id(track), "conflict",
                                 conflict=conflict, detail="new event")
    summary = updater.resolve_conflict(
        conflict, ConflictResolutionAction.RELOAD,
        root=Path("C:/phase13"), session_id="session")
    assert not summary.accepted and summary.diagnostic == "stale_conflict"


def test_missing_file_requires_explicit_remove():
    workspace, model, track = workspace_with("artist")
    conflict = analyze_stale_file(
        snapshot_workspace_files(workspace)[0], None, None,
        observed_path=track.path, state=ExternalChangeState.MISSING,
        generation=workspace.generation, content_revision=workspace.content_revision,
        change_revision=workspace.change_revision, session_id="session")
    updater, conflict = apply_conflict(workspace, model, track, conflict)
    summary = updater.resolve_conflict(
        conflict, ConflictResolutionAction.REMOVE_MISSING,
        root=Path("C:/phase13"), session_id="session")
    assert summary.accepted and workspace.tracks == []


def batch(root, kind, path, *, prior=None):
    raw = FilesystemEvent("session", 1, 1, kind, path, prior)
    return FilesystemEventBatch(
        "session", 1, (normalize_filesystem_event(root, raw),))


def test_refresh_service_uses_authoritative_disk_state_for_update_and_conflict(tmp_path, monkeypatch):
    root = tmp_path / "music"; root.mkdir(); path = root / "song.mp3"
    path.write_bytes(b"old")
    current = item(path, file_identity=capture_file_identity(path))
    workspace = TagEditorWorkspaceState(); workspace.set_tracks([current])
    path.write_bytes(b"new-content")
    disk = item(path, title="Disk", file_identity=capture_file_identity(path))
    monkeypatch.setattr(FileRefreshService, "read_stable_item",
                        staticmethod(lambda _evidence, _cancellation=None: disk))
    service = FileRefreshService()
    refresh = service.build_plan(
        root=root, batch=batch(root, FilesystemEventKind.MODIFIED, path),
        generation=workspace.generation, content_revision=workspace.content_revision,
        change_revision=workspace.change_revision,
        tracked=snapshot_workspace_files(workspace))
    assert refresh.results[0].action is RefreshAction.UPDATE

    current.proposed.artist = "Local"
    workspace.capture_proposals([current], ChangeOrigin.MANUAL)
    conflict_plan = service.build_plan(
        root=root, batch=batch(root, FilesystemEventKind.MODIFIED, path),
        generation=workspace.generation, content_revision=workspace.content_revision,
        change_revision=workspace.change_revision,
        tracked=snapshot_workspace_files(workspace))
    assert conflict_plan.results[0].action is RefreshAction.CONFLICT
    assert conflict_plan.results[0].conflict is not None


def test_refresh_service_add_remove_relocate_and_replacement(tmp_path, monkeypatch):
    root = tmp_path / "music"; root.mkdir()
    old_path = root / "old.mp3"; old_path.write_bytes(b"same")
    original_identity = capture_file_identity(old_path)
    tracked_item = item(old_path, file_identity=original_identity)
    workspace = TagEditorWorkspaceState(); workspace.set_tracks([tracked_item])
    service = FileRefreshService()

    added_path = root / "added.mp3"; added_path.write_bytes(b"added")
    added_item = item(added_path, file_identity=capture_file_identity(added_path))
    monkeypatch.setattr(FileRefreshService, "read_stable_item",
                        staticmethod(lambda evidence, _cancellation=None:
                                     added_item if evidence.path == added_path else
                                     item(evidence.path, file_identity=evidence.identity)))
    add_plan = service.build_plan(
        root=root, batch=batch(root, FilesystemEventKind.CREATED, added_path),
        generation=workspace.generation, content_revision=workspace.content_revision,
        change_revision=workspace.change_revision,
        tracked=snapshot_workspace_files(workspace))
    assert any(result.action is RefreshAction.ADD for result in add_plan.results)

    missing = root / "missing.mp3"
    missing_item = item(missing, file_identity=identity(missing, inode=99))
    missing_workspace = TagEditorWorkspaceState(); missing_workspace.set_tracks([missing_item])
    remove_plan = service.build_plan(
        root=root, batch=batch(root, FilesystemEventKind.REMOVED, missing),
        generation=missing_workspace.generation,
        content_revision=missing_workspace.content_revision,
        change_revision=missing_workspace.change_revision,
        tracked=snapshot_workspace_files(missing_workspace))
    assert remove_plan.results[0].action is RefreshAction.REMOVE

    new_path = root / "moved.mp3"; old_path.rename(new_path)
    relocate_plan = service.build_plan(
        root=root, batch=batch(root, FilesystemEventKind.RELOCATED, new_path, prior=old_path),
        generation=workspace.generation, content_revision=workspace.content_revision,
        change_revision=workspace.change_revision,
        tracked=snapshot_workspace_files(workspace))
    assert any(result.action is RefreshAction.RELOCATE for result in relocate_plan.results)

    replacement_workspace = TagEditorWorkspaceState()
    replacement = item(added_path, file_identity=identity(added_path, inode=999))
    replacement.proposed.title = "Local"
    replacement_workspace.set_tracks([replacement])
    replacement_workspace.capture_proposals([replacement], ChangeOrigin.MANUAL)
    replace_plan = service.build_plan(
        root=root, batch=batch(root, FilesystemEventKind.MODIFIED, added_path),
        generation=replacement_workspace.generation,
        content_revision=replacement_workspace.content_revision,
        change_revision=replacement_workspace.change_revision,
        tracked=snapshot_workspace_files(replacement_workspace))
    assert replace_plan.results[0].state is ExternalChangeState.CONFLICT
    assert replace_plan.results[0].conflict.diagnostic == "file_replaced"
