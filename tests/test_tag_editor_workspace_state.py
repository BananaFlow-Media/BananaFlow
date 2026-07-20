from __future__ import annotations

import os

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.metadata_models import ApplyOutcome, AudioTrackItem, OriginalTags
from ui.controllers.metadata_controller import MetadataController
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_filter_proxy_model import MetadataFilterProxyModel
from ui.models.metadata_table_model import COL_FILENAME, MetadataTableModel
from ui.panels.metadata_editor.panel import MetadataEditorPanel


def _app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _tracks(tmp_path):
    result = []
    for name in ("zulu.mp3", "alpha.mp3", "middle.mp3"):
        path = tmp_path / name
        result.append(AudioTrackItem(path=path, folder=tmp_path, ext=".mp3", original=OriginalTags(title=name)))
    return result


def _workspace(tmp_path):
    state = TagEditorWorkspaceState()
    tracks = _tracks(tmp_path)
    state.set_tracks(tracks)
    return state, tracks


def _changed(item, title="Changed"):
    item.proposed.title = title


def test_selection_and_apply_scope_are_independent(tmp_path):
    state, tracks = _workspace(tmp_path)
    state.set_selected_paths({tracks[0].path})
    assert state.apply_candidates() == []  # selecting unchanged never includes it

    _changed(tracks[1])
    assert state.apply_candidates() == [tracks[1]]  # unselected edit is auto-included
    state.set_selected_paths(set())
    assert state.apply_candidates() == [tracks[1]]  # deselection cannot remove it


def test_read_only_and_unsupported_items_keep_filename_apply_eligibility(tmp_path):
    state = TagEditorWorkspaceState()
    readonly = AudioTrackItem(path=tmp_path / "read.aac", folder=tmp_path, ext=".aac", metadata_editable=False)
    unsupported = AudioTrackItem(path=tmp_path / "unknown.bin", folder=tmp_path, ext=".bin", metadata_editable=False)
    state.set_tracks([readonly, unsupported])
    readonly.proposed.title = "must not write"
    assert state.apply_candidates() == []
    readonly.proposed_filename = "renamed.aac"
    unsupported.proposed_filename = "renamed.bin"
    assert state.apply_candidates() == [readonly, unsupported]
    state.set_apply_excluded({readonly.path}, True)
    assert state.apply_candidates() == [unsupported]


def test_read_only_mixed_proposal_renames_without_metadata_writer(tmp_path, monkeypatch):
    from ui.workers.metadata_worker import MetadataApplyWorker

    path = tmp_path / "source.aac"; path.write_bytes(b"opaque audio")
    item = AudioTrackItem(path=path, folder=tmp_path, ext=".aac", metadata_editable=False)
    item.proposed.title = "must remain pending"
    item.proposed_filename = "renamed.aac"
    state = TagEditorWorkspaceState(); state.set_tracks([item]); stable_id = state.item_id(item)
    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", lambda *a: pytest.fail("metadata writer called"))
    worker = MetadataApplyWorker([item], tmp_path / "backup.json", operation_id="readonly-rename", root=tmp_path)
    results = []; worker.finished.connect(results.append); worker.run()
    assert results and results[0].success_count == 1
    assert item.path.name == "renamed.aac" and item.proposed_filename is None
    assert item.proposed.title == "must remain pending"
    state.reconcile_apply_outcome(results[0].outcomes[0])
    assert state.item_id(item) == stable_id and state.track_for_path(item.path) is item


def test_read_only_failed_rename_remains_pending(tmp_path):
    from ui.workers.metadata_worker import MetadataApplyWorker

    path = tmp_path / "source.aac"; path.write_bytes(b"opaque audio")
    (tmp_path / "taken.aac").write_bytes(b"exists")
    item = AudioTrackItem(path=path, folder=tmp_path, ext=".aac", metadata_editable=False, proposed_filename="taken.aac")
    worker = MetadataApplyWorker([item], tmp_path / "backup.json", operation_id="readonly-blocked", root=tmp_path)
    results = []; worker.finished.connect(results.append); worker.run()
    # A late/fresh physical collision invalidates the complete reviewed batch;
    # it is not a partial metadata Apply even for rename-only media.
    assert results and results[0].aborted and not results[0].preflight_ok
    assert results[0].outcomes == []
    assert item.path == path and item.proposed_filename == "taken.aac"


