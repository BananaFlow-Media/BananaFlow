"""Focused behavioral regressions found by the final source-app retest."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest


def _spotify_album_html(artist: str) -> str:
    track = {
        "id": "1UruS1fpkhIXklaJnttSzA",
        "uri": "spotify:track:1UruS1fpkhIXklaJnttSzA",
        "name": "השם יעזור",
        "artists": {"items": [{
            "id": "28jEBK1RysfSUBHFofFflA",
            "profile": {"name": artist},
        }]},
        "duration": {"totalMilliseconds": 190000},
    }
    data = {"entities": {"items": {"spotify:album:5tu": {
        "__typename": "Album",
        "name": "השם יעזור",
        "coverArt": {"sources": [{
            "url": "https://i.scdn.co/image/cover",
            "width": 640, "height": 640,
        }]},
        "tracks": {"items": [{"track": track}]},
    }}}}
    payload = base64.b64encode(
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return (
        '<script id="initialState" type="text/plain">'
        + payload
        + "</script>"
    )


class _SpotifyPage:
    def __init__(self, context_kwargs):
        self._context_kwargs = context_kwargs

    def route(self, *_args, **_kwargs):
        pass

    def goto(self, *_args, **_kwargs):
        pass

    def content(self):
        artist = "אודיה" if self._context_kwargs.get("locale") == "he-IL" else "Odeya"
        return _spotify_album_html(artist)


class _SpotifyBrowser:
    def __init__(self):
        self.context_kwargs = None

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return SimpleNamespace(
            new_page=lambda: _SpotifyPage(kwargs),
        )

    def close(self):
        pass


class _SpotifyPlaywright:
    def __init__(self, browser):
        self.chromium = SimpleNamespace(launch=lambda **_kwargs: browser)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.mark.skipif(os.name != "nt", reason="Qt application tests are Windows-only")
def test_hebrew_spotify_display_survives_queue_restart_and_download_request(
    tmp_path, monkeypatch,
):
    """The user-facing locale must be chosen before the first card is emitted.

    This follows the localized Spotify page through parser -> card -> queue
    persistence -> DownloadRequest, the boundaries at which the real display
    metadata could previously drift or be replaced by matching metadata.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import AppConfig
    from core.playlist_parser import PlaylistParser, UrlKind
    import core.scraper as scraper
    from ui import i18n
    from ui.app_window import AppWindow
    from ui.components.track_card import TrackCard
    from ui.controllers.download_controller import DownloadController

    QApplication.instance() or QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    browser = _SpotifyBrowser()
    monkeypatch.setattr(
        scraper,
        "_sync_playwright_for",
        lambda _feature: lambda: _SpotifyPlaywright(browser),
    )

    original_language = i18n.current_language()
    try:
        i18n.set_language("he")
        album_url = "https://open.spotify.com/album/5tuYABXBjwZ5aUYQHmXzNk"
        result = PlaylistParser().parse(album_url)
    finally:
        i18n.set_language(original_language)

    assert result.error == ""
    assert browser.context_kwargs["locale"] == "he-IL"
    assert result.tracks[0].artist == "אודיה"

    track = result.tracks[0]
    card = TrackCard(
        track.title, track.artist, platform="spotify", queue_index=1,
        track_url=track.url, album=track.album,
        parent_artist=track.parent_artist, release_type=track.release_type,
        album_index=track.album_index, thumbnail_url=track.thumbnail_url,
        total_tracks=track.total_tracks, duration_sec=track.duration_sec,
        spotify_id=track.spotify_id, spotify_key_kind=track.spotify_key_kind,
        match_status=track.match_status, source_kind=track.source_kind,
        source_url=track.source_url,
    )

    class SaveShim:
        def __init__(self):
            self._queue_panel = SimpleNamespace(get_all_cards=lambda: [card])
            self._cfg = SimpleNamespace(queue_state=[], save=lambda: None)

    save = SaveShim()
    AppWindow._save_queue_state(save)
    assert save._cfg.queue_state[0]["artist"] == "אודיה"

    restored = []
    restore = SimpleNamespace(_add_track_to_queue=restored.append)
    AppWindow._restore_queue_state(restore, save._cfg.queue_state)
    assert restored[0].artist == "אודיה"

    cfg = AppConfig()
    cfg.duplicate_action = "overwrite"
    engine = SimpleNamespace(_cancel_event=threading.Event())
    controller = DownloadController(cfg, engine)
    built = []
    worker = SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(
        controller, "_build_batch_worker",
        lambda jobs, _preexisting: built.extend(jobs) or worker,
    )
    controller.start_batch([card], {
        "media_type": "audio", "quality_label": "320",
        "audio_format": "mp3", "video_format": "mp4",
        "output_dir": str(tmp_path / "out"),
    }, UrlKind.ALBUM, result.playlist_title)
    request = built[0][1]
    assert request.forced_artist == "אודיה"
    assert request.spotify_match_identity["artist"] == "אודיה"
    controller._on_track_status(str(id(card)), "starting")
    controller._on_track_status(str(id(card)), "downloading")
    assert card.artist == "אודיה"


