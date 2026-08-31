"""Provider-neutral output folder and collision-avoidance contracts."""

from __future__ import annotations

import pytest

from core.output_layout import decide_output_layout, english_category_name
from core.playlist_parser import UrlKind


@pytest.mark.parametrize(
    ("kwargs", "expected_folder", "include_artist"),
    [
        (
            dict(source_kind=UrlKind.SINGLE_VIDEO, release_type="single"),
            "", True,
        ),
        (
            dict(
                source_kind=UrlKind.PLAYLIST, release_type="playlist",
                collection_title="Road Trip", album="A Track Album",
                parent_artist="Track Artist",
            ),
            "Road Trip", False,
        ),
        (
            dict(
                source_kind=UrlKind.ALBUM, release_type="album",
                collection_title="The Album", album="The Album",
            ),
            "The Album", False,
        ),
        (
            dict(
                source_kind=UrlKind.ARTIST, release_type="album",
                collection_title="The Album", parent_artist="The Artist",
            ),
            "The Artist/Albums/The Album", False,
        ),
        (
            dict(
                source_kind=UrlKind.ARTIST, release_type="ep",
                collection_title="Small Release", parent_artist="The Artist",
                total_tracks=4,
            ),
            "The Artist/Singles & EPs/Small Release", False,
        ),
        (
            dict(
                source_kind=UrlKind.ARTIST, release_type="compilation",
                collection_title="Collected", parent_artist="The Artist",
            ),
            "The Artist/Compilations/Collected", True,
        ),
        (
            dict(
                source_kind=UrlKind.ARTIST, release_type="playlist",
                collection_title="Artist Picks", parent_artist="The Artist",
                total_tracks=1,
            ),
            "The Artist/Playlists/Artist Picks", False,
        ),
        (
            dict(
                source_kind=UrlKind.ARTIST, release_type="single",
                collection_title="One Song", parent_artist="The Artist",
                total_tracks=1,
            ),
            "The Artist/Singles & EPs", False,
        ),
    ],
)
def test_output_layout_matrix(kwargs, expected_folder, include_artist):
    defaults = dict(
        collection_title="", album="", parent_artist="", artist="Artist",
        category="", total_tracks=0, platform="spotify", track_title="Song",
    )
    defaults.update(kwargs)

    decision = decide_output_layout(**defaults)

    assert decision.render_folder(localize_category=english_category_name) == expected_folder
    assert decision.include_artist_in_filename is include_artist


def test_direct_playlist_ignores_track_artist_and_track_album_for_routing():
    decision = decide_output_layout(
        source_kind=UrlKind.PLAYLIST,
        release_type="playlist",
        collection_title="Shared Playlist",
        album="Unrelated Album",
        parent_artist="Track Artist",
        artist="Track Artist",
        total_tracks=20,
    )

    assert decision.render_folder() == "Shared Playlist"


def test_multi_disc_release_gets_a_disc_subfolder():
    decision = decide_output_layout(
        source_kind=UrlKind.ARTIST,
        release_type="album",
        collection_title="Long Album",
        album="Long Album",
        parent_artist="Artist",
        artist="Artist",
        multi_disc=True,
        disc_number=2,
    )

    assert decision.render_folder(
        localize_category=english_category_name,
        disc_label=lambda number: f"Disc {number}",
    ) == "Artist/Albums/Long Album/Disc 2"


def test_folder_setting_disables_every_automatic_subfolder():
    decision = decide_output_layout(
        source_kind=UrlKind.ARTIST,
        release_type="album",
        collection_title="Album",
        album="Album",
        parent_artist="Artist",
        artist="Artist",
        playlist_subfolders=False,
    )

    assert decision.render_folder() == ""


def test_legacy_card_does_not_turn_an_album_tag_into_a_folder():
    decision = decide_output_layout(
        source_kind="",
        release_type="",
        collection_title="",
        album="Descriptive Album Tag",
        parent_artist="",
        artist="Artist",
    )

    assert decision.render_folder() == ""


def test_singles_category_can_be_disabled_without_losing_ep_release_folder():
    ep = decide_output_layout(
        source_kind=UrlKind.ARTIST,
        release_type="ep",
        collection_title="EP Name",
        album="EP Name",
        parent_artist="Artist",
        artist="Artist",
        total_tracks=4,
        singles_subfolder=False,
    )
    single = decide_output_layout(
        source_kind=UrlKind.ARTIST,
        release_type="single",
        collection_title="Single Name",
        album="Single Name",
        parent_artist="Artist",
        artist="Artist",
        total_tracks=1,
        singles_subfolder=False,
    )

    assert ep.render_folder() == "Artist/EP Name"
    assert single.render_folder() == "Artist"
