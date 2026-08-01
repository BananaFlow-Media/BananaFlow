from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QToolButton

from core.metadata_models import AudioTrackItem, OriginalTags
from core.metadata_processor import build_scan_result
from ui.controllers.metadata_controller import MetadataController
from ui.controllers.tag_editor_navigation_state import TagEditorNavigationState
from ui.panels.metadata_editor.panel import MetadataEditorPanel


def _app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _state_with_current_folder(tmp_path):
    root = tmp_path / "music"
    album = root / "artist" / "album"
    album.mkdir(parents=True)
    state = TagEditorNavigationState()
    state.set_root(root)
    assert state.navigate(root / "artist")
    assert state.navigate(album)
    assert state.back()
    return state, root, album


def test_rename_remaps_current_and_forward_history(tmp_path):
    state, root, album = _state_with_current_folder(tmp_path)
    artist = album.parent
    renamed = root / "renamed-artist"

    artist.rename(renamed)
    state.remap_folder(artist, renamed)

    assert state.current == renamed
    assert state.can_go_forward
    assert state.forward()
    assert state.current == renamed / "album"


def test_move_remaps_current_and_forward_history(tmp_path):
    state, root, album = _state_with_current_folder(tmp_path)
    artist = album.parent
    destination_parent = root / "archive"
    destination_parent.mkdir()
    destination = destination_parent / artist.name

    shutil.move(str(artist), str(destination))
    state.remap_folder(artist, destination)

    assert state.current == destination
    assert state.can_go_forward
    assert state.forward()
    assert state.current == destination / "album"


def test_delete_current_folder_recovers_parent_and_prunes_stale_history(tmp_path):
    state, root, album = _state_with_current_folder(tmp_path)
    artist = album.parent
    missing = root / "missing"
    outside = tmp_path / "outside"
    state._back.extend([root, root, missing, outside])
    state._forward.extend([album, album, missing, outside])

    shutil.rmtree(artist)
    state.reconcile_after_delete(artist)

    assert state.current == root
    assert not state.can_go_back
    assert not state.can_go_forward


def _panel_at_album(tmp_path):
    _app()
    root = tmp_path / "music"
    album = root / "artist" / "album"
    album.mkdir(parents=True)
    track_path = album / "song.mp3"
    track_path.write_bytes(b"audio")
    track = AudioTrackItem(
        path=track_path,
        folder=album,
        ext=".mp3",
        original=OriginalTags(title="Original"),
    )
    track.proposed.title = "Pending"

    panel = MetadataEditorPanel()
    # Wire panel and controller exactly as AppWindow does: physical mutations
    # are controller-owned, so these tests drive the real production path
    # rather than a panel that reaches into the filesystem by itself.
    controller = MetadataController()
    controller._session.scan_result = build_scan_result(root, [track], 0, {root, album})
    panel.set_workspace_state(controller.workspace_state)
    controller.set_incremental_updater(panel.incremental_workspace_updater())
    panel.set_file_operation_owner(controller)
    panel._root_folder = root
    panel._navigation.set_root(root)
    panel._file_operations.set_root(root)
    panel._model.load_tracks([track])
    panel._workspace.set_selected_items([track])
    panel._workspace.set_apply_excluded({track.path}, True)
    panel._rebuild_tree_from_loaded_tracks()
    assert panel._navigation.navigate(album)
    panel._apply_navigation_filter()
    return panel, controller, root, album, track


def _breadcrumb_labels(panel: MetadataEditorPanel) -> list[str]:
    return [
        widget.text()
        for index in range(panel._breadcrumbs_layout.count())
        if isinstance((widget := panel._breadcrumbs_layout.itemAt(index).widget()), QToolButton)
    ]


def _dispose(panel: MetadataEditorPanel, controller=None) -> None:
    panel.close()
    panel.deleteLater()
    if controller is not None:
        controller.deleteLater()
    QApplication.processEvents()


def test_panel_rename_rebases_filter_breadcrumb_tree_and_workspace(monkeypatch, tmp_path):
    panel, controller, _root, album, track = _panel_at_album(tmp_path)
    identity = panel._workspace.item_id(track)
    try:
        monkeypatch.setattr("ui.panels.metadata_editor.prompts.get_text", lambda *args, **kwargs: ("renamed", True))
        panel._on_tree_rename(album, is_file=False)

        renamed = album.parent / "renamed"
        assert panel._navigation.current == renamed
        assert panel._proxy.rowCount() == 1
        assert panel._tree.currentItem().data(0, Qt.UserRole) == renamed
        assert "renamed" in _breadcrumb_labels(panel)
        assert track.path == renamed / "song.mp3"
        assert panel._workspace.item_id(track) == identity
        assert panel._workspace.selected_tracks() == [track]
        assert panel._workspace.excluded_tracks() == [track]
        assert panel._workspace.apply_candidates() == []
        # The controller — not the panel — performed and recorded the rename.
        entries = controller._own_operations.entries()
        assert [entry.operation_type for entry in entries] == ["rename"]
        assert entries[0].final_state_recorded
    finally:
        _dispose(panel, controller)


def test_panel_move_and_delete_keep_navigation_filter_and_apply_state(monkeypatch, tmp_path):
    panel, controller, root, album, track = _panel_at_album(tmp_path)
    identity = panel._workspace.item_id(track)
    archive = root / "archive"
    archive.mkdir()
    destination = archive / album.name
    try:
        panel._on_tree_item_moved(album, destination)

        assert panel._navigation.current == destination
        assert panel._proxy.rowCount() == 1
        assert track.path == destination / "song.mp3"
        assert panel._workspace.item_id(track) == identity
        assert panel._workspace.selected_tracks() == [track]
        assert panel._workspace.excluded_tracks() == [track]
        assert panel._workspace.apply_candidates() == []
        assert [entry.operation_type for entry in controller._own_operations.entries()] == ["move"]

        monkeypatch.setattr("ui.panels.metadata_editor.prompts.confirm", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            "core.metadata_processor.send_to_recycle_bin", lambda path: shutil.rmtree(path))
        panel._on_tree_delete(destination, is_file=False)

        assert panel._navigation.current == archive
        assert panel._proxy.rowCount() == 0
        assert panel._tree.currentItem().data(0, Qt.UserRole) == archive
        assert "archive" in _breadcrumb_labels(panel)
        assert panel._workspace.selected_tracks() == []
        assert panel._workspace.apply_candidates() == []
        recycle = [entry for entry in controller._own_operations.entries()
                   if entry.operation_type == "recycle"]
        assert len(recycle) == 1 and recycle[0].expected_absence
    finally:
        _dispose(panel, controller)


def test_panel_never_mutates_the_filesystem_without_a_controller_owner(monkeypatch, tmp_path):
    """The panel must refuse an unowned mutation instead of doing it itself."""
    panel, controller, _root, album, track = _panel_at_album(tmp_path)
    try:
        panel.set_file_operation_owner(None)
        monkeypatch.setattr("ui.panels.metadata_editor.prompts.get_text",
                            lambda *args, **kwargs: ("renamed", True))
        panel._on_tree_rename(album, is_file=False)
        assert album.exists() and not (album.parent / "renamed").exists()
        assert track.path == album / "song.mp3"
    finally:
        _dispose(panel, controller)
