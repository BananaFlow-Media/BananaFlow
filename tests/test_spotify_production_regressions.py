"""Production-derived regressions for Spotify track import and queue safety."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.match_errors import SpotifyMetadataInvalid
from utils.spotify_resolver import (
    SpotifyResolver,
    normalise_spotify_artist_credits,
    parse_spotify_embed_track_html,
)


FIXTURE = Path(__file__).parent / "fixtures" / "spotify_track_embed_responses.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _embed_html(track_id: str) -> str:
    entity = _fixture()["tracks"][track_id]["entity"]
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></html>"
    )


@pytest.mark.parametrize(
    ("track_id", "title", "artists", "duration"),
    [
        (
            "4CF4nWNKzvRsFN542wXLyX",
            "באת לי פתאום",
            ["Keren Peles", "Roni Alter"],
            197,
        ),
        (
            "2VxeLyX666F8uXCJ0dZF8B",
            "Shallow",
            ["Lady Gaga", "Bradley Cooper"],
            215,
        ),
    ],
)
def test_frozen_production_embed_extracts_only_track_credits(
    track_id, title, artists, duration,
):
    metadata = parse_spotify_embed_track_html(_embed_html(track_id), track_id)
    assert metadata["title"] == title
    assert metadata["artist_credits"] == artists
    assert metadata["artist"] == ", ".join(artists)
    assert metadata["duration_sec"] == duration
    assert not any(
        phrase.casefold() in metadata["artist"].casefold()
        for phrase in ("Show all", "Popular Releases", "Popular Albums", "Popular Singles")
    )
    assert "משי קליינשטיין" not in metadata["artist"]


def test_repeated_credits_are_deduplicated_but_polluted_page_scope_is_rejected():
    assert normalise_spotify_artist_credits(
        ["Lady Gaga", "Lady Gaga", "Bradley Cooper", "", " , "]
    ) == ["Lady Gaga", "Bradley Cooper"]
    polluted = _fixture()["tracks"]["2VxeLyX666F8uXCJ0dZF8B"][
        "polluted_full_page_artist_links"
    ]
    with pytest.raises(SpotifyMetadataInvalid, match="page UI text"):
        normalise_spotify_artist_credits(polluted)


def test_malformed_structured_track_is_explicitly_rejected():
    payload = _fixture()["tracks"]["2VxeLyX666F8uXCJ0dZF8B"]["entity"] | {
        "artists": [{"name": "Show all"}, {"name": "Popular Albums by Lady Gaga"}],
    }
    page = {"props": {"pageProps": {"state": {"data": {"entity": payload}}}}}
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(page) + "</script>"
    with pytest.raises(SpotifyMetadataInvalid):
        parse_spotify_embed_track_html(html, "2VxeLyX666F8uXCJ0dZF8B")


def test_embed_parser_handles_spaced_script_end_tag_without_consuming_page_content():
    track_id = "2VxeLyX666F8uXCJ0dZF8B"
    html = _embed_html(track_id).replace(
        "</script>",
        "</script   ><script>window.UNRELATED = 'Popular Albums';</script>",
    )
    metadata = parse_spotify_embed_track_html(html, track_id)
    assert metadata["title"] == "Shallow"
    assert metadata["artist_credits"] == ["Lady Gaga", "Bradley Cooper"]


def test_individual_track_production_path_emits_valid_metadata_as_pending(monkeypatch):
    track_id = "2VxeLyX666F8uXCJ0dZF8B"
    metadata = parse_spotify_embed_track_html(_embed_html(track_id), track_id)
    row = SpotifyResolver._make_dict(
        metadata["title"], metadata["artist"], metadata["duration_sec"] * 1000,
        "", metadata["spotify_url"],
    )
    row.update(spotify_id=track_id, artist_credits=metadata["artist_credits"])
    monkeypatch.setattr(SpotifyResolver, "_embed_fallback", lambda *_a, **_k: [dict(row)])

    from core.scraper import scrape_spotify_track

    emitted = []
    title, items = scrape_spotify_track(
        f"https://open.spotify.com/track/{track_id}", on_item=emitted.append,
    )
    assert title == "Shallow"
    assert items[0]["artist"] == "Lady Gaga, Bradley Cooper"
    assert emitted[0]["match_status"] == "pending"
    assert emitted[0]["url"].startswith("ytsearch1:")


def test_inconclusive_strict_path_invokes_general_fallback_and_ranks_real_candidates(
    monkeypatch,
):
    import core.spotify_match_scorer as scorer
    import core.scraper as scraper
    import ytmusicapi

    class _YTM:
        def search(self, *_args, **_kwargs):
            return [
                {
                    "videoId": "karaoke0",
                    "title": "Shallow Karaoke",
                    "artists": [{"name": "Karaoke All Stars"}],
                    "duration_seconds": 215,
                },
                {
                    "videoId": "cover000",
                    "title": "Lady Gaga & Bradley Cooper - Shallow",
                    "artists": [{"name": "Acoustic Sessions"}],
                    "duration_seconds": 216,
                },
            ]

    general_candidates = [
        {
            "id": "karaoke1",
            "title": "Shallow Karaoke",
            "channel": "Karaoke All Stars",
            "artists": [{"name": "Karaoke All Stars"}],
            "duration": 215,
        },
        {
            "id": "tribute1",
            "title": "Lady Gaga & Bradley Cooper - Shallow",
            "channel": "Tribute Stage",
            "artists": [{"name": "Tribute Stage"}],
            "duration": 215,
        },
        {
            "id": "official1",
            "title": "Shallow",
            "channel": "Lady Gaga",
            "artists": [{"name": "Lady Gaga"}, {"name": "Bradley Cooper"}],
            "duration": 215,
        },
        {
            "id": "fanwrong",
            "title": "Shallow - Lady Gaga Bradley Cooper",
            "channel": "Fan Uploads",
            "artists": [{"name": "Fan Uploads"}],
            "duration": 215,
        },
    ]
    calls = []
    monkeypatch.setattr(ytmusicapi, "YTMusic", _YTM)
    monkeypatch.setattr(
        scorer, "_search",
        lambda *_a, **_k: calls.append("general") or list(general_candidates),
    )
    monkeypatch.setattr(scorer, "_deep_validate_urls", lambda *_a, **_k: [])

    resolved = scraper._resolve_to_ytm_url(
        "Shallow", "Lady Gaga, Bradley Cooper", 215,
    )
    assert calls == ["general"]
    assert resolved == "https://www.youtube.com/watch?v=official1"


def test_safe_low_confidence_structured_candidate_remains_downloadable(monkeypatch):
    import core.spotify_match_scorer as scorer
    import core.scraper as scraper
    import ytmusicapi

    class _YTM:
        def search(self, *_args, **_kwargs):
            return [{
                "videoId": "safe-low",
                # Frozen-style partial metadata: a minor title typo and no
                # duration, but exact structured performer credits.
                "title": "Shalxo",
                "artists": [{"name": "Lady Gaga"}, {"name": "Bradley Cooper"}],
            }]

    general_calls = []
    monkeypatch.setattr(ytmusicapi, "YTMusic", _YTM)
    monkeypatch.setattr(
        scorer, "_search", lambda *_a, **_k: general_calls.append(1) or [],
    )
    resolved = scraper._resolve_to_ytm_url(
        "Shallow", "Lady Gaga, Bradley Cooper", 215,
    )
    assert general_calls == [1]
    assert resolved == "https://music.youtube.com/watch?v=safe-low"


def test_unresolved_spotify_job_never_reaches_engine_and_does_not_block_resolved_peer(
    tmp_path,
):
    from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
    from core.download_orchestrator import DownloadOrchestrator

    class _Engine:
        def __init__(self):
            self._cancel_event = threading.Event()
            self.urls = []

        def download(self, request):
            assert request.url
            self.urls.append(request.url)
            request.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=request.url,
                output_path=str(tmp_path / "ok.mp3"),
                fraction=1.0,
            ))

    class _Callbacks:
        def __init__(self):
            self.errors = []

        def on_track_error(self, key, error):
            self.errors.append((key, error))

        def __getattr__(self, name):
            if name.startswith("on_"):
                return lambda *_a, **_k: None
            raise AttributeError(name)

    engine = _Engine()
    callbacks = _Callbacks()
    resolved = DownloadRequest(
        url="https://www.youtube.com/watch?v=official1",
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
    )
    unresolved = DownloadRequest(
        url="ytsearch1:unresolved spotify track",
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
    )
    unresolved.spotify_match_identity = {"title": "Song", "artist": "Artist"}
    unresolved.url_resolver = lambda _cancel: ""
    result = DownloadOrchestrator(
        engine, callbacks, max_workers=2,
    ).run_batch(
        [("resolved", resolved), ("unresolved", unresolved)],
        delay_range=(0.0, 0.0),
    )
    assert result.completed == 1
    assert result.failed == 1
    assert engine.urls == ["https://www.youtube.com/watch?v=official1"]
    assert callbacks.errors[0][0] == "unresolved"
    assert callbacks.errors[0][1].message_key == "err_safe_match_not_found"


pytestmark_qt = pytest.mark.skipif(os.name != "nt", reason="Qt queue regression is Windows-only")


@pytestmark_qt
def test_mixed_queue_excludes_invalid_rows_and_never_builds_empty_url_jobs(
    tmp_path, monkeypatch,
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import AppConfig
    from ui.components.track_card import TrackCard
    from ui.controllers.download_controller import DownloadController

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = AppConfig()
    cfg.duplicate_action = "overwrite"

    class _Engine:
        def __init__(self):
            self._cancel_event = threading.Event()

    class _Worker:
        def __init__(self):
            self.started = False

        def start(self):
            self.started = True

    ctrl = DownloadController(cfg, _Engine())
    built = []
    worker = _Worker()
    monkeypatch.setattr(
        ctrl, "_build_batch_worker",
        lambda jobs, preexisting: built.extend(jobs) or worker,
    )
    valid = TrackCard(
        "Shallow", "Lady Gaga, Bradley Cooper", platform="spotify",
        track_url="https://music.youtube.com/watch?v=official1",
    )
    invalid = TrackCard(
        "Broken Spotify row", platform="spotify", track_url="",
        match_status="metadata_invalid",
        resolution_error="spotify_metadata_invalid_card",
    )
    opts = {
        "media_type": "audio",
        "quality_label": "320",
        "audio_format": "mp3",
        "video_format": "mp4",
        "output_dir": str(tmp_path / "out"),
    }
    ctrl.start_batch([valid, invalid], opts, None, "")
    assert worker.started
    assert len(built) == 1
    assert built[0][1].url == "https://music.youtube.com/watch?v=official1"
    assert all(req.url for _key, req in built)
    assert invalid.match_status == "metadata_invalid"
    assert not invalid.is_selected()

    # A second click containing only the invalid row cannot submit it.
    built.clear()
    ctrl.start_batch([invalid], opts, None, "")
    assert built == []
    app.processEvents()
