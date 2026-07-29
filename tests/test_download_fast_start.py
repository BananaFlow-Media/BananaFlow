"""
tests/test_download_fast_start.py
=================================
PR3 tests for the two-stage Spotify *download* fast-start:

* ``core.match_prefetcher.MatchPrefetcher`` warms plugins once and pre-resolves
  only the leading N tracks (never the whole catalog), via the same resolve
  path (unchanged match quality), and is cancellable.
* Spotify pipeline downloads preserve the user's Fast-mode inter-start delay
  at actual engine starts, while resolver work stays parallel.
* the orchestrator resolves each track's URL the instant before its own
  download — the first download starts without pre-matching the whole catalog.

Offline — no network, no Qt, no real yt-dlp (fakes stand in for the resolver
and the download engine).
"""

from __future__ import annotations

import tempfile
import threading
import time

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
from core.download_orchestrator import DownloadOrchestrator


# ──────────────────────────────────────────────────────────────────────────────
# MatchPrefetcher — bounded, quality-preserving, cancellable fast-start
# ──────────────────────────────────────────────────────────────────────────────

class TestMatchPrefetcher:
    def test_prefetch_provenance_uses_the_canonical_match_cache_key(self):
        from core.match_prefetcher import (
            clear_prefetched_matches,
            mark_prefetched_match,
            was_prefetched_match,
        )

        clear_prefetched_matches()
        track = {
            "spotify_url": "https://open.spotify.com/track/canonical123?si=test",
            "title": "Song",
            "artist": "Artist",
            "duration_sec": 200,
        }
        mark_prefetched_match(track)
        assert was_prefetched_match("canonical123")
        assert not was_prefetched_match(track["spotify_url"])
        clear_prefetched_matches()

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

    def test_warm_up_async_warms_plugins_off_thread(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        warmed = threading.Event()
        monkeypatch.setattr(
            "core.runtime_components.warm_up_plugins", lambda: warmed.set()
        )
        MatchPrefetcher().warm_up_async()
        assert warmed.wait(timeout=5)

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

    def test_shutdown_joins_cancel_aware_provider_wait(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        monkeypatch.setattr("core.runtime_components.warm_up_plugins", lambda: None)
        entered = threading.Event()
        exited = threading.Event()

        def waiting_resolver(td, cookies_file=None, cancel_check=None):
            entered.set()
            while not cancel_check():
                time.sleep(0.01)
            exited.set()
            return ""

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", waiting_resolver)
        pf = MatchPrefetcher(limit=1, max_workers=1)
        pf.start([{"title": "provider wait", "artist": "A", "spotify_id": "wait"}])
        assert entered.wait(timeout=1)
        try:
            assert pf.shutdown(timeout_s=1.0) is True
            assert exited.is_set()
            assert pf._thread is None or not pf._thread.is_alive()  # noqa: SLF001
        finally:
            pf.cancel()
            if pf._thread is not None:  # noqa: SLF001
                pf._thread.join(timeout=2)  # noqa: SLF001

    def test_shutdown_timeout_is_bounded_and_remains_joinable(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        monkeypatch.setattr("core.runtime_components.warm_up_plugins", lambda: None)
        entered = threading.Event()
        release = threading.Event()

        def blocked_provider(td, cookies_file=None, cancel_check=None):
            entered.set()
            release.wait(timeout=2)
            return ""

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", blocked_provider)
        pf = MatchPrefetcher(limit=1, max_workers=1)
        pf.start([{"title": "blocked", "artist": "A", "spotify_id": "blocked"}])
        assert entered.wait(timeout=1)

        try:
            started = time.monotonic()
            assert pf.shutdown(timeout_s=0.05) is False
            assert time.monotonic() - started < 0.5
            release.set()
            assert pf.shutdown(timeout_s=1.0) is True
        finally:
            release.set()
            pf.cancel()
            if pf._thread is not None:  # noqa: SLF001
                pf._thread.join(timeout=2)  # noqa: SLF001

    def test_shutdown_retains_ownership_of_a_superseded_provider_call(self, monkeypatch):
        from core.match_prefetcher import MatchPrefetcher

        monkeypatch.setattr("core.runtime_components.warm_up_plugins", lambda: None)
        first_entered = threading.Event()
        first_release = threading.Event()

        def resolver(td, cookies_file=None, cancel_check=None):
            if td["spotify_id"] == "first":
                first_entered.set()
                first_release.wait(timeout=2)
            return ""

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", resolver)
        pf = MatchPrefetcher(limit=1, max_workers=1)
        pf.start([{"title": "first", "artist": "A", "spotify_id": "first"}])
        assert first_entered.wait(timeout=1)
        pf.start([{"title": "second", "artist": "A", "spotify_id": "second"}])
        try:
            assert pf.shutdown(timeout_s=0.05) is False
            first_release.set()
            assert pf.shutdown(timeout_s=1.0) is True
        finally:
            first_release.set()
            pf.cancel()


# ──────────────────────────────────────────────────────────────────────────────
# Direct-only legacy stagger
# ──────────────────────────────────────────────────────────────────────────────

class TestShouldStagger:
    def test_direct_only_batch_keeps_original_stagger(self):
        assert not DownloadOrchestrator._should_stagger(0)
        assert DownloadOrchestrator._should_stagger(1)
        assert DownloadOrchestrator._should_stagger(5)


# ──────────────────────────────────────────────────────────────────────────────
# First download starts without pre-matching the whole catalog
# ──────────────────────────────────────────────────────────────────────────────

def _req(url: str = "placeholder") -> DownloadRequest:
    # The real OS temp dir, not "." — run_batch() now creates a real batch
    # workspace under output_dir (see utils.paths.make_batch_workspace), and
    # "." would have meant the repo's own working directory.
    return DownloadRequest(url=url, output_dir=tempfile.gettempdir(), media_type=MediaType.AUDIO)


class _NullCallbacks:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _OrderEngine:
    """Fake engine that records each download in a shared event log so the
    resolve/download interleaving is observable."""

    def __init__(self, log: list, hold_s: float = 0.0) -> None:
        self._cancel_event = threading.Event()
        self._log = log
        self._hold_s = hold_s

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        i = int(req.url.rsplit("VID", 1)[-1])
        self._log.append(("download", i))
        if self._hold_s:
            time.sleep(self._hold_s)
        if req.on_finished:
            req.on_finished(
                DownloadProgress(
                    status=DownloadStatus.FINISHED, url=req.url, output_path="/tmp/o.mp3"
                )
            )


class TestLazyResolvePipelining:
    def test_fast_pipeline_staggers_real_engine_starts(self):
        starts: list[float] = []

        class Engine(_PlainEngine):
            def download(self, req):
                starts.append(time.monotonic())
                super().download(req)

        engine = Engine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=3)

        def make_job(i):
            req = _req(f"placeholder://{i}")
            req.youtube_reliability_mode = "fast"
            req.url_resolver = lambda _ev, _i=i: f"https://www.youtube.com/watch?v=VID{_i}"
            return f"k{i}", req

        orch.run_batch([make_job(i) for i in range(3)], delay_range=(0.06, 0.06))

        assert len(starts) == 3
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert all(gap >= 0.04 for gap in gaps)

    def test_mixed_direct_and_lazy_fast_jobs_share_one_start_cadence(self):
        starts: list[float] = []

        class Engine(_PlainEngine):
            def download(self, req):
                starts.append(time.monotonic())
                super().download(req)

        engine = Engine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=3)
        direct = _req("https://example.com/direct")
        direct.youtube_reliability_mode = "fast"
        lazy = []
        for i in range(2):
            req = _req(f"placeholder://{i}")
            req.youtube_reliability_mode = "fast"
            req.url_resolver = lambda _ev, _i=i: f"https://www.youtube.com/watch?v=VID{_i}"
            lazy.append((f"lazy-{i}", req))

        orch.run_batch([("direct", direct), *lazy], delay_range=(0.06, 0.06))

        assert len(starts) == 3
        assert all(b - a >= 0.04 for a, b in zip(starts, starts[1:]))

    def test_cancel_during_pipeline_stagger_does_not_start_the_reserved_job(self):
        first_started = threading.Event()
        downloads: list[str] = []

        class Engine(_PlainEngine):
            def download(self, req):
                downloads.append(req.url)
                first_started.set()
                super().download(req)

        engine = Engine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=2)

        def make_job(i):
            req = _req(f"placeholder://{i}")
            req.youtube_reliability_mode = "fast"
            req.url_resolver = lambda _ev, _i=i: f"https://www.youtube.com/watch?v=VID{_i}"
            return f"k{i}", req

        thread = threading.Thread(
            target=orch.run_batch,
            args=([make_job(0), make_job(1)],),
            kwargs={"delay_range": (0.5, 0.5)},
        )
        thread.start()
        assert first_started.wait(timeout=2)
        orch.cancel()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert len(downloads) == 1

    def test_conservative_pipeline_uses_only_its_existing_gate_and_cooldown(self):
        engine = _PlainEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)
        orch._pipeline_delay_range = (1.0, 1.0)  # noqa: SLF001 - direct gate unit test
        started = time.monotonic()
        assert orch._wait_for_pipeline_stagger(  # noqa: SLF001 - direct gate unit test
            "k", threading.Event(), conservative_youtube=True
        )
        assert time.monotonic() - started < 0.1

    def test_downloads_consume_a_bounded_resolver_lookahead(self, monkeypatch):
        # Drop the conservative cooldown so the test is fast; one download
        # worker makes the bounded resolver look-ahead deterministic.
        monkeypatch.setattr(
            DownloadOrchestrator, "_youtube_cooldown", lambda self, ev, key: None
        )

        log: list[tuple[str, int]] = []
        engine = _OrderEngine(log, hold_s=0.05)
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

        # Every transfer consumes a URL prepared by the resolver pool.
        assert {i for phase, i in log if phase == "resolve"} == set(range(n))
        assert {i for phase, i in log if phase == "download"} == set(range(n))
        for i in range(n):
            assert log.index(("resolve", i)) < log.index(("download", i))
        # The bounded look-ahead starts a real transfer before the third track
        # is resolved, so the whole catalog is never pre-matched upfront.
        assert log.index(("download", 0)) < log.index(("resolve", 2))

    def test_cancel_stops_the_pipeline_before_more_tracks_are_resolved(self):
        started = threading.Event()
        release = threading.Event()
        resolved: list[int] = []
        engine = _OrderEngine([])
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)

        def make_job(i):
            req = _req(f"placeholder://{i}")

            def resolver(ev, _i=i):
                resolved.append(_i)
                started.set()
                release.wait(timeout=2)
                return f"https://www.youtube.com/watch?v=VID{_i}"

            req.url_resolver = resolver
            return f"k{i}", req

        thread = threading.Thread(
            target=orch.run_batch, args=([make_job(i) for i in range(6)],)
        )
        thread.start()
        assert started.wait(timeout=2)
        orch.cancel()
        release.set()
        thread.join(timeout=3)

        assert not thread.is_alive()
        assert resolved == [0]
        assert engine._log == []


