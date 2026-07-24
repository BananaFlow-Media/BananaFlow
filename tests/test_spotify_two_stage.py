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
import time

import pytest

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
from core.download_orchestrator import DownloadOrchestrator


# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — metadata-only emit
# ──────────────────────────────────────────────────────────────────────────────

class TestEmitPendingTrack:
    def test_tags_pending_and_adds_placeholder_url(self):
        from core.scraper import _emit_pending_track
        fired = []
        for td in (
            {"title": "Song A", "artist": "Artist"},
            {"title": "Song B", "artist": "Artist", "url": ""},
        ):
            _emit_pending_track(td, on_item=fired.append)

        assert len(fired) == 2
        for it in fired:
            assert it["url"].startswith("ytsearch1:")
            assert it["match_status"] == "pending"

    def test_keeps_existing_nonempty_url(self):
        from core.scraper import _emit_pending_track
        td = {"title": "S", "artist": "A", "url": "ytsearch1:preset audio"}
        _emit_pending_track(td, on_item=None)
        assert td["url"] == "ytsearch1:preset audio"
        assert td["match_status"] == "pending"


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

# ──────────────────────────────────────────────────────────────────────────────
# Conservative-mode serialization for lazy-resolver (Spotify) downloads
# ──────────────────────────────────────────────────────────────────────────────