def test_explicit_exclusion_and_inclusion_do_not_depend_on_selection(tmp_path):
    state, tracks = _workspace(tmp_path)
    _changed(tracks[0])
    _changed(tracks[1], "Other")
    state.set_apply_excluded({tracks[0].path}, True)
    assert state.apply_candidates() == [tracks[1]]
    assert state.excluded_tracks() == [tracks[0]]

    state.set_selected_paths({tracks[0].path})
    state.set_selected_paths(set())
    assert state.apply_candidates() == [tracks[1]]

    state.set_apply_excluded({tracks[0].path}, False)
    assert {item.path for item in state.apply_candidates()} == {tracks[0].path, tracks[1].path}


def test_reverting_clears_stale_exclusion_and_next_edit_reincludes(tmp_path):
    state, tracks = _workspace(tmp_path)
    _changed(tracks[0])
    state.set_apply_excluded({tracks[0].path}, True)
    tracks[0].proposed.clear()
    assert state.apply_candidates() == []
    assert not tracks[0].excluded_from_apply

    _changed(tracks[0], "Again")
    assert state.apply_candidates() == [tracks[0]]


def test_proxy_filtering_sorting_and_mapping_do_not_change_apply_scope(tmp_path):
    _app()
    state, tracks = _workspace(tmp_path)
    _changed(tracks[0])
    _changed(tracks[2], "Changed middle")
    model = MetadataTableModel(workspace=state)
    model.load_tracks(tracks)
    proxy = MetadataFilterProxyModel(state)
    proxy.setSourceModel(model)

    expected = {item.path for item in state.apply_candidates()}
    proxy.set_path_visible(tracks[0].path, False)
    assert {item.path for item in state.apply_candidates()} == expected
    assert tracks[0].path not in {item.path for item in proxy.visible_tracks()}

    proxy.sort(COL_FILENAME, Qt.AscendingOrder)
    assert {item.path for item in state.apply_candidates()} == expected
    for row in range(proxy.rowCount()):
        item = proxy.track_at_row(row)
        source = proxy.mapToSource(proxy.index(row, 0))
        assert item is model.track_at_row(source.row())

    proxy.set_show_excluded_only(True)
    assert state.apply_candidates()
    assert proxy.rowCount() == 0


def test_reset_discards_stale_path_references(tmp_path):
    state, tracks = _workspace(tmp_path)
    _changed(tracks[0])
    state.set_selected_paths({tracks[0].path})
    state.set_apply_excluded({tracks[0].path}, True)

    replacement = AudioTrackItem(
        path=tmp_path / "replacement.mp3", folder=tmp_path, ext=".mp3", original=OriginalTags(title="new")
    )
    state.set_tracks([replacement])
    assert state.selected_tracks() == []
    assert state.excluded_tracks() == []
    assert state.apply_candidates() == []


def test_bulk_edit_uses_selected_scope_and_only_proposes(tmp_path):
    state, tracks = _workspace(tmp_path)
    state.set_selected_paths({tracks[1].path})
    controller = MetadataController()
    sentinel = tracks[1].path
    sentinel.write_bytes(b"metadata must not write this file")
    before = sentinel.read_bytes()
    controller.apply_artist_to_scope("Artist", state.edit_scope())
    assert tracks[1].proposed.artist == "Artist"
    assert tracks[0].proposed.artist is None
    assert sentinel.read_bytes() == before
    controller.deleteLater()


def test_panel_apply_count_and_button_follow_workspace_candidates(tmp_path):
    _app()
    from ui.i18n import current_language, set_language
    previous_language = current_language()
    set_language("en")
    panel = MetadataEditorPanel()
    try:
        state = panel._workspace
        tracks = _tracks(tmp_path)
        panel._model.load_tracks(tracks)
        _changed(tracks[0])
        _changed(tracks[1], "Two")
        panel.on_auto_rules_applied()
        assert panel._apply_scope_lbl.text() == "2 files will be applied"
        assert panel._apply_btn.isEnabled()

        state.set_apply_excluded({tracks[0].path, tracks[1].path}, True)
        panel._refresh_checked_scope_state()
        assert panel._apply_scope_lbl.text() == "0 files will be applied"
        assert not panel._apply_btn.isEnabled()
    finally:
        panel.deleteLater()
        set_language(previous_language)