# ──────────────────────────────────────────────────────────────────────────────
# Timing diagnostics: gate starvation + first byte
# ──────────────────────────────────────────────────────────────────────────────

class _PlainEngine:
    """Fake engine that reports immediate success (no progress ticks)."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        if req.on_finished:
            req.on_finished(
                DownloadProgress(status=DownloadStatus.FINISHED, url=req.url, output_path="/tmp/o.mp3")
            )


class _ProgressEngine:
    """Fake engine that emits one non-zero progress tick (the first byte) and
    then finishes."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        if req.on_progress:
            req.on_progress(DownloadProgress(
                status=DownloadStatus.DOWNLOADING, url=req.url,
                downloaded_bytes=1024, total_bytes=2048, fraction=0.5,
            ))
        if req.on_finished:
            req.on_finished(
                DownloadProgress(status=DownloadStatus.FINISHED, url=req.url, output_path="/tmp/o.mp3")
            )


class _WeightedOnlyEngine(_ProgressEngine):
    """Reports phase/weighted progress before transfer bytes arrive."""

    def __init__(self, after_weighted_progress) -> None:
        super().__init__()
        self._after_weighted_progress = after_weighted_progress

    def download(self, req: DownloadRequest) -> None:
        if req.on_progress:
            req.on_progress(DownloadProgress(
                status=DownloadStatus.DOWNLOADING, url=req.url,
                downloaded_bytes=0, total_bytes=2048, fraction=0.2,
            ))
            self._after_weighted_progress()
            req.on_progress(DownloadProgress(
                status=DownloadStatus.DOWNLOADING, url=req.url,
                downloaded_bytes=1024, total_bytes=2048, fraction=0.5,
            ))
        if req.on_finished:
            req.on_finished(
                DownloadProgress(status=DownloadStatus.FINISHED, url=req.url, output_path="/tmp/o.mp3")
            )


