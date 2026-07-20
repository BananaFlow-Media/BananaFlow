import os
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_navigation_state import TagEditorNavigationState
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_filter_proxy_model import MetadataFilterProxyModel
from ui.models.metadata_table_model import MetadataTableModel

# Absolute roots on the host OS: "C:/music" is absolute on Windows but a relative
# path on POSIX, and set_root() normalizes to absolute — so the un-normalized
# literal no longer equalled state.current on macOS/Linux.
_MUSIC = Path("C:/music") if os.name == "nt" else Path("/music")
_ELSEWHERE = Path("C:/elsewhere") if os.name == "nt" else Path("/elsewhere")


def test_navigation_history_is_root_bounded():
    root = _MUSIC
    album = root / "artist" / "album"
    state = TagEditorNavigationState()
    state.set_root(root)

    assert state.current == root
    assert not state.navigate(_ELSEWHERE)
    assert state.navigate(root / "artist")
    assert state.navigate(album)
    assert state.can_go_back and state.can_go_up
    assert state.up() and state.current == root / "artist"
    assert state.back() and state.current == album
    assert state.forward() and state.current == root / "artist"


def test_navigation_normalizes_paths_clears_forward_and_resets_for_new_root(tmp_path):
    root = tmp_path / "root"
    nested = root / "artist" / "album"
    state = TagEditorNavigationState()
    state.set_root(root)

    assert state.navigate(nested / "..")
    assert state.current == root / "artist"
    assert state.back()
    assert state.can_go_forward
    assert state.navigate(root / "other")
    assert not state.can_go_forward

    state.set_root(tmp_path / "replacement")
    assert state.current == tmp_path / "replacement"
    assert not state.can_go_back and not state.can_go_forward and not state.can_go_up


def test_proxy_folder_and_search_filters_do_not_change_apply_scope(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    root = tmp_path / "music"
    first = AudioTrackItem(
        path=root / "one" / "alpha.mp3", folder=root / "one", ext=".mp3",
        original=OriginalTags(title="Alpha", artist="Artist"),
    )
    second = AudioTrackItem(
        path=root / "two" / "bravo.mp3", folder=root / "two", ext=".mp3",
        original=OriginalTags(title="Bravo", artist="Other"),
    )
    first.proposed.title = "Alpha edited"
    second.proposed.title = "Bravo edited"
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([first, second])
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks([first, second])
    workspace.set_selected_items([first])
    proxy = MetadataFilterProxyModel(workspace)
    proxy.setSourceModel(model)

    proxy.set_folder(root / "one")
    assert proxy.rowCount() == 1
    assert workspace.apply_candidates() == [first, second]
    assert workspace.selected_tracks() == [first]

    proxy.set_search_text("bravo")
    assert proxy.rowCount() == 0
    assert workspace.apply_candidates() == [first, second]

    proxy.set_folder(None)
    assert proxy.rowCount() == 1
    assert proxy.track_at_row(0) is second


def test_migration_8_preserves_valid_last_folder_and_is_idempotent():
    from config_migrate import CURRENT_VERSION, migrate

    data = {"config_version": 7, "tag_editor_last_folder": "C:/music"}
    assert migrate(data)
    assert data["config_version"] == CURRENT_VERSION
    assert CURRENT_VERSION >= 8
    assert data["tag_editor_last_folder"] == "C:/music"
    assert not migrate(data)
