import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.change_sets import ChangeOrigin, FileIdentity
from core.file_refresh_service import (
    FileRefreshResult, RefreshAction, WorkspaceRefreshPlan,
)
from core.filesystem_monitoring import ExternalChangeState
from core.metadata_csv import build_metadata_export_plan
from core.metadata_io import IOScope
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.incremental_workspace_updater import IncrementalWorkspaceUpdater
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_filter_proxy_model import MetadataFilterProxyModel
from ui.models.metadata_table_model import COL_FILENAME, MetadataTableModel


def app():
    return QApplication.instance() or QApplication([])


def track(path: Path, title="Title", inode=1):
    return AudioTrackItem(
        path, path.parent, path.suffix, original=OriginalTags(title=title),
        format_id="mp3", metadata_editable=True,
        baseline_identity=FileIdentity(str(path), 10, 20, 1, inode))


def plan(workspace, *results):
    return WorkspaceRefreshPlan(
        "session", workspace.generation, workspace.content_revision,
        workspace.change_revision, tuple(results))


def setup_model(items):
    app(); workspace = TagEditorWorkspaceState(); model = MetadataTableModel(workspace=workspace)
    model.load_tracks(list(items))
    return workspace, model, IncrementalWorkspaceUpdater(workspace, model)


def test_refresh_one_item_preserves_id_selection_and_avoids_reset(tmp_path):
    original = track(tmp_path / "a.mp3", "Old")
    other = track(tmp_path / "b.mp3", "Other", 2)
    workspace, model, updater = setup_model([original, other])
    identity = workspace.item_id(original); other_id = workspace.item_id(other)
    workspace.set_selected_items([other])
    refreshed = track(original.path, "New")
    resets = model.operation_counters.resets
    result = FileRefreshResult(
        RefreshAction.UPDATE, ExternalChangeState.CHANGED_ON_DISK,
        original.path, identity, refreshed_item=refreshed)
    summary = updater.apply(plan(workspace, result), root=tmp_path)
    assert summary.accepted and summary.refreshed == 1
    assert workspace.track_for_id(identity) is original
    assert original.original.title == "New"
    assert workspace.item_id(other) == other_id
    assert workspace.selected_tracks() == [other]
    assert model.operation_counters.resets == resets
    assert model.operation_counters.refreshed_rows >= 1


def test_add_remove_relocate_are_incremental_and_ids_never_reused(tmp_path):
    first = track(tmp_path / "a.mp3")
    workspace, model, updater = setup_model([first])
    first_id = workspace.item_id(first); generation = workspace.generation
    added = track(tmp_path / "b.mp3", inode=2)
    add = FileRefreshResult(RefreshAction.ADD, ExternalChangeState.CHANGED_ON_DISK,
                            added.path, refreshed_item=added)
    assert updater.apply(plan(workspace, add), root=tmp_path).accepted
    added_id = workspace.item_id(added)
    remove = FileRefreshResult(RefreshAction.REMOVE, ExternalChangeState.MISSING,
                               first.path, first_id)
    assert updater.apply(plan(workspace, remove), root=tmp_path).accepted
    assert workspace.generation == generation and workspace.track_for_id(first_id) is None
    third = track(tmp_path / "c.mp3", inode=3)
    assert updater.apply(plan(workspace, FileRefreshResult(
        RefreshAction.ADD, ExternalChangeState.CHANGED_ON_DISK,
        third.path, refreshed_item=third)), root=tmp_path).accepted
    assert workspace.item_id(third) > added_id > first_id
    prior = added.path; destination = tmp_path / "renamed.mp3"
    relocate = FileRefreshResult(
        RefreshAction.RELOCATE, ExternalChangeState.MOVED, destination,
        added_id, prior_path=prior, refreshed_item=track(destination, inode=2))
    assert updater.apply(plan(workspace, relocate), root=tmp_path).accepted
    assert workspace.track_for_id(added_id) is added and added.path == destination
    assert model.operation_counters.resets == 1
    assert model.operation_counters.inserted_rows == 2
    assert model.operation_counters.removed_rows == 1


def test_removal_retires_change_set_exclusion_selection_visibility_and_history(tmp_path):
    first, second = track(tmp_path / "a.mp3"), track(tmp_path / "b.mp3", inode=2)
    workspace, model, updater = setup_model([first, second])
    first.proposed.title = "Pending"
    workspace.capture_proposals([first], ChangeOrigin.MANUAL, label="title")
    workspace.set_apply_excluded_ids({workspace.item_id(first)}, True)
    workspace.set_selected_items([first]); workspace.set_visible_items([first])
    removed_id = workspace.item_id(first); generation = workspace.generation
    result = FileRefreshResult(RefreshAction.REMOVE, ExternalChangeState.MISSING,
                               first.path, removed_id)
    assert updater.apply(plan(workspace, result), root=tmp_path).accepted
    assert workspace.generation == generation
    assert not workspace.change_set.records(item_ids={removed_id})
    assert not workspace.selected_tracks()
    assert removed_id not in {workspace.item_id(item) for item in workspace.visible_tracks()}
    assert not workspace.can_undo_proposals()


def test_sorted_insert_and_refresh_do_not_reset_model(tmp_path):
    first = track(tmp_path / "z.mp3")
    workspace, model, updater = setup_model([first])
    model.sort(COL_FILENAME, Qt.AscendingOrder)
    resets = model.operation_counters.resets
    added = track(tmp_path / "a.mp3", inode=2)
    assert updater.apply(plan(workspace, FileRefreshResult(
        RefreshAction.ADD, ExternalChangeState.CHANGED_ON_DISK,
        added.path, refreshed_item=added)), root=tmp_path).accepted
    assert model.track_at_row(0) is added
    assert model.operation_counters.resets == resets


def test_stale_proxy_filter_and_selection_do_not_cause_disk_io_or_refilter(tmp_path, monkeypatch):
    first, second = track(tmp_path / "a.mp3"), track(tmp_path / "b.mp3", inode=2)
    workspace, model, updater = setup_model([first, second])
    proxy = MetadataFilterProxyModel(workspace); proxy.setSourceModel(model)
    before = proxy.invalidation_count
    workspace.set_selected_items([first])
    assert proxy.invalidation_count == before
    workspace.set_external_state(workspace.item_id(second), "conflict")
    proxy.set_show_stale_only(True)
    assert proxy.rowCount() == 1 and proxy.track_at_row(0) is second


def test_content_revision_invalidates_csv_preview_without_root_generation_change(tmp_path):
    first = track(tmp_path / "a.mp3")
    workspace, model, updater = setup_model([first])
    identity = workspace.item_id(first); generation = workspace.generation
    export = build_metadata_export_plan(
        workspace, root=tmp_path, item_ids=(identity,), scope=IOScope.ALL_LOADED)
    workspace.set_external_state(identity, "unreadable", detail="network")
    assert workspace.generation == generation
    assert not export.identity.current_for(workspace)


def test_stale_plan_is_rejected_before_any_partial_mutation(tmp_path):
    first = track(tmp_path / "a.mp3")
    workspace, model, updater = setup_model([first])
    stale = plan(workspace, FileRefreshResult(
        RefreshAction.REMOVE, ExternalChangeState.MISSING,
        first.path, workspace.item_id(first)))
    workspace.set_external_state(workspace.item_id(first), "refreshing")
    summary = updater.apply(stale, root=tmp_path)
    assert not summary.accepted and summary.diagnostic == "stale_content_revision"
    assert workspace.track_for_id(workspace.item_id(first)) is first