class TestGateStarvationMetric:
    def test_gate_idle_recorded_when_matches_lag(self, monkeypatch):
        # Drop the cooldown so the only thing that can delay the next acquire is
        # the (deliberately slow) match — i.e. genuine gate starvation.
        monkeypatch.setattr(
            DownloadOrchestrator, "_youtube_cooldown", lambda self, ev, key: None
        )
        engine = _PlainEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)

        def make_job(i):
            req = _req(f"placeholder://{i}")
            req.youtube_reliability_mode = "conservative"

            def resolver(ev, _i=i):
                time.sleep(0.05)  # match lags behind the freed gate
                return f"https://www.youtube.com/watch?v=VID{_i}"

            req.url_resolver = resolver
            return (f"k{i}", req)

        orch.run_batch([make_job(i) for i in range(3)])

        # gate_idle is recorded for the 2nd and 3rd acquires (never the first),
        # and is non-zero because each match lagged behind the freed gate.
        total, count = orch._phase_times.get("gate_idle", (0.0, 0.0))  # noqa: SLF001
        assert count == 2
        assert total > 0.0

    def test_no_gate_idle_when_not_serialized(self, monkeypatch):
        # A single conservative job never engages the serial gate, so there is
        # no gate_idle phase at all.
        engine = _PlainEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)
        req = _req("https://www.youtube.com/watch?v=ONLY")
        req.youtube_reliability_mode = "conservative"
        orch.run_batch([("k0", req)])
        assert "gate_idle" not in orch._phase_times  # noqa: SLF001


