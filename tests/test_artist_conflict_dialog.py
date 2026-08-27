"""Headless checks for exclusive category controls in duplicate dialogs."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from core.artist_catalog import CatalogDuplicateGroup
from core.duplicate_detector import DuplicateGroup, VideoInfo


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_catalog_only_category_is_exclusive_per_group(app):
    from ui.dialogs.catalog_conflict_dialog import CatalogConflictDialog

    tracks = [
        {"title": "Song", "catalog_section": "album", "album": "Album"},
        {"title": "Song", "catalog_section": "single", "album": "Single"},
    ]
    group = CatalogDuplicateGroup("g", "Song", "exact", (0, 1))
    dialog = CatalogConflictDialog([group], tracks)

    dialog.select_only_category("single")
    assert dialog._cards[0].selected_indices() == {1}
    dialog.set_all(True)
    assert dialog._cards[0].selected_indices() == {0, 1}
    dialog.close()


def test_youtube_only_videos_clears_playlist_copy(app):
    from ui.dialogs.conflict_resolution_dialog import ConflictResolutionDialog

    video = VideoInfo("id", "Song", "u", "", 10, "Videos", "videos")
    playlist = VideoInfo(
        "id", "Song", "u", "", 10, "Playlists", "playlist_item",
        playlist_name="Mix", playlist_index=1,
    )
    dialog = ConflictResolutionDialog([DuplicateGroup("id", "Song", [video, playlist])])

    dialog._select_only("non_playlist")
    decision = dialog._cards[0].build_decision()
    assert decision.keep_keys == {"Videos"}
    dialog.close()
