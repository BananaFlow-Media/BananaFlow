from __future__ import annotations


def test_filename_sort_key_is_natural():
    from ui.models.metadata_table_model import _fold

    assert sorted(["track 10.mp3", "track 2.mp3", "track 1.mp3"], key=_fold) == [
        "track 1.mp3", "track 2.mp3", "track 10.mp3",
    ]


def test_file_operation_rebase_preserves_workspace_identity(tmp_path):
    from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState

    workspace = TagEditorWorkspaceState()
    track = _item(tmp_path, "old.mp3")
    track.proposed.title = "Pending title"
    model = MetadataTableModel(workspace=workspace)
    model.load_tracks([track])
    identity = workspace.item_id(track)

    destination = tmp_path / "Album" / "new.mp3"
    model.update_file_path(track, destination)

    assert workspace.item_id(track) == identity
    assert workspace.track_for_path(destination) is track
    assert track.proposed.title == "Pending title"

from PySide6.QtCore import Qt

from core.metadata_models import AudioTrackItem, OriginalTags, TrackStatus
from ui.i18n import TRANSLATIONS, set_language, t
from ui.models.metadata_table_model import (
    COL_FILENAME,
    COL_FILENAME_NEW,
    MetadataTableModel,
)


def _item(tmp_path, name: str = "Song.MP3") -> AudioTrackItem:
    path = tmp_path / name
    return AudioTrackItem(
        path=path,
        folder=tmp_path,
        ext=path.suffix,
        original=OriginalTags(title="Song"),
    )


def test_filename_tooltip_includes_file_semantics(tmp_path):
    set_language("en")
    item = _item(tmp_path)
    item.proposed_filename = "New Song.mp3"
    model = MetadataTableModel()
    model.load_tracks([item])

    tooltip = model.data(model.index(0, COL_FILENAME), Qt.ToolTipRole)

    assert f"Path: {item.path}" in tooltip
    assert "Type: MP3 audio file" in tooltip
    assert "New name: New Song.mp3" in tooltip


def test_filename_tooltip_reports_unsupported_status(tmp_path):
    set_language("en")
    item = _item(tmp_path, "raw-file.bin")
    item.status = TrackStatus.UNSUPPORTED
    item.error_msg = t("meta_unsupported_format_tooltip")
    model = MetadataTableModel()
    model.load_tracks([item])

    tooltip = model.data(model.index(0, COL_FILENAME_NEW), Qt.ToolTipRole)

    assert "Status: Unsupported format" in tooltip


def test_filename_accessible_roles_reuse_file_semantics(tmp_path):
    set_language("en")
    item = _item(tmp_path)
    model = MetadataTableModel()
    model.load_tracks([item])

    index = model.index(0, COL_FILENAME)

    assert model.data(index, Qt.AccessibleTextRole) == "Song.MP3"
    assert f"Path: {item.path}" in model.data(index, Qt.AccessibleDescriptionRole)


def test_file_tooltip_i18n_keys_exist():
    keys = {
        "mt_file_tooltip_path",
        "mt_file_tooltip_type",
        "mt_file_tooltip_status",
        "mt_file_tooltip_new_name",
        "mt_file_type_audio",
        "mt_file_type_unknown",
        "mt_status_error",
        "meta_a11y_file_tree",
        "meta_a11y_file_tree_desc",
        "meta_a11y_details_table",
        "meta_a11y_details_table_desc",
        "meta_a11y_table_header",
        "meta_a11y_zoom_out",
        "meta_a11y_zoom_value",
        "meta_a11y_zoom_in",
    }

    assert keys <= TRANSLATIONS["en"].keys()
    assert keys <= TRANSLATIONS["he"].keys()
