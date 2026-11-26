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


@pytest.mark.parametrize("redirected", [False, True])
def test_youtube_flat_playlist_keeps_its_folder_and_source_positions(monkeypatch, redirected):
    from unittest.mock import MagicMock

    import yt_dlp

    from core.filename_numbering import decide_numbering
    from core.playlist_parser import PlaylistParser, SourcePlatform, UrlKind

    canonical = "https://www.youtube.com/playlist?list=PLSourcePlaylist"
    url = "https://www.google.com/goto?url=opaque" if redirected else canonical
    youtube_dl = MagicMock()
    youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
        "_type": "playlist",
        "webpage_url": canonical,
        "title": "Shared Playlist",
        "entries": [
            {"id": "aaaaaaaaaaa", "title": "First", "artist": "Artist One"},
            None,
            {"id": "bbbbbbbbbbb", "title": "Third", "album": "Different Album"},
            {"id": "ccccccccccc", "title": "Reported position", "playlist_index": 9},
        ],
    }
    monkeypatch.setattr(yt_dlp, "YoutubeDL", youtube_dl)
    emitted = []
    result = PlaylistParser().parse(url, on_item=lambda track, *_: emitted.append(track))

    assert result.success()
    assert result.kind is UrlKind.PLAYLIST
    assert result.platform is SourcePlatform.YOUTUBE
    assert result.url == canonical
    assert emitted == result.tracks
    assert [track.index for track in emitted] == [1, 2, 3]
    assert [track.album_index for track in emitted] == [1, 3, 9]
    for track in emitted:
        assert track.source_url == canonical
        assert track.source_kind == UrlKind.PLAYLIST.name
        decision = decide_output_layout(
            source_kind=track.source_kind, release_type=track.release_type,
            collection_title=track.collection_title, album=track.album,
            parent_artist=track.parent_artist, artist=track.artist,
        )
        assert decision.render_folder() == "Shared Playlist"
        numbering = decide_numbering(
            source_kind=track.source_kind, release_type=track.release_type,
            collection_index=track.album_index,
        )
        assert numbering.filename_index == track.album_index
        assert numbering.metadata_track_index is None


@pytest.mark.parametrize("fallback", ["paginated", "links", "stream"])
def test_generic_fallback_preserves_collection_or_single_video(monkeypatch, fallback):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import yt_dlp

    from core import listing_scraper, universal_extractor
    from core.playlist_parser import PlaylistParser, UrlKind

    url = "https://media.example/selected-videos"
    video = {"url": "https://media.example/watch/1", "title": "Only Video"}
    youtube_dl = MagicMock()
    youtube_dl.return_value.__enter__.return_value.extract_info.return_value = None
    monkeypatch.setattr(yt_dlp, "YoutubeDL", youtube_dl)

    def listing(_url, on_item, **_kwargs):
        if fallback == "paginated":
            row = dict(video, album="Collection", collection_title="Collection", album_index=1)
            on_item(row)
            return "Collection", [row]
        return "", []

    monkeypatch.setattr(listing_scraper, "scrape_listing_page", listing)
    monkeypatch.setattr(
        universal_extractor, "scrape_generic_video_listing",
        lambda *_args, **_kwargs: [video] if fallback == "links" else [],
    )
    monkeypatch.setattr(
        universal_extractor, "find_streams",
        lambda *_args, **_kwargs: [SimpleNamespace(
            url=video["url"], page_title=video["title"], stream_type="hls",
        )],
    )
    emitted = []
    result = PlaylistParser().parse(url, on_item=lambda t, *_: emitted.append(t))
    assert result.success()
    assert emitted == result.tracks and len(emitted) == 1
    track = emitted[0]
    if fallback == "stream":
        assert result.kind is UrlKind.SINGLE_VIDEO
        assert result.playlist_title == "Only Video"
        assert track.collection_title == ""
        assert track.album_index == 0
    else:
        expected_title = "Collection" if fallback == "paginated" else "selected videos"
        assert result.kind is UrlKind.PLAYLIST
        assert result.playlist_title == track.collection_title == expected_title
        assert track.album_index == 1
    assert track.source_kind == result.kind.name
    assert track.source_url == url


@pytest.mark.parametrize(
    ("kind", "release", "artist", "title", "expected"),
    [
        ("PLAYLIST", "playlist", "Artist", "Jazz / Ambient", "Jazz - Ambient"),
        ("ARTIST", "album", "AC/DC", "Live \\ Deluxe", "AC-DC/Albums/Live - Deluxe"),
    ],
)
def test_slashes_in_provider_names_do_not_add_folders(kind, release, artist, title, expected):
    from core.downloader import _sanitize_folder_name
    from core.output_layout import english_category_name

    decision = decide_output_layout(
        source_kind=kind, release_type=release, collection_title=title,
        album="", parent_artist=artist, artist=artist,
    )
    rendered = decision.render_folder(localize_category=english_category_name)
    assert _sanitize_folder_name(rendered) == expected


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