def test_rename_outcome_preserves_workspace_identity_and_proxy_visibility(tmp_path):
    _app()
    controller = MetadataController()
    try:
        state = controller.workspace_state
        tracks = _tracks(tmp_path)
        selected, hidden = tracks[:2]
        model = MetadataTableModel(workspace=state)
        model.load_tracks(tracks)
        proxy = MetadataFilterProxyModel(state)
        proxy.setSourceModel(model)
        old_selected, old_hidden = selected.path, hidden.path
        new_selected = tmp_path / "new-selected.mp3"
        new_hidden = tmp_path / "new-hidden.mp3"

        _changed(selected)
        state.set_selected_paths({old_selected})
        state.set_apply_excluded({old_selected}, True)
        proxy.set_allowed_paths({old_selected, tracks[2].path})
        assert selected in proxy.visible_tracks()
        assert hidden not in proxy.visible_tracks()

        selected.path = new_selected
        selected.folder = new_selected.parent
        hidden.path = new_hidden
        hidden.folder = new_hidden.parent
        controller._on_apply_file_outcome(ApplyOutcome(old_selected, new_selected))
        controller._on_apply_file_outcome(ApplyOutcome(old_hidden, new_hidden))

        assert state.selected_tracks() == [selected]
        assert selected in proxy.visible_tracks()
        assert hidden not in proxy.visible_tracks()
        assert state.track_for_path(old_selected) is None
        assert state.track_for_path(new_selected) is selected
        state.set_selected_paths({new_selected})
        assert state.selected_tracks() == [selected]
        state.set_apply_excluded({new_selected}, False)
        assert selected in state.apply_candidates()
        state.set_apply_excluded({new_selected}, True)
        assert selected not in state.apply_candidates()

        selected.proposed.clear()  # successful Apply left no proposal
        assert state.apply_candidates() == []
        _changed(selected, "Edited after rename")
        assert state.apply_candidates() == [selected]

        proxy.sort(COL_FILENAME, Qt.AscendingOrder)
        for row in range(proxy.rowCount()):
            item = proxy.track_at_row(row)
            source = proxy.mapToSource(proxy.index(row, 0))
            assert item is model.track_at_row(source.row())

        replacement = AudioTrackItem(
            path=old_selected, folder=tmp_path, ext=".mp3", original=OriginalTags(title="replacement")
        )
        state.add_tracks([replacement])
        assert state.selected_tracks() == [selected]
        assert replacement not in state.visible_tracks()
        assert not replacement.excluded_from_apply
    finally:
        controller.deleteLater()


def test_revert_all_clears_filename_proposals_exclusions_and_apply_ui(tmp_path):
    _app()
    from ui.i18n import current_language, set_language
    previous_language = current_language()
    set_language("en")
    panel = MetadataEditorPanel()
    controller = MetadataController()
    try:
        panel.set_workspace_state(controller.workspace_state)
        tracks = _tracks(tmp_path)
        filename_only, combined = tracks[:2]
        panel._model.load_tracks(tracks)
        filename_only.proposed_filename = "renamed.mp3"
        combined.proposed.title = "Metadata change"
        combined.proposed_filename = "combined.mp3"
        panel._workspace.set_apply_excluded({filename_only.path}, True)
        panel.on_auto_rules_applied()
        assert panel._apply_btn.isEnabled()

        controller.revert_all([filename_only, combined])
        panel.on_auto_rules_applied()
        assert not filename_only.has_changes
        assert not combined.has_changes
        assert filename_only.proposed_filename is None
        assert combined.proposed_filename is None
        assert not filename_only.excluded_from_apply
        assert panel._apply_scope_lbl.text() == "0 files will be applied"
        assert not panel._apply_btn.isEnabled()
    finally:
        controller.deleteLater()
        panel.deleteLater()
        set_language(previous_language)
