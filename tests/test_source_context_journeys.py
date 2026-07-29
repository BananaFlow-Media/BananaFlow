"""Behavior contracts for source identity, artwork, and output routing."""

from __future__ import annotations

import os
import threading

import pytest

from core.playlist_parser import UrlKind


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Qt application tests are Windows-only")


def _options(output_dir):
    return {
        "media_type": "audio",
        "quality_label": "320",
        "audio_format": "mp3",
        "video_format": "mp4",
        "output_dir": str(output_dir),
    }


def _controller(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import AppConfig
    from ui.controllers.download_controller import DownloadController

    QApplication.instance() or QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = AppConfig()
    cfg.duplicate_action = "overwrite"
    cfg.playlist_subfolders = True
    cfg.playlist_index_prefix = True

    class Engine:
        def __init__(self):
            self._cancel_event = threading.Event()

    class Worker:
        def start(self):
            pass

    controller = DownloadController(cfg, Engine())
    built = []
    monkeypatch.setattr(
        controller, "_build_batch_worker",
        lambda jobs, preexisting: built.extend(jobs) or Worker(),
    )
    return controller, built


def test_independent_spotify_tracks_stay_root_level_and_keep_artwork(tmp_path, monkeypatch):
    from ui.components.track_card import TrackCard

    controller, built = _controller(tmp_path, monkeypatch)
    cards = [
        TrackCard(
            "First", "Artist One", queue_index=1, platform="spotify",
            track_url="https://music.youtube.com/watch?v=one",
            parent_artist="Artist One", album="Single One", release_type="single",
            thumbnail_url="https://image.spotify/one.jpg",
            source_kind=UrlKind.SINGLE_VIDEO.name,
            source_url="https://open.spotify.com/track/one",
        ),
        TrackCard(
            "Second", "Artist Two", queue_index=2, platform="spotify",
            track_url="https://music.youtube.com/watch?v=two",
            parent_artist="Artist Two", album="Single Two", release_type="single",
            thumbnail_url="https://image.spotify/two.jpg",
            source_kind=UrlKind.SINGLE_VIDEO.name,
            source_url="https://open.spotify.com/track/two",
        ),
    ]

    controller.start_batch(cards, _options(tmp_path / "out"), UrlKind.SINGLE_VIDEO, "Second")
    requests = [request for _key, request in built]
    assert len(requests) == 2
    assert all(request.playlist_name in (None, "") for request in requests)
    assert all(request.forced_index is None for request in requests)
    assert all(request.is_solo for request in requests)
    assert [request.source_url for request in requests] == [card.source_url for card in cards]
    assert [request.thumbnail_url for request in requests] == [card.thumbnail_url for card in cards]
    from core.download_request_codec import request_from_dict, request_to_dict
    restored = request_from_dict(request_to_dict(requests[0]))
    assert restored.source_kind == UrlKind.SINGLE_VIDEO.name
    assert restored.source_url == cards[0].source_url
    assert restored.thumbnail_url == cards[0].thumbnail_url


def test_parser_stamps_direct_source_identity_on_spotify_artwork(monkeypatch):
    import core.scraper as scraper
    from core.playlist_parser import PlaylistParser

    url = "https://open.spotify.com/track/2VxeLyX666F8uXCJ0dZF8B"

    def fake_scrape(_url, on_item, **_kwargs):
        row = {
            "title": "Shallow", "artist": "Lady Gaga, Bradley Cooper",
            "url": "ytsearch1:Lady Gaga Bradley Cooper Shallow audio",
            "thumbnail_url": "https://image.spotify/shallow.jpg",
            "parent_artist": "Lady Gaga", "album": "A Star Is Born",
            "release_type": "single", "match_status": "pending",
        }
        on_item(row)
        return "Shallow", [row]

    monkeypatch.setattr(scraper, "scrape_spotify_track", fake_scrape)
    result = PlaylistParser().parse(url)
    assert len(result.tracks) == 1
    track = result.tracks[0]
    assert track.thumbnail_url == "https://image.spotify/shallow.jpg"
    assert track.source_kind == UrlKind.SINGLE_VIDEO.name
    assert track.source_url == url


def test_queue_restart_preserves_artwork_and_source_identity():
    from types import SimpleNamespace
    from ui.app_window import AppWindow

    card = SimpleNamespace(
        title="Shallow", artist="Lady Gaga, Bradley Cooper",
        track_url="https://music.youtube.com/watch?v=track", duration="3:35",
        thumbnail_url="https://image.spotify/shallow.jpg", platform="spotify",
        album="A Star Is Born", parent_artist="Lady Gaga", release_type="single",
        category="", album_index=1, total_tracks=1, duration_sec=215,
        spotify_id="2Vxe", spotify_key_kind="spotify_id", match_status="pending",
        resolution_error="", source_kind=UrlKind.SINGLE_VIDEO.name,
        source_url="https://open.spotify.com/track/2Vxe",
        get_status=lambda: "queued",
    )

    class SaveShim:
        def __init__(self):
            self._queue_panel = SimpleNamespace(get_all_cards=lambda: [card])
            self._cfg = SimpleNamespace(queue_state=[], save=lambda: None)

    save = SaveShim()
    AppWindow._save_queue_state(save)
    saved = save._cfg.queue_state
    assert saved[0]["thumbnail_url"] == card.thumbnail_url
    assert saved[0]["source_kind"] == UrlKind.SINGLE_VIDEO.name
    assert saved[0]["source_url"] == card.source_url

    class RestoreShim:
        def __init__(self):
            self.items = []

        def _add_track_to_queue(self, item):
            self.items.append(item)

    restore = RestoreShim()
    AppWindow._restore_queue_state(restore, saved)
    assert restore.items[0].thumbnail_url == card.thumbnail_url
    assert restore.items[0].source_kind == UrlKind.SINGLE_VIDEO.name
    assert restore.items[0].source_url == card.source_url


def test_queue_restart_repairs_legacy_valid_spotify_unresolved_card():
    """A strict miss saved by the broken revision must not stay disabled."""
    from ui.components.track_card import TrackCard

    card = TrackCard(
        "השם יעזור", "Odeya", platform="spotify", track_url="",
        match_status="unresolved", resolution_error="spotify_unresolved_card",
        spotify_id="1UruS1fpkhIXklaJnttSzA",
    )

    assert card.match_status == "pending"
    assert card.resolution_error == ""
    assert card.is_downloadable()
    assert card.is_selected()


def test_search_results_are_independent_and_channel_imports_are_grouped(tmp_path, monkeypatch):
    from config import AppConfig
    from core.duplicate_detector import VideoInfo
    from core.playlist_parser import SourcePlatform
    from core.search_engine import ResultKind, SearchResult
    from ui.controllers.channel_flow_controller import ChannelFlowController
    from ui.controllers.search_controller import SearchController

    search = SearchController(AppConfig())
    emitted = []
    search.result_to_queue.connect(emitted.append)
    search.add_to_queue(SearchResult(
        result_index=1, title="Found", artist="Artist",
        url="https://youtu.be/search00001", platform=SourcePlatform.YOUTUBE,
        kind=ResultKind.TRACK, thumbnail_url="https://image/search.jpg",
    ))
    assert emitted[0].source_kind == UrlKind.SINGLE_VIDEO.name
    assert emitted[0].source_url == "https://youtu.be/search00001"

    channel_url = "https://youtube.com/@artist"
    channel = ChannelFlowController(
        channel_url=channel_url, channel_name="Channel Artist",
        config=AppConfig(), parent_widget=None,
    )
    tracks = channel._build_tracks({"Videos": [VideoInfo(
        video_id="channel0001", title="Video",
        url="https://youtu.be/channel0001", thumbnail_url="",
        duration_sec=60, tab_name="Videos", tab_type="videos",
    )]})
    assert tracks[0]["source_kind"] == UrlKind.ARTIST.name
    assert tracks[0]["source_url"] == channel_url


@pytest.mark.parametrize(
    ("source_kind", "release_type", "album", "expected_fragment", "expected_index"),
    [
        (UrlKind.ALBUM.name, "album", "Album Name", "Album Name", 3),
        (UrlKind.PLAYLIST.name, "playlist", "Playlist Name", "Playlist Name", None),
        (UrlKind.ARTIST.name, "single", "Single Name", "Artist Name", 7),
    ],
)
def test_grouped_sources_keep_collection_paths_when_one_card_is_selected(
    tmp_path, monkeypatch, source_kind, release_type, album,
    expected_fragment, expected_index,
):
    from ui.components.track_card import TrackCard

    controller, built = _controller(tmp_path, monkeypatch)
    card = TrackCard(
        "Track", "Artist Name", queue_index=7, platform="spotify",
        track_url="https://music.youtube.com/watch?v=track",
        parent_artist="" if source_kind == UrlKind.PLAYLIST.name else "Artist Name",
        album=album, release_type=release_type, album_index=3,
        source_kind=source_kind, source_url=f"https://open.spotify.com/{source_kind.lower()}/id",
    )
    controller.start_batch([card], _options(tmp_path / "out"), UrlKind.SINGLE_VIDEO, "Wrong Global")
    request = built[0][1]
    assert expected_fragment in (request.playlist_name or "")
    assert request.forced_index == expected_index
    assert not request.is_solo


def test_spotify_artwork_reaches_final_embedding_and_failure_stays_nonfatal(
    tmp_path, monkeypatch,
):
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    import core.thumbnail_cropper as cropper
    import core.downloader as downloader

    output = tmp_path / "track.mp3"
    output.write_bytes(b"audio")
    calls = []
    monkeypatch.setattr(downloader.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        cropper, "embed_custom_thumbnail",
        lambda path, url, **kwargs: calls.append((path, url, kwargs)) or False,
    )
    request = DownloadRequest(
        url="https://music.youtube.com/watch?v=track",
        output_dir=str(tmp_path), media_type=MediaType.AUDIO,
        thumbnail_url="https://image.spotify/cover.jpg",
    )
    failures = DownloadEngine()._run_final_pipeline(request, str(output))
    assert calls[0][1] == "https://image.spotify/cover.jpg"
    assert "thumbnail embed" in failures
    assert output.exists()
