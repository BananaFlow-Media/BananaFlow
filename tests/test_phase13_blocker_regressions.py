"""Regressions for the seven proven Phase 13 review blockers.

Each group pins one corrected production behaviour.  They deliberately drive
the real workspace/controller/service objects rather than helpers, because the
original defects were all cases where a correct helper existed but production
either never called it or called it with the wrong scope.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.change_sets import ChangeOrigin, FileIdentity, capture_file_identity
from core.file_refresh_service import (
    ConflictResolutionAction, FileRefreshService, RefreshAction,
    analyze_stale_file, snapshot_workspace_files,
)
from core.filesystem_monitoring import (
    APPLY_BLOCKING_EXTERNAL_STATES, ExternalChangeState, FilesystemEvent,
    FilesystemEventBatch, FilesystemEventKind, FilesystemEvidence,
    SETTLED_EXTERNAL_STATES, external_state_blocks_apply, is_external_change,
    normalize_filesystem_event,
)
from core.metadata_models import AudioTrackItem, OriginalTags
from core.metadata_processor import build_scan_result
from ui.controllers.incremental_workspace_updater import IncrementalWorkspaceUpdater
from ui.controllers.metadata_controller import MetadataController
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_filter_proxy_model import MetadataFilterProxyModel
from ui.models.metadata_table_model import MetadataTableModel


def app():
    return QApplication.instance() or QApplication([])


def identity(path, size=10, mtime=20, inode=1):
    return FileIdentity(str(path), size, mtime, 1, inode)


def item(path, *, title="Base", artist="Artist", file_identity=None):
    path = Path(path)
    return AudioTrackItem(
        path, path.parent, ".mp3", original=OriginalTags(title=title, artist=artist),
        format_id="mp3", metadata_editable=True,
        baseline_identity=file_identity or identity(path))


def batch(root, kind, path, *, prior=None, sequence=1):
    raw = FilesystemEvent("session", 1, sequence, kind, Path(path), prior)
    return FilesystemEventBatch(
        "session", 1, (normalize_filesystem_event(root, raw),))


def build(service, root, event_batch, workspace, *, recursive=True):
    return service.build_plan(
        root=root, batch=event_batch, generation=workspace.generation,
        content_revision=workspace.content_revision,
        change_revision=workspace.change_revision,
        tracked=snapshot_workspace_files(workspace), recursive=recursive)


# ── Blocker 1: a temporarily lost root must never destroy the workspace ─────


def root_loss_workspace(tmp_path):
    app()
    root = tmp_path / "music"
    root.mkdir()
    clean, proposed = root / "clean.mp3", root / "proposed.mp3"
    clean.write_bytes(b"clean-audio")
    proposed.write_bytes(b"proposed-audio")
    clean_item = item(clean, file_identity=capture_file_identity(clean))
    proposed_item = item(proposed, file_identity=capture_file_identity(proposed))
    workspace = TagEditorWorkspaceState()
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks([clean_item, proposed_item])
    proposed_item.proposed.artist = "Local Artist"
    workspace.capture_proposals([proposed_item], ChangeOrigin.MANUAL, label="local")
    return root, workspace, model, clean_item, proposed_item


def lose_root(root, workspace, model):
    """Plan and apply one real ROOT_LOST batch after the root disappears.

    The root is renamed aside rather than deleted: a share that drops or a USB
    volume that is unplugged makes the path unreachable while every file keeps
    its identity, which is exactly the case the old code destroyed.
    """
    updater = IncrementalWorkspaceUpdater(workspace, model)
    root.rename(offline_root(root))
    plan = build(FileRefreshService(), root,
                 batch(root, FilesystemEventKind.ROOT_LOST, root), workspace)
    return updater, plan


def offline_root(root):
    return root.parent / f"{root.name}__offline"


def test_root_loss_never_removes_clean_files_or_discards_proposals(tmp_path):
    root, workspace, model, clean_item, proposed_item = root_loss_workspace(tmp_path)
    clean_id = workspace.item_id(clean_item)
    proposed_id = workspace.item_id(proposed_item)
    updater, plan = lose_root(root, workspace, model)

    assert plan.root_lost
    assert {result.action for result in plan.results} == {RefreshAction.MARK_STATE}
    assert not any(result.action is RefreshAction.REMOVE for result in plan.results)
    assert updater.apply(plan, root=root).accepted

    assert workspace.track_for_id(clean_id) is clean_item
    assert workspace.track_for_id(proposed_id) is proposed_item
    assert clean_item.external_state == ExternalChangeState.ROOT_UNAVAILABLE.value
    assert proposed_item.proposed.artist == "Local Artist"
    assert workspace.change_set.records(item_ids={proposed_id})
    assert workspace.proposal_history.undo_depth == 1


def test_root_loss_preserves_ids_selection_visibility_and_exclusions(tmp_path):
    root, workspace, model, clean_item, proposed_item = root_loss_workspace(tmp_path)
    workspace.set_apply_excluded_ids({workspace.item_id(proposed_item)}, True)
    workspace.set_selected_items([clean_item])
    workspace.set_visible_items([clean_item, proposed_item])
    ids = {workspace.item_id(clean_item), workspace.item_id(proposed_item)}
    generation = workspace.generation

    updater, plan = lose_root(root, workspace, model)
    assert updater.apply(plan, root=root).accepted

    assert workspace.generation == generation
    assert {workspace.item_id(track) for track in workspace.tracks} == ids
    assert workspace.selected_tracks() == [clean_item]
    assert len(workspace.visible_tracks()) == 2
    assert proposed_item.excluded_from_apply


def test_root_loss_blocks_apply_only_while_unavailable(tmp_path):
    root, workspace, model, _clean, proposed_item = root_loss_workspace(tmp_path)
    assert workspace.apply_candidates() == [proposed_item]
    updater, plan = lose_root(root, workspace, model)
    assert updater.apply(plan, root=root).accepted
    # Nothing may be written to a folder that is not there, but the proposal
    # stays pending and the item stays a visible blocker rather than vanishing.
    assert workspace.apply_candidates() == []
    assert workspace.apply_blockers() == [proposed_item]
    assert external_state_blocks_apply(ExternalChangeState.ROOT_UNAVAILABLE)


def test_root_restore_reconciles_unchanged_removed_and_replaced_files(tmp_path):
    root, workspace, model, clean_item, proposed_item = root_loss_workspace(tmp_path)
    survivor = root / "survivor.mp3"
    survivor.write_bytes(b"survivor")
    survivor_item = item(survivor, file_identity=capture_file_identity(survivor))
    model.add_tracks([survivor_item])
    survivor_id = workspace.item_id(survivor_item)
    clean_id = workspace.item_id(clean_item)
    proposed_id = workspace.item_id(proposed_item)
    clean_bytes = clean_item.path.read_bytes()
    proposed_path = proposed_item.path

    updater, plan = lose_root(root, workspace, model)
    assert updater.apply(plan, root=root).accepted

    # While the volume was away: one file was genuinely deleted and a different
    # file took over the proposed item's path.  The survivor is untouched.
    offline = offline_root(root)
    (offline / clean_item.path.name).unlink()
    (offline / proposed_path.name).unlink()
    (offline / proposed_path.name).write_bytes(b"a completely different file")
    offline.rename(root)
    restored = build(FileRefreshService(), root,
                     batch(root, FilesystemEventKind.ROOT_RESTORED, root), workspace)

    by_id = {result.item_id: result for result in restored.results}
    assert not restored.root_lost
    # Unchanged survivor: same stable ID, back to current, nothing removed.
    assert by_id[survivor_id].action is RefreshAction.MARK_STATE
    assert by_id[survivor_id].state is ExternalChangeState.CURRENT
    # Genuinely removed while unavailable — only now is removal legitimate.
    assert by_id[clean_id].action is RefreshAction.REMOVE
    # Replacement at the same path keeps proposals and becomes a conflict.
    assert by_id[proposed_id].action is RefreshAction.CONFLICT
    assert by_id[proposed_id].conflict is not None
    assert clean_bytes and proposed_item.proposed.artist == "Local Artist"


def test_controller_keeps_the_folder_tree_when_the_root_goes_offline(tmp_path):
    """Root loss must not rebuild the watch set or folder tree from nothing."""
    app()
    root, workspace, model, clean_item, proposed_item = root_loss_workspace(tmp_path)
    control = MetadataController()
    try:
        control.workspace_state.set_tracks([clean_item, proposed_item])
        control._session.scan_result = build_scan_result(
            root, [clean_item, proposed_item], 0, {root})
        control._on_scan_complete(control._session.scan_result)
        folders_before = set(control._session.scan_result.folder_set)
        control.set_incremental_updater(
            IncrementalWorkspaceUpdater(control.workspace_state, model))

        updater, _plan = lose_root(root, control.workspace_state, model)
        plan = build(FileRefreshService(), root,
                     batch(root, FilesystemEventKind.ROOT_LOST, root),
                     control.workspace_state)
        plan = type(plan)(**{**plan.__dict__,
                             "session_id": control.watch_session.session_id,
                             "generation": control.watch_session.generation})
        control._current_refresh_worker = None
        control._on_filesystem_plan(plan)

        assert set(control._session.scan_result.folder_set) == folders_before
        assert len(control.workspace_state.tracks) == 2
    finally:
        control._stop_watch_session()
        control.deleteLater()


# ── Blocker 4: a safe rebase is one coherent, reversible transition ─────────


def rebase_setup(local_field="artist"):
    app()
    path = Path("C:/phase13/song.mp3")
    track = item(path)
    workspace = TagEditorWorkspaceState()
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks([track])
    setattr(track.proposed, local_field, "Local Value")
    workspace.capture_proposals(
        [track], ChangeOrigin.ONLINE_METADATA, label="local",
        source={"provider": "musicbrainz", "attribution": "MusicBrainz",
                "url": "https://musicbrainz.org/x"})
    return workspace, model, track


def resolve_keep_local(monkeypatch, workspace, model, track, disk, *,
                       observed=None, state=ExternalChangeState.CHANGED_ON_DISK):
    updater = IncrementalWorkspaceUpdater(workspace, model)
    snapshot = snapshot_workspace_files(workspace)[0]
    observed = observed or disk.path
    conflict = analyze_stale_file(
        snapshot, disk, disk.baseline_identity, observed_path=observed,
        state=state, generation=workspace.generation,
        content_revision=workspace.content_revision,
        change_revision=workspace.change_revision, session_id="session")
    workspace.set_external_state(workspace.item_id(track), state.value,
                                 conflict=conflict)
    conflict = track.external_conflict = type(conflict)(**{
        **conflict.__dict__, "content_revision": workspace.content_revision,
        "change_revision": workspace.change_revision})
    evidence = FilesystemEvidence(observed, True, identity=disk.baseline_identity)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.inspect_filesystem_path",
        lambda _root, _path: evidence)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.FileRefreshService.read_stable_item",
        lambda _evidence: disk)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.strong_same_file",
        lambda _left, _right: True)
    return updater, updater.resolve_conflict(
        conflict, ConflictResolutionAction.KEEP_LOCAL,
        root=Path("C:/phase13"), session_id="session")


def test_safe_rebase_creates_exactly_one_new_undo_transition(monkeypatch):
    workspace, model, track = rebase_setup()
    before_depth = workspace.proposal_history.undo_depth
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=identity(track.path, mtime=21))
    _updater, summary = resolve_keep_local(monkeypatch, workspace, model, track, disk)
    assert summary.accepted
    # The old test only proved the *local edit* was undoable.  The rebase itself
    # must add its own transition even though the proposal did not move.
    assert workspace.proposal_history.undo_depth == before_depth + 1


def test_undo_and_redo_restore_the_prior_and_refreshed_baseline(monkeypatch):
    workspace, model, track = rebase_setup()
    old_identity = track.baseline_identity
    new_identity = identity(track.path, mtime=21)
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=new_identity)
    _updater, summary = resolve_keep_local(monkeypatch, workspace, model, track, disk)
    assert summary.accepted
    assert track.original.title == "Disk Title"
    assert track.baseline_identity == new_identity

    assert workspace.undo_proposals()
    assert track.original.title == "Base"
    assert track.baseline_identity == old_identity
    assert track.proposed.artist == "Local Value"

    assert workspace.redo_proposals()
    assert track.original.title == "Disk Title"
    assert track.baseline_identity == new_identity
    assert track.proposed.artist == "Local Value"


def test_rebase_undo_preserves_proposal_source_attribution_and_exclusion(monkeypatch):
    workspace, model, track = rebase_setup()
    workspace.set_apply_excluded_ids({workspace.item_id(track)}, True)
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=identity(track.path, mtime=21))
    _updater, summary = resolve_keep_local(monkeypatch, workspace, model, track, disk)
    assert summary.accepted

    def artist_record():
        return next(record for record in workspace.change_set.records()
                    if record.field == "artist")

    assert artist_record().source_provider == "musicbrainz"
    assert artist_record().origin is ChangeOrigin.ONLINE_METADATA
    assert track.excluded_from_apply
    assert workspace.undo_proposals()
    assert artist_record().source_provider == "musicbrainz"
    assert artist_record().source_url == "https://musicbrainz.org/x"
    assert track.excluded_from_apply
    assert workspace.redo_proposals()
    assert artist_record().source_attribution == "MusicBrainz"
    assert track.excluded_from_apply


def test_relocation_plus_safe_rebase_is_one_reversible_transition(monkeypatch):
    workspace, model, track = rebase_setup()
    before_depth = workspace.proposal_history.undo_depth
    original_path = track.path
    moved = original_path.parent / "moved.mp3"
    disk = item(moved, title="Disk Title", artist="Artist",
                file_identity=identity(moved, mtime=21))
    _updater, summary = resolve_keep_local(
        monkeypatch, workspace, model, track, disk,
        observed=moved, state=ExternalChangeState.MOVED)
    assert summary.accepted
    assert track.path == moved and track.original.title == "Disk Title"
    assert workspace.proposal_history.undo_depth == before_depth + 1

    assert workspace.undo_proposals()
    assert track.path == original_path
    assert track.original.title == "Base"
    assert workspace.track_for_path(original_path) is track

    assert workspace.redo_proposals()
    assert track.path == moved
    assert workspace.track_for_path(moved) is track


def test_unsafe_overlap_creates_no_history_or_baseline_mutation(monkeypatch):
    workspace, model, track = rebase_setup("title")
    before_depth = workspace.proposal_history.undo_depth
    disk = item(track.path, title="Disk Title",
                file_identity=identity(track.path, mtime=21))
    _updater, summary = resolve_keep_local(monkeypatch, workspace, model, track, disk)
    assert not summary.accepted and summary.diagnostic == "overlapping_changes"
    assert workspace.proposal_history.undo_depth == before_depth
    assert track.original.title == "Base"
    assert track.proposed.title == "Local Value"


def test_reload_remains_a_deliberate_proposal_discard_boundary(monkeypatch):
    workspace, model, track = rebase_setup()
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=identity(track.path, mtime=21))
    updater = IncrementalWorkspaceUpdater(workspace, model)
    snapshot = snapshot_workspace_files(workspace)[0]
    conflict = analyze_stale_file(
        snapshot, disk, disk.baseline_identity, observed_path=track.path,
        state=ExternalChangeState.CHANGED_ON_DISK, generation=workspace.generation,
        content_revision=workspace.content_revision,
        change_revision=workspace.change_revision, session_id="session")
    workspace.set_external_state(workspace.item_id(track), "changed_on_disk",
                                 conflict=conflict)
    conflict = track.external_conflict = type(conflict)(**{
        **conflict.__dict__, "content_revision": workspace.content_revision,
        "change_revision": workspace.change_revision})
    evidence = FilesystemEvidence(track.path, True, identity=disk.baseline_identity)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.inspect_filesystem_path",
        lambda _root, _path: evidence)
    monkeypatch.setattr(
        "ui.controllers.incremental_workspace_updater.FileRefreshService.read_stable_item",
        lambda _evidence: disk)
    assert updater.resolve_conflict(
        conflict, ConflictResolutionAction.RELOAD,
        root=Path("C:/phase13"), session_id="session").accepted
    assert not track.has_changes
    assert not workspace.change_set.records()
    # Reload explicitly discarded this item's proposals, so no history command
    # may bring them back.
    assert not workspace.can_undo_proposals()


def test_removed_item_cannot_be_resurrected_by_a_rebase_command(monkeypatch):
    workspace, model, track = rebase_setup()
    disk = item(track.path, title="Disk Title", artist="Artist",
                file_identity=identity(track.path, mtime=21))
    _updater, summary = resolve_keep_local(monkeypatch, workspace, model, track, disk)
    assert summary.accepted
    identity_value = workspace.item_id(track)
    workspace.remove_item_ids({identity_value})
    assert workspace.track_for_id(identity_value) is None
    assert workspace.proposal_history.undo_depth == 0
    assert not workspace.undo_proposals()
    assert workspace.tracks == []


# ── Blocker 5: an excluded stale item must not block safe included work ─────


def apply_scope_workspace(tmp_path):
    app()
    safe_path, stale_path = tmp_path / "safe.mp3", tmp_path / "stale.mp3"
    safe_path.write_bytes(b"safe")
    stale_path.write_bytes(b"stale")
    safe = item(safe_path, file_identity=capture_file_identity(safe_path))
    stale = item(stale_path, file_identity=capture_file_identity(stale_path))
    control = MetadataController()
    control._session.scan_result = build_scan_result(tmp_path, [safe, stale], 0, {tmp_path})
    control.workspace_state.set_tracks([safe, stale])
    safe.proposed.title = "Safe Pending"
    stale.proposed.title = "Stale Pending"
    control.workspace_state.capture_proposals(
        [safe, stale], ChangeOrigin.MANUAL, label="edits")
    control.workspace_state.set_external_state(
        control.workspace_state.item_id(stale), ExternalChangeState.CONFLICT.value,
        detail="external_change")
    return control, safe, stale


def test_excluded_stale_item_does_not_block_a_safe_included_candidate(tmp_path):
    control, safe, stale = apply_scope_workspace(tmp_path)
    workspace = control.workspace_state
    workspace.set_apply_excluded_ids({workspace.item_id(stale)}, True)
    started = []
    control.apply_started.connect(lambda: started.append(True))
    launched = {}
    control.apply_changes(backup_dir=tmp_path / "backups")

    assert workspace.apply_blockers() == []
    assert workspace.apply_candidates() == [safe]
    assert started, "Apply must run for the safe included file"
    worker = control._apply_worker
    assert worker is not None
    launched = list(getattr(worker, "_items", getattr(worker, "items", [])) or [])
    control.cancel_apply()
    worker.wait(5000)
    # The excluded stale proposal is untouched, pending and still reviewable.
    assert stale.proposed.title == "Stale Pending"
    assert stale.excluded_from_apply
    assert workspace.change_set.records(item_ids={workspace.item_id(stale)})
    if launched:
        assert stale not in launched


def test_included_stale_item_blocks_and_unexcluding_restores_the_block(tmp_path):
    control, safe, stale = apply_scope_workspace(tmp_path)
    workspace = control.workspace_state
    # Included and stale: it must block itself rather than be written.
    assert workspace.apply_blockers() == [stale]
    control.apply_changes(backup_dir=tmp_path / "backups")
    assert control._apply_worker is None

    workspace.set_apply_excluded_ids({workspace.item_id(stale)}, True)
    assert workspace.apply_blockers() == []
    workspace.set_apply_excluded_ids({workspace.item_id(stale)}, False)
    assert workspace.apply_blockers() == [stale]


def test_resolving_the_stale_item_makes_it_eligible_again(tmp_path):
    control, safe, stale = apply_scope_workspace(tmp_path)
    workspace = control.workspace_state
    workspace.set_external_state(workspace.item_id(stale),
                                 ExternalChangeState.CURRENT.value)
    assert workspace.apply_blockers() == []
    assert workspace.apply_candidates() == [safe, stale]


def test_review_blockers_and_apply_scope_agree_on_excluded_items(tmp_path):
    control, safe, stale = apply_scope_workspace(tmp_path)
    workspace = control.workspace_state
    workspace.set_apply_excluded_ids({workspace.item_id(stale)}, True)
    from ui.panels.metadata_editor.panel import MetadataEditorPanel
    panel = MetadataEditorPanel()
    try:
        panel.set_workspace_state(workspace)
        blocked = panel._review_blocked_records(workspace.change_set.records())
        # Review counts exactly what Apply will refuse: nothing.
        assert blocked == {}
        assert len(workspace.apply_candidates()) == len(workspace.change_set.records(
            item_ids={workspace.item_id(safe)}))
    finally:
        panel.close()
        panel.deleteLater()
        QApplication.processEvents()


# ── Blocker 6: the external count and filter describe one set ───────────────


def test_every_external_state_is_counted_exactly_when_it_is_filtered():
    app()
    workspace = TagEditorWorkspaceState()
    model = MetadataTableModel(workspace=workspace)
    states = list(ExternalChangeState)
    tracks = [item(Path(f"C:/phase13/{state.value}.mp3"),
                   file_identity=identity(f"C:/phase13/{state.value}.mp3", inode=index))
              for index, state in enumerate(states, start=1)]
    model.load_tracks(tracks)
    proxy = MetadataFilterProxyModel(workspace)
    proxy.setSourceModel(model)
    for track, state in zip(tracks, states):
        workspace.set_external_state(workspace.item_id(track), state.value)

    expected = {state.value for state in states} - SETTLED_EXTERNAL_STATES
    counted = {track.external_state for track in tracks
               if is_external_change(track.external_state)}
    assert counted == expected

    proxy.set_show_stale_only(True)
    shown = {proxy.track_at_row(row).external_state for row in range(proxy.rowCount())}
    # The number on the chip and the rows behind it must be the same set.
    assert shown == expected
    assert proxy.rowCount() == len(counted)
    assert ExternalChangeState.CHANGED_ON_DISK.value in shown
    assert ExternalChangeState.CURRENT.value not in shown
    assert ExternalChangeState.IGNORED_OWN_OPERATION.value not in shown


def test_controller_count_matches_the_filtered_row_count(tmp_path):
    app()
    control = MetadataController()
    tracks = [item(tmp_path / "a.mp3", file_identity=identity(tmp_path / "a.mp3", inode=1)),
              item(tmp_path / "b.mp3", file_identity=identity(tmp_path / "b.mp3", inode=2)),
              item(tmp_path / "c.mp3", file_identity=identity(tmp_path / "c.mp3", inode=3))]
    workspace = control.workspace_state
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks(tracks)
    proxy = MetadataFilterProxyModel(workspace)
    proxy.setSourceModel(model)
    workspace.set_external_state(workspace.item_id(tracks[0]),
                                 ExternalChangeState.CHANGED_ON_DISK.value)
    workspace.set_external_state(workspace.item_id(tracks[1]),
                                 ExternalChangeState.CONFLICT.value)
    proxy.set_show_stale_only(True)
    assert control._external_change_count() == 2
    assert proxy.rowCount() == 2


def test_apply_blocking_and_external_change_sets_are_distinct_and_explicit():
    # changed_on_disk needs attention but does not by itself make a write unsafe.
    assert is_external_change(ExternalChangeState.CHANGED_ON_DISK)
    assert not external_state_blocks_apply(ExternalChangeState.CHANGED_ON_DISK)
    assert not is_external_change(ExternalChangeState.CURRENT)
    assert not is_external_change(ExternalChangeState.IGNORED_OWN_OPERATION)
    assert APPLY_BLOCKING_EXTERNAL_STATES <= {
        state.value for state in ExternalChangeState} - SETTLED_EXTERNAL_STATES
    assert is_external_change("changed_on_disk") and not is_external_change("current")


# ── Blocker 7: the original scan scope survives the whole session ───────────


def non_recursive_tree(tmp_path):
    root = tmp_path / "music"
    child = root / "child"
    child.mkdir(parents=True)
    direct = root / "direct.mp3"
    nested = child / "nested.mp3"
    direct.write_bytes(b"direct")
    nested.write_bytes(b"nested")
    return root, child, direct, nested


def test_scan_result_carries_the_recursive_flag_into_the_watch_session(tmp_path):
    app()
    root, _child, _direct, _nested = non_recursive_tree(tmp_path)
    control = MetadataController()
    try:
        result = build_scan_result(root, [], 0, {root}, recursive=False)
        assert result.recursive is False
        control._on_scan_complete(result)
        assert control.watch_session is not None
        assert control.watch_session.recursive is False
    finally:
        control._stop_watch_session()
        control.deleteLater()


def test_non_recursive_manual_refresh_and_overflow_admit_direct_files_only(tmp_path):
    app()
    root, _child, direct, nested = non_recursive_tree(tmp_path)
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([])
    service = FileRefreshService()
    for kind in (FilesystemEventKind.MANUAL_REFRESH,
                 FilesystemEventKind.OVERFLOW_UNKNOWN,
                 FilesystemEventKind.ROOT_RESTORED):
        plan = build(service, root, batch(root, kind, root), workspace,
                     recursive=False)
        added = {result.path for result in plan.results
                 if result.action is RefreshAction.ADD}
        assert direct in added, kind
        assert nested not in added, kind

    recursive_plan = build(
        service, root, batch(root, FilesystemEventKind.MANUAL_REFRESH, root),
        workspace, recursive=True)
    recursive_added = {result.path for result in recursive_plan.results
                       if result.action is RefreshAction.ADD}
    assert {direct, nested} <= recursive_added


def test_non_recursive_directory_creation_never_pulls_in_descendants(tmp_path):
    app()
    root, child, _direct, nested = non_recursive_tree(tmp_path)
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([])
    plan = build(FileRefreshService(), root,
                 batch(root, FilesystemEventKind.DIRECTORY_CREATED, child),
                 workspace, recursive=False)
    assert not [result for result in plan.results if result.action is RefreshAction.ADD]

    recursive_plan = build(
        FileRefreshService(), root,
        batch(root, FilesystemEventKind.DIRECTORY_CREATED, child),
        workspace, recursive=True)
    assert nested in {result.path for result in recursive_plan.results
                      if result.action is RefreshAction.ADD}


def test_non_recursive_session_ignores_a_file_event_from_a_subdirectory(tmp_path):
    app()
    root, _child, _direct, nested = non_recursive_tree(tmp_path)
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([])
    plan = build(FileRefreshService(), root,
                 batch(root, FilesystemEventKind.CREATED, nested),
                 workspace, recursive=False)
    assert plan.results == ()
