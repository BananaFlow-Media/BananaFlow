from pathlib import Path

import pytest

from core.change_sets import FileIdentity
from core.metadata_io import CancellationToken, IOScope, MetadataIOError, MetadataValueSource
from core.metadata_models import AudioTrackItem, OriginalTags
from core.playlist_export import (
    PlaylistFormat, PlaylistOrder, PlaylistPathMode, build_playlist_plan,
    export_playlist, render_playlist,
)
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def workspace(tmp_path: Path):
    root = tmp_path / "מוזיקה"; root.mkdir()
    specs = (("10 שיר.mp3", 2, 10, "כותרת\nחדשה", "אמן", 125.8),
             ("2 שיר.mp3", 1, 2, "", "", None))
    items = []
    for name, disc, track, title, artist, duration in specs:
        path = root / name; path.write_bytes(b"media")
        tags = OriginalTags(title=title, artist=artist, disc_num=disc, track_num=track,
                            file_properties={"duration_seconds": duration})
        items.append(AudioTrackItem(path, root, ".mp3", original=tags, format_id="mp3",
            baseline_identity=FileIdentity(str(path), path.stat().st_size, path.stat().st_mtime_ns)))
    state = TagEditorWorkspaceState(); state.set_tracks(items)
    return state, root, items


def test_m3u8_current_paths_extinf_unicode_and_pending_rename_warning(tmp_path):
    state, root, items = workspace(tmp_path)
    items[0].proposed_filename = "future.mp3"
    ids = tuple(state.item_id(item) for item in items)
    plan = build_playlist_plan(state, item_ids=ids, scope=IOScope.ALL_LOADED,
        order=PlaylistOrder.CURRENT_VIEW, path_mode=PlaylistPathMode.AUTO,
        value_source=MetadataValueSource.EFFECTIVE, format=PlaylistFormat.M3U8)
    destination = tmp_path / "playlist.m3u8"
    data = render_playlist(plan, destination).decode("utf-8")
    assert data.startswith("#EXTM3U\r\n")
    assert "#EXTINF:125,אמן - כותרת חדשה" in data
    assert "future.mp3" not in data and "10 שיר.mp3" in data
    assert "#EXTINF:-1,2 שיר" in data
    assert any(warning.kind.value == "pending_rename" for warning in plan.warnings)
    export_playlist(plan, destination)
    assert destination.read_bytes() == render_playlist(plan, destination)


def test_natural_and_track_order_are_explicit_snapshots(tmp_path):
    state, _, items = workspace(tmp_path); ids = tuple(state.item_id(item) for item in items)
    natural = build_playlist_plan(state, item_ids=ids, scope=IOScope.VISIBLE,
                                  order=PlaylistOrder.NATURAL_PATH)
    track = build_playlist_plan(state, item_ids=ids, scope=IOScope.SELECTED,
                                order=PlaylistOrder.TRACK_DISC)
    assert natural.entries[0].path.name == "2 שיר.mp3"
    assert track.entries[0].track_number == 2
    items.reverse()
    assert natural.entries[0].path.name == "2 שיר.mp3"


def test_m3u_has_bom_absolute_mode_missing_files_and_cancel(tmp_path):
    state, _, items = workspace(tmp_path); ids = tuple(state.item_id(item) for item in items)
    items[1].path.unlink()
    plan = build_playlist_plan(state, item_ids=ids, scope=IOScope.ALL_LOADED,
        path_mode=PlaylistPathMode.ABSOLUTE, format=PlaylistFormat.M3U)
    destination = tmp_path / "playlist.m3u"
    data = render_playlist(plan, destination)
    assert data.startswith(b"\xef\xbb\xbf") and str(items[0].path.resolve()).encode("utf-8") in data
    assert len(plan.entries) == 1 and any(w.kind.value == "missing_file" for w in plan.warnings)
    token = CancellationToken(); token.cancel()
    with pytest.raises(MetadataIOError):
        export_playlist(plan, destination, cancellation=token)
    assert not destination.exists()
