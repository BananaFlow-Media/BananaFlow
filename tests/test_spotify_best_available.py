"""Production-shaped coverage for Spotify's best-available download contract."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "spotify_odeya_album.json"


def _album_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _ytm_row(track: dict) -> dict:
    candidate = track["ytm"]
    return {
        "videoId": candidate["videoId"],
        "title": candidate["title"],
        "artists": [{"name": name} for name in candidate["artists"]],
        "duration_seconds": candidate["duration_sec"],
        "album": {"name": candidate["album"]},
    }


def _install_odeya_search_fixture(monkeypatch):
    """Use real scorer boundaries with frozen production-shaped search data."""
    import core.spotify_match_scorer as scorer
    import ytmusicapi

    fixture = _album_fixture()
    general_queries: list[str] = []

    class FixtureYTMusic:
        def search(self, query, **_kwargs):
            track = next(item for item in fixture["tracks"] if item["title"] in query)
            # A karaoke result appears first to prove search order is not the answer.
            return [
                {
                    "videoId": f"karaoke{track['index']}",
                    "title": f"{track['title']} Karaoke",
                    "artists": [{"name": "Karaoke All Stars"}],
                    "duration_seconds": track["duration_sec"],
                    "album": {"name": "Karaoke Collection"},
                },
                _ytm_row(track),
            ]

    monkeypatch.setattr(ytmusicapi, "YTMusic", FixtureYTMusic)
    monkeypatch.setattr(
        scorer,
        "_search",
        lambda query, **_kwargs: general_queries.append(query) or [],
    )
    monkeypatch.setattr(scorer, "_deep_validate_urls", lambda *_a, **_k: [])
    return fixture, general_queries


def test_odeya_album_all_ten_tracks_reach_broad_fallback_and_remain_downloadable(
    monkeypatch,
):
    import core.scraper as scraper

    fixture, general_queries = _install_odeya_search_fixture(monkeypatch)
    resolved = []
    for track in fixture["tracks"]:
        resolved.append(
            scraper._resolve_to_ytm_url(
                track["title"], track["spotify_artist"], track["duration_sec"],
            )
        )

    assert len(general_queries) == 10, "strict misses must run the broader fallback"
    assert resolved == [
        f"https://music.youtube.com/watch?v={track['ytm']['videoId']}"
        for track in fixture["tracks"]
    ]
    assert all(resolved)


def test_no_discovered_candidate_returns_a_real_legacy_search_request(monkeypatch):
    import core.spotify_match_scorer as scorer
    import core.scraper as scraper
    import ytmusicapi

    class EmptyYTMusic:
        def search(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(ytmusicapi, "YTMusic", EmptyYTMusic)
    monkeypatch.setattr(scorer, "_search", lambda *_a, **_k: [])

    resolved = scraper._resolve_to_ytm_url("Valid Song", "Valid Artist", 200)

    assert resolved == "ytsearch1:Valid Artist Valid Song audio"


def test_odeya_album_all_ten_produced_jobs_reach_the_engine(monkeypatch, tmp_path):
    import core.scraper as scraper
    from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus
    from core.download_orchestrator import DownloadOrchestrator

    fixture, general_queries = _install_odeya_search_fixture(monkeypatch)

    class Engine:
        def __init__(self):
            self._cancel_event = threading.Event()
            self.urls: list[str] = []

        def download(self, request):
            assert request.url
            self.urls.append(request.url)
            request.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=request.url,
                output_path=str(tmp_path / "probe.mp3"),
                fraction=1.0,
            ))

    class Callbacks:
        def __init__(self):
            self.errors = []

        def on_track_error(self, key, error):
            self.errors.append((key, error))

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    jobs = []
    for track in fixture["tracks"]:
        identity = {
            "title": track["title"],
            "artist": track["spotify_artist"],
            "album": fixture["album_name"],
            "duration_sec": track["duration_sec"],
        }
        request = DownloadRequest(
            url=f"ytsearch1:{track['spotify_artist']} {track['title']}",
            output_dir=str(tmp_path),
            spotify_match_identity=dict(identity),
            youtube_reliability_mode="fast",
        )
        request.url_resolver = (
            lambda _cancel, td=identity: scraper._resolve_to_ytm_url(
                td["title"], td["artist"], td["duration_sec"], album=td["album"],
            )
        )
        jobs.append((str(track["index"]), request))

    engine = Engine()
    callbacks = Callbacks()
    result = DownloadOrchestrator(engine, callbacks, max_workers=3).run_batch(
        jobs, delay_range=(0.0, 0.0),
    )

    assert len(general_queries) == 10
    assert result.completed == 10
    assert result.failed == 0
    assert callbacks.errors == []
    assert len(engine.urls) == 10
    assert all(url.startswith("https://music.youtube.com/watch?v=") for url in engine.urls)


def test_low_confidence_general_ranking_prefers_official_over_bad_alternatives(
    monkeypatch,
):
    import core.spotify_match_scorer as scorer

    candidates = [
        {
            "id": "karaoke1", "title": "השם יעזור Karaoke",
            "channel": "Karaoke All Stars", "duration": 211,
        },
        {
            "id": "tribute1", "title": "השם יעזור",
            "channel": "Odeya Tribute Experience", "duration": 211,
        },
        {
            "id": "wrongone", "title": "השם יעזור",
            "channel": "Unrelated Performer", "duration": 211,
        },
        {
            "id": "official1", "title": "אודיה - השם יעזור",
            "channel": "אודיה - הערוץ הרשמי", "duration": 212,
        },
    ]
    monkeypatch.setattr(scorer, "_search", lambda *_a, **_k: candidates)
    monkeypatch.setattr(
        scorer,
        "_deep_validate_urls",
        lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("deno", 15)),
    )

    result = scorer.find_best_youtube_match(
        "השם יעזור", "Odeya", 211, allow_reasonable_fallback=True,
    )

    assert result is not None
    assert result.url.endswith("official1")
    assert result.safe is False
    assert result.breakdown["resolution_path"] == "flat_reasonable"


def test_same_script_wrong_artist_is_not_a_reasonable_direct_fallback(monkeypatch):
    import core.spotify_match_scorer as scorer

    monkeypatch.setattr(scorer, "_search", lambda *_a, **_k: [{
        "id": "wrongartist1",
        "title": "Valid Song",
        "channel": "Unrelated Performer",
        "artists": [{"name": "Unrelated Performer"}],
        "duration": 200,
    }])
    monkeypatch.setattr(scorer, "_deep_validate_urls", lambda *_a, **_k: [])

    result = scorer.find_best_youtube_match(
        "Valid Song", "Valid Artist", 200, allow_reasonable_fallback=True,
    )

    assert result is None


def test_match_search_and_deep_validation_respect_open_provider_circuit(monkeypatch):
    import core.spotify_match_scorer as scorer
    import utils.yt_dlp_opts as opts_module
    import yt_dlp

    observed: list[bool] = []

    def fake_opts(**kwargs):
        observed.append(kwargs.get("respect_po_token_circuit", True))
        return {}

    class FakeYDL:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, target, download=False):
            if target.startswith("ytsearch"):
                return {"entries": []}
            return {}

    monkeypatch.setattr(opts_module, "build_base_ydl_opts", fake_opts)
    monkeypatch.setattr(opts_module, "temp_cookies_copy", lambda _path: _NullContext())
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)

    scorer._search("ytsearch1:test", extract_flat=True, cookies_file=None)
    scorer._deep_validate_urls(
        ["https://youtube.test/video"], title="Song", artist="Artist",
        duration_sec=200, cookies_file=None,
    )

    assert observed == [True, True]


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


def test_empty_valid_lazy_resolution_is_repaired_before_engine_submission(tmp_path):
    from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus
    from core.download_orchestrator import DownloadOrchestrator

    class Engine:
        def __init__(self):
            self._cancel_event = threading.Event()
            self.urls: list[str] = []

        def download(self, request):
            self.urls.append(request.url)
            request.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED, url=request.url,
                output_path=str(tmp_path / "attempt.mp3"), fraction=1.0,
            ))

    class Callbacks:
        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    request = DownloadRequest(
        url="ytsearch1:stale placeholder", output_dir=str(tmp_path),
        spotify_match_identity={"title": "Valid Song", "artist": "Valid Artist"},
    )
    request.url_resolver = lambda _event: ""
    engine = Engine()

    result = DownloadOrchestrator(engine, Callbacks(), max_workers=1).run_batch(
        [("valid", request)], delay_range=(0.0, 0.0),
    )

    assert result.completed == 1
    assert result.failed == 0
    assert engine.urls == ["ytsearch1:Valid Artist Valid Song audio"]