def test_cold_match_is_single_flight_and_warm_lookup_is_local(monkeypatch):
    """Prefetch and Download may request one cold key concurrently only once."""
    from core.match_cache import MatchCache
    import core.match_cache as match_cache
    import core.scraper as scraper

    cache = MatchCache(":memory:")
    monkeypatch.setattr(match_cache, "get_match_cache", lambda: cache)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def resolve(*_args, **_kwargs):
        calls.append(time.monotonic())
        entered.set()
        release.wait(timeout=2)
        return "https://www.youtube.com/watch?v=singleflight"

    monkeypatch.setattr(scraper, "_resolve_to_ytm_url", resolve)
    track = {
        "spotify_id": "1UruS1fpkhIXklaJnttSzA",
        "title": "השם יעזור", "artist": "אודיה", "duration_sec": 190,
    }
    results = []
    owner_track = dict(track)
    waiter_track = dict(track)
    threads = [
        threading.Thread(
            target=lambda td=td: results.append(scraper.resolve_track_to_youtube(td))
        )
        for td in (owner_track, waiter_track)
    ]
    threads[0].start()
    assert entered.wait(timeout=1)
    threads[1].start()
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert results == [
        "https://www.youtube.com/watch?v=singleflight",
        "https://www.youtube.com/watch?v=singleflight",
    ]
    assert len(calls) == 1, "the cold resolver work must be shared, not duplicated"
    assert owner_track["_match_source"] == "live"
    assert waiter_track["_match_source"] == "shared"

    from core.batch_progress import BatchProgressAggregator

    aggregator = BatchProgressAggregator()
    aggregator.register("waiter")
    aggregator.mark_resolution_source("waiter", waiter_track["_match_source"])
    assert aggregator._jobs["waiter"].resolve_source == "shared"  # noqa: SLF001

    started = time.monotonic()
    assert scraper.resolve_track_to_youtube(dict(track)).endswith("singleflight")
    assert len(calls) == 1
    assert time.monotonic() - started < 0.05


def test_individual_spotify_track_prefers_localized_scoped_initial_state(monkeypatch):
    import core.scraper as scraper
    from utils.spotify_resolver import SpotifyResolver

    monkeypatch.setattr(
        SpotifyResolver, "_localized_page_html",
        lambda *_args, **_kwargs: _spotify_album_html("אודיה"),
    )
    monkeypatch.setattr(
        SpotifyResolver, "_embed_fallback",
        lambda *_args, **_kwargs: pytest.fail(
            "localized structured data should win before the Latin embed"
        ),
    )
    title, rows = scraper.scrape_spotify_track(
        "https://open.spotify.com/track/1UruS1fpkhIXklaJnttSzA",
        locale="he-IL",
    )
    assert title == "השם יעזור"
    assert rows[0]["artist"] == "אודיה"
    assert rows[0]["parent_artist"] == "אודיה"


def test_delayed_spotify_collaborator_hydration_never_leaves_trailing_comma():
    """A live album row may expose the collaborator anchor before its text."""
    from core.scraper import (
        _read_spotify_artist_credits,
        _validated_spotify_display_metadata,
    )

    class Link:
        def __init__(self, values):
            self.values = list(values)

        def inner_text(self):
            return self.values[0]

        def text_content(self):
            return self.values[0]

    primary = Link(["אודיה"])
    collaborator = Link(["", "Shir Koren"])

    class Page:
        waits = 0

        def wait_for_timeout(self, _milliseconds):
            self.waits += 1
            if len(collaborator.values) > 1:
                collaborator.values.pop(0)

    page = Page()
    credits = _read_spotify_artist_credits(
        [primary, collaborator, primary], page,
    )
    _title, artist, credits = _validated_spotify_display_metadata(
        "בוקר טוב אהבה שלי", credits,
    )

    assert credits == ["אודיה", "Shir Koren"]
    assert artist == "אודיה, Shir Koren"
    assert page.waits == 1
