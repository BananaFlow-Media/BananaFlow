"""
tests/test_download_fast_start.py
=================================
PR3 tests for the two-stage Spotify *download* fast-start:

* ``core.match_prefetcher.MatchPrefetcher`` warms plugins once and pre-resolves
  only the leading N tracks (never the whole catalog), via the same resolve
  path (unchanged match quality), and is cancellable.
* ``DownloadOrchestrator._should_stagger`` skips the inter-start stagger for the
  opening pool-fill wave and applies it only to later jobs.
* the orchestrator resolves each track's URL the instant before its own
  download — the first download starts without pre-matching the whole catalog.

Offline — no network, no Qt, no real yt-dlp (fakes stand in for the resolver
and the download engine).
"""

from __future__ import annotations

import threading
import time

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
from core.download_orchestrator import DownloadOrchestrator


# ──────────────────────────────────────────────────────────────────────────────
# MatchPrefetcher — bounded, quality-preserving, cancellable fast-start
# ──────────────────────────────────────────────────────────────────────────────

class TestMatchPrefetcher:
    def test_pre_resolves_only_the_first_n_and_warms_plugins_once(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        warm_calls = {"n": 0}
        monkeypatch.setattr(
            "core.runtime_components.warm_up_plugins",
            lambda: warm_calls.__setitem__("n", warm_calls["n"] + 1),
        )

        resolved_lock = threading.Lock()
        resolved: list[str] = []

        def fake_resolve(td, cookies_file=None, cancel_check=None):
            with resolved_lock:
                resolved.append(td["title"])
            return "https://music.youtube.com/watch?v=X"

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", fake_resolve)

        tracks = [{"title": f"t{i}", "artist": "A", "spotify_id": f"id{i}"} for i in range(20)]
        pf = MatchPrefetcher(limit=8)
        pf.start(tracks)
        pf._thread.join(timeout=5)  # noqa: SLF001 - deterministic wait in test
        assert pf._thread is not None and not pf._thread.is_alive()  # noqa: SLF001

        # Exactly the first 8 titles were resolved — the catalog was NOT
        # pre-matched in full, and nothing past the window was touched.
        assert set(resolved) == {f"t{i}" for i in range(8)}
        assert len(resolved) == 8
        # Plugins were warmed exactly once, off the click path.
        assert warm_calls["n"] == 1

    def test_empty_or_no_pending_is_a_noop(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        monkeypatch.setattr("core.runtime_components.warm_up_plugins", lambda: None)
        called = {"n": 0}

        def fake_resolve(td, cookies_file=None, cancel_check=None):
            called["n"] += 1
            return ""

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", fake_resolve)

        pf = MatchPrefetcher(limit=8)
        pf.start([])
        pf._thread.join(timeout=5)  # noqa: SLF001
        assert called["n"] == 0

    def test_cancel_stops_further_resolves(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        monkeypatch.setattr("core.runtime_components.warm_up_plugins", lambda: None)

        gate = threading.Event()
        seen_lock = threading.Lock()
        seen: list[str] = []

        def fake_resolve(td, cookies_file=None, cancel_check=None):
            with seen_lock:
                seen.append(td["title"])
            gate.wait(timeout=2)  # hold so cancel can land mid-pass
            return ""

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", fake_resolve)

        tracks = [{"title": f"t{i}", "artist": "A", "spotify_id": f"id{i}"} for i in range(8)]
        # Single worker so resolves are strictly sequential and cancel is observable.
        pf = MatchPrefetcher(limit=8, max_workers=1)
        pf.start(tracks)

        # Wait until the first resolve is in flight, then cancel and release.
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        pf.cancel()
        gate.set()
        pf._thread.join(timeout=5)  # noqa: SLF001

        # Cancel landed: not every track was resolved.
        assert 0 < len(seen) < 8


# ──────────────────────────────────────────────────────────────────────────────
# Opening-wave stagger skip
# ──────────────────────────────────────────────────────────────────────────────

class TestShouldStagger:
    def test_opening_wave_is_not_staggered_later_jobs_are(self):
        n_workers = 3
        # The first pool-fill (indices 0..n_workers-1) fills immediately.
        assert not DownloadOrchestrator._should_stagger(0, n_workers)
        assert not DownloadOrchestrator._should_stagger(1, n_workers)
        assert not DownloadOrchestrator._should_stagger(2, n_workers)
        # Everything past the opening wave is staggered.
        assert DownloadOrchestrator._should_stagger(3, n_workers)
        assert DownloadOrchestrator._should_stagger(4, n_workers)

    def test_batch_smaller_than_pool_never_staggers(self):
        # 2 jobs, 3 workers: both are in the opening wave.
        assert not DownloadOrchestrator._should_stagger(0, 3)
        assert not DownloadOrchestrator._should_stagger(1, 3)


# ──────────────────────────────────────────────────────────────────────────────
# First download starts without pre-matching the whole catalog
# ──────────────────────────────────────────────────────────────────────────────

def _req(url: str = "placeholder") -> DownloadRequest:
    return DownloadRequest(url=url, output_dir=".", media_type=MediaType.AUDIO)


class _NullCallbacks:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _OrderEngine:
    """Fake engine that records each download in a shared event log so the
    resolve/download interleaving is observable."""

    def __init__(self, log: list) -> None:
        self._cancel_event = threading.Event()
        self._log = log

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        i = int(req.url.rsplit("VID", 1)[-1])
        self._log.append(("download", i))
        if req.on_finished:
            req.on_finished(
                DownloadProgress(
                    status=DownloadStatus.FINISHED, url=req.url, output_path="/tmp/o.mp3"
                )
            )


class TestLazyResolvePipelining:
    def test_first_download_precedes_later_resolves(self, monkeypatch):
        # Drop the conservative cooldown so the test is fast; the serial pool
        # (max_workers=1) is what makes the interleaving deterministic.
        monkeypatch.setattr(
            DownloadOrchestrator, "_youtube_cooldown", lambda self, ev, key: None
        )

        log: list[tuple[str, int]] = []
        engine = _OrderEngine(log)
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)

        def make_job(i):
            req = _req(f"placeholder://{i}")

            def resolver(ev, _i=i):
                log.append(("resolve", _i))
                return f"https://www.youtube.com/watch?v=VID{_i}"

            req.url_resolver = resolver
            return (f"k{i}", req)

        n = 4
        orch.run_batch([make_job(i) for i in range(n)])

        # Each track is resolved immediately before its own download — the URL
        # is resolved the instant before it downloads, never all upfront.
        assert log == [phase for i in range(n) for phase in (("resolve", i), ("download", i))]
        # The crisp property: the FIRST download happens before the SECOND
        # track is even matched — no whole-catalog pre-match gate.
        assert log.index(("download", 0)) < log.index(("resolve", 1))