class _ConcurrencyEngine:
    """Fake engine that records the peak number of *simultaneous* downloads."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0
        self.downloaded_urls: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
            self.downloaded_urls.append(req.url)
        time.sleep(0.05)  # hold the "download" so real overlap would be visible
        with self._lock:
            self._current -= 1
        if req.on_finished:
            req.on_finished(
                DownloadProgress(status=DownloadStatus.FINISHED, url=req.url, output_path="/tmp/o.mp3")
            )


class TestConservativeSerialization:
    def test_lazy_resolver_youtube_downloads_are_serialized(self, monkeypatch):
        # Skip the 5-10s inter-job cooldown so the test is fast; the semaphore
        # (CONSERVATIVE_MAX_PARALLEL_YOUTUBE == 1) is what enforces serial order.
        monkeypatch.setattr(
            DownloadOrchestrator, "_youtube_cooldown", lambda self, ev, key: None
        )

        engine = _ConcurrencyEngine()
        # max_workers=4: without the gate the resolved YouTube downloads would
        # overlap. Matching (the resolver) may still run in parallel.
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=4)

        def make_job(i):
            req = _req(f"placeholder://{i}")
            req.youtube_reliability_mode = "conservative"
            req.url_resolver = lambda ev, _i=i: f"https://www.youtube.com/watch?v=VID{_i}"
            return (f"k{i}", req)

        jobs = [make_job(i) for i in range(3)]
        orch.run_batch(jobs)

        # Despite max_workers=4, conservative mode serialized the downloads.
        assert engine.max_concurrent == 1
        # Each job downloaded the exact URL its resolver produced (the gate saw
        # them as YouTube jobs). Match the full prefix, not a bare "youtube.com"
        # substring, to avoid the incomplete-URL-sanitization antipattern.
        assert sorted(engine.downloaded_urls) == [
            f"https://www.youtube.com/watch?v=VID{i}" for i in range(3)
        ]

    def test_fast_mode_lazy_downloads_are_not_serialized(self, monkeypatch):
        monkeypatch.setattr(
            DownloadOrchestrator, "_youtube_cooldown", lambda self, ev, key: None
        )
        engine = _ConcurrencyEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=4)

        def make_job(i):
            req = _req(f"placeholder://{i}")
            req.youtube_reliability_mode = "fast"  # not conservative → no gate
            req.url_resolver = lambda ev, _i=i: f"https://www.youtube.com/watch?v=VID{_i}"
            return (f"k{i}", req)

        orch.run_batch([make_job(i) for i in range(3)])
        # Fast mode allows real parallelism (control for the test above).
        assert engine.max_concurrent > 1


# ──────────────────────────────────────────────────────────────────────────────
# Progressive stage-1 emit — a fake Playwright page drives the scroll scrape
# ──────────────────────────────────────────────────────────────────────────────

class _Loc:
    """Tolerant fake of a Playwright Locator: every method returns a safe
    default so the scraper never crashes on an unmodelled call."""

    def __init__(self, *, count=0, text="", attrs=None, children=None,
                 items=None, items_seq=None):
        self._count = count
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items
        self._items_seq = items_seq  # list of lists; one popped per .all()

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def all(self):
        if self._items_seq is not None:
            return self._items_seq.pop(0) if self._items_seq else []
        return self._items if self._items is not None else []

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def locator(self, sel):
        for key, child in self._children.items():
            if key in sel:
                return child
        return _Loc()

    def filter(self, **kwargs):
        return _Loc(count=0)

    def scroll_into_view_if_needed(self):
        pass

    def evaluate(self, *a, **k):
        return self._attrs.get("__eval__", "")


def _row(idx, title, events):
    """A fake tracklist row that appends an event when its title is read (a
    proxy for 'this track was scraped in this pass')."""
    link = _Loc(count=1, attrs={"href": f"/track/id{idx}"},
                children={"div": _Loc(count=1, text=title)})
    return _Loc(
        count=1,
        attrs={"aria-rowindex": str(idx)},
        children={
            "a[data-testid='internal-track-link']": link,
            "a[href*='/artist/']": _Loc(items=[_Loc(count=1, text="Artist")]),
            "img": _Loc(count=1, attrs={"src": "http://img/x"}),
            "div, span": _Loc(count=0),
            "div[dir='auto']": _Loc(count=1, text=title),
        },
    )


class _FakePage:
    def __init__(self, passes, events):
        self._events = events
        grid = _Loc(children={
            "div[data-testid='tracklist-row']": _Loc(items_seq=list(passes)),
        })
        self._grid = grid

    def goto(self, *a, **k):
        pass

    def content(self):
        return ""  # no embedded JSON → forces the scroll path

    def wait_for_selector(self, *a, **k):
        pass

    def wait_for_timeout(self, *a, **k):
        self._events.append("scroll")

    def evaluate(self, *a, **k):
        return "Test Catalog"

    def locator(self, sel):
        if "entity-image" in sel or "main img" in sel:
            return _Loc(count=0)  # skip header thumbnail
        if "role='grid'" in sel or "track-list" in sel:
            return self._grid
        return _Loc()


class TestProgressiveScrapeEmit:
    def test_first_item_is_emitted_before_the_scrape_finishes(self, monkeypatch):
        """In metadata_only mode the grid scraper must publish each track from
        inside the scroll loop — so the first track is delivered before later
        tracks are even scraped, not all at once after the whole scroll."""
        from core import scraper

        events: list[str] = []

        # Real _emit_pending_track, but record the emission order in `events`.
        real_emit = scraper._emit_pending_track

        def recording_emit(track_dict, on_item=None):
            def _rec(td):
                events.append(f"emit:{td['title']}")
                sink.append(td)
            real_emit(track_dict, _rec)

        sink: list[dict] = []
        monkeypatch.setattr(scraper, "_emit_pending_track", recording_emit)

        # Pass 1 yields Song 1; pass 2 yields Song 1 (dup) + Song 2; pass 3 empty.
        passes = [
            [_row(1, "Song 1", events)],
            [_row(1, "Song 1", events), _row(2, "Song 2", events)],
            [],
        ]
        page = _FakePage(passes, events)

        title, items = scraper._scrape_spotify_grid_on_page(
            page, "https://open.spotify.com/album/x", "Album",
            on_item=lambda td: None, metadata_only=True,
        )

        # Both tracks emitted, tagged pending, with a ytsearch placeholder URL.
        assert [d["title"] for d in sink] == ["Song 1", "Song 2"]
        assert all(d["match_status"] == "pending" for d in sink)
        assert all(d["url"].startswith("ytsearch1:") for d in sink)

        # Interleaving: Song 1 was emitted, then a scroll happened, then Song 2
        # was emitted — proving emission occurs during the scrape, not after it.
        assert "emit:Song 1" in events and "emit:Song 2" in events
        i1 = events.index("emit:Song 1")
        i2 = events.index("emit:Song 2")
        assert i1 < i2
        assert "scroll" in events[i1:i2]


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