class TestFirstByteMetric:
    def test_first_byte_wait_recorded_on_first_downloaded_byte(self):
        engine = _ProgressEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)
        orch.run_batch([("k0", _req("https://example.com/a"))])
        total, count = orch._phase_times.get("first_byte_wait", (0.0, 0.0))  # noqa: SLF001
        assert count == 1
        assert total >= 0.0

    def test_weighted_progress_without_bytes_is_not_a_first_byte(self):
        class Callbacks(_NullCallbacks):
            def __init__(self):
                self.first_bytes: list[str] = []

            def on_track_first_byte(self, key):
                self.first_bytes.append(key)

        callbacks = Callbacks()
        seen_after_weighted_progress: list[list[str]] = []
        orch = DownloadOrchestrator(
            engine=_WeightedOnlyEngine(
                lambda: seen_after_weighted_progress.append(callbacks.first_bytes.copy())
            ),
            callbacks=callbacks,
            max_workers=1,
        )
        orch.run_batch([("k0", _req("https://example.com/a"))])
        assert seen_after_weighted_progress == [[]]
        assert callbacks.first_bytes == ["k0"]
        assert orch._phase_times["first_byte_wait"][1] == 1  # noqa: SLF001

    def test_no_first_byte_wait_without_progress(self):
        # An engine that never reports progress records no first-byte phase.
        engine = _PlainEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks(), max_workers=1)
        orch.run_batch([("k0", _req("https://example.com/a"))])
        assert "first_byte_wait" not in orch._phase_times  # noqa: SLF001
