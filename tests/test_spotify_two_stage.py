"""
tests/test_spotify_two_stage.py
===============================
PR2 tests for the two-stage Spotify import:

* stage-1 metadata-only emit (`core.scraper._emit_metadata_only`),
* `TrackMeta` deferred-match fields,
* the lazy `DownloadRequest.url_resolver` hook and its execution inside
  `DownloadOrchestrator` (URL resolved the instant before download, honoring
  cancellation),
* the intra-resolve cancel check in `resolve_track_to_youtube`.

Offline — no network, no Qt, no real yt-dlp downloads (a fake engine stands
in for `DownloadEngine`).
"""

from __future__ import annotations

import threading

import pytest

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
from core.download_orchestrator import DownloadOrchestrator


# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — metadata-only emit
# ──────────────────────────────────────────────────────────────────────────────

class TestEmitMetadataOnly:
    def test_tags_pending_and_adds_placeholder_url(self):
        from core.scraper import _emit_metadata_only
        fired = []
        items = [
            {"title": "Song A", "artist": "Artist"},
            {"title": "Song B", "artist": "Artist", "url": ""},
        ]
        _emit_metadata_only(items, on_item=fired.append)

        assert len(fired) == 2
        for it in items:
            assert it["url"].startswith("ytsearch1:")
            assert it["match_status"] == "pending"

    def test_keeps_existing_nonempty_url(self):
        from core.scraper import _emit_metadata_only
        items = [{"title": "S", "artist": "A", "url": "ytsearch1:preset audio"}]
        _emit_metadata_only(items, on_item=None)
        assert items[0]["url"] == "ytsearch1:preset audio"
        assert items[0]["match_status"] == "pending"


class TestTrackMetaFields:
    def test_defaults_to_matched(self):
        from core.playlist_parser import TrackMeta
        tm = TrackMeta(title="x")
        assert tm.match_status == "matched"
        assert tm.spotify_id == ""
        assert tm.spotify_key_kind == "spotify_id"

    def test_pending_fields_round_trip(self):
        from core.playlist_parser import TrackMeta
        tm = TrackMeta(title="y", match_status="pending", spotify_id="sid", spotify_key_kind="composite")
        assert tm.match_status == "pending"
        assert tm.spotify_id == "sid"
        assert tm.spotify_key_kind == "composite"


# ──────────────────────────────────────────────────────────────────────────────
# Lazy URL resolver — field + orchestrator execution
# ──────────────────────────────────────────────────────────────────────────────

def _req(url: str = "placeholder") -> DownloadRequest:
    return DownloadRequest(url=url, output_dir=".", media_type=MediaType.AUDIO)


class _FakeEngine:
    """Minimal stand-in for DownloadEngine: records the URL it was asked to
    download and reports immediate success."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self.downloaded_urls: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        self.downloaded_urls.append(req.url)
        if req.on_finished:
            req.on_finished(
                DownloadProgress(
                    status=DownloadStatus.FINISHED,
                    url=req.url,
                    output_path="/tmp/out.mp3",
                )
            )


class _NullCallbacks:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class TestDownloadRequestField:
    def test_defaults_none_and_is_callable(self):
        r = _req()
        assert r.url_resolver is None
        r.url_resolver = lambda ev: "https://music.youtube.com/watch?v=X"
        assert r.url_resolver(None) == "https://music.youtube.com/watch?v=X"


class TestOrchestratorLazyResolve:
    def test_resolver_result_is_what_gets_downloaded(self):
        engine = _FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)

        seen_cancel_ev = {}

        def resolver(ev):
            seen_cancel_ev["ev"] = ev
            return "https://music.youtube.com/watch?v=RESOLVED"

        req = _req("placeholder://unresolved")
        req.url_resolver = resolver

        orch.run_batch([("k1", req)])

        # The engine downloaded the RESOLVED url, never the placeholder.
        assert engine.downloaded_urls == ["https://music.youtube.com/watch?v=RESOLVED"]
        assert req.url == "https://music.youtube.com/watch?v=RESOLVED"
        # The resolver received the per-request cancel Event (not None).
        assert isinstance(seen_cancel_ev.get("ev"), threading.Event)

    def test_cancel_before_resolve_skips_download(self):
        engine = _FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)

        called = {"n": 0}

        def resolver(ev):
            called["n"] += 1
            return "https://music.youtube.com/watch?v=RESOLVED"

        req = _req()
        req.url_resolver = resolver

        # Pre-cancel: run_batch short-circuits every job as cancelled before
        # any worker runs, so the resolver never fires and nothing downloads.
        engine._cancel_event.set()
        orch.run_batch([("k1", req)])

        assert engine.downloaded_urls == []
        assert called["n"] == 0

    def test_resolver_exception_does_not_sink_the_batch(self):
        engine = _FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)

        def bad_resolver(ev):
            raise RuntimeError("resolve boom")

        req = _req("placeholder")
        req.url_resolver = bad_resolver

        # Should not raise; the job proceeds with whatever url it had.
        orch.run_batch([("k1", req)])
        assert engine.downloaded_urls == ["placeholder"]


# ──────────────────────────────────────────────────────────────────────────────
# Intra-resolve cancellation
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveCancel:
    def test_cancel_returns_last_resort_before_ytdlp_fallback(self, monkeypatch):
        import core.scraper as scraper

        # Make the cheap YTM path yield nothing so control reaches the cancel
        # gate that guards the heavier yt-dlp fallback.
        def no_ytm(*a, **k):
            raise RuntimeError("no ytmusicapi")

        # find_best_youtube_match must NOT be called once we cancel.
        called = {"fallback": 0}

        def fallback_spy(*a, **k):
            called["fallback"] += 1
            return None

        monkeypatch.setattr("ytmusicapi.YTMusic", no_ytm, raising=False)
        monkeypatch.setattr(
            "core.spotify_match_scorer.find_best_youtube_match", fallback_spy
        )

        url = scraper._resolve_to_ytm_url(
            "Song", "Artist", 200, cancel_check=lambda: True
        )
        assert url.startswith("ytsearch1:")
        assert called["fallback"] == 0  # cancelled before the heavy fallback
