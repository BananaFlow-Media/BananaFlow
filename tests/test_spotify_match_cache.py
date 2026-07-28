"""
tests/test_spotify_match_cache.py
=================================
PR1 regression tests for the Spotify-artist import fixes:

* the persistent Spotify->YouTube match cache (``core.match_cache``),
* cache-key selection in ``core.scraper`` (stable id vs. composite),
* the cache short-circuit in ``resolve_track_to_youtube`` (no re-search on hit),
* the yt-dlp plugin-registration race fix (``warm_up_plugins``) that used to
  spam ``AssertionError: PoTokenProvider ... already registered``.

All offline — no network, no Qt.
"""

from __future__ import annotations

import contextlib
import io
import threading

import pytest

from core.match_cache import MatchCache


# ──────────────────────────────────────────────────────────────────────────────
# MatchCache
# ──────────────────────────────────────────────────────────────────────────────

class TestMatchCache:
    def test_miss_then_hit(self):
        c = MatchCache(":memory:")
        assert c.get("track123", 1) is None
        c.put("track123", "https://music.youtube.com/watch?v=abc", 0.9, 1)
        assert c.get("track123", 1) == "https://music.youtube.com/watch?v=abc"

    def test_algo_version_isolates_entries(self):
        c = MatchCache(":memory:")
        c.put("track123", "https://music.youtube.com/watch?v=v1", 0.9, 1)
        # A different algorithm version must not read the old row.
        assert c.get("track123", 2) is None
        assert c.get("track123", 1) == "https://music.youtube.com/watch?v=v1"

    def test_put_upserts_same_key(self):
        c = MatchCache(":memory:")
        c.put("k", "https://music.youtube.com/watch?v=old", 0.5, 1)
        c.put("k", "https://music.youtube.com/watch?v=new", 0.8, 1)
        assert c.get("k", 1) == "https://music.youtube.com/watch?v=new"
        assert c.count() == 1

    def test_empty_key_is_noop(self):
        c = MatchCache(":memory:")
        c.put("", "https://music.youtube.com/watch?v=x", 0.9, 1)
        assert c.count() == 0
        assert c.get("", 1) is None

    def test_compare_and_delete_cannot_remove_newer_url(self):
        c = MatchCache(":memory:")
        c.put("k", "https://youtube.test/new", 0.9, 3)
        assert not c.delete("k", 3, expected_url="https://youtube.test/old")
        assert c.get("k", 3) == "https://youtube.test/new"
        assert c.delete("k", 3, expected_url="https://youtube.test/new")
        assert c.get("k", 3) is None

    def test_composite_key_normalizes_and_buckets(self):
        # Case/whitespace differences and <=3s jitter collapse to one key.
        a = MatchCache.composite_key("The Beatles", "Hey  Jude", 431)
        b = MatchCache.composite_key("the beatles", "HEY JUDE", 432)
        assert a == b
        assert a.startswith("c:")

    def test_composite_key_differs_on_real_difference(self):
        a = MatchCache.composite_key("Artist", "Song A", 200)
        b = MatchCache.composite_key("Artist", "Song B", 200)
        assert a != b


# ──────────────────────────────────────────────────────────────────────────────
# Cache-key selection in core.scraper
# ──────────────────────────────────────────────────────────────────────────────

class TestCacheKeySelection:
    def test_prefers_spotify_id_field(self):
        from core.scraper import _spotify_cache_key
        key, kind = _spotify_cache_key(
            {"spotify_id": "track123", "title": "t", "artist": "a"}
        )
        assert kind == "spotify_id"
        assert key == "track123"

    def test_parses_id_from_spotify_url(self):
        from core.scraper import _spotify_cache_key
        key, kind = _spotify_cache_key(
            {
                "spotify_url": "https://open.spotify.com/track/track123?si=xyz",
                "title": "t",
                "artist": "a",
            }
        )
        assert kind == "spotify_id"
        assert key == "track123"

    def test_falls_back_to_composite(self):
        from core.scraper import _spotify_cache_key
        key, kind = _spotify_cache_key(
            {"title": "Song", "artist": "Artist", "duration_sec": 200}
        )
        assert kind == "composite"
        assert key.startswith("c:")

    def test_id_from_url_helper_handles_empty(self):
        from core.scraper import _spotify_id_from_url
        assert _spotify_id_from_url("") == ""
        assert _spotify_id_from_url("https://open.spotify.com/artist/abc") == ""


# ──────────────────────────────────────────────────────────────────────────────
# resolve_track_to_youtube: cache short-circuits the search
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveTrackToYoutube:
    def test_second_call_is_a_cache_hit(self, monkeypatch):
        import core.match_cache as mc
        import core.scraper as scraper

        # Fresh in-memory cache behind the singleton accessor.
        monkeypatch.setattr(mc, "_SINGLETON", MatchCache(":memory:"))

        calls = {"n": 0}

        def fake_resolve(
            title, artist, duration_sec, cookies_file=None,
            cancel_check=None, **_kwargs,
        ):
            calls["n"] += 1
            return "https://music.youtube.com/watch?v=CACHED"

        monkeypatch.setattr(scraper, "_resolve_to_ytm_url", fake_resolve)

        td = {"spotify_id": "sid1", "title": "Song", "artist": "Artist", "duration_sec": 200}
        first = scraper.resolve_track_to_youtube(td)
        second = scraper.resolve_track_to_youtube(td)

        assert first == second == "https://music.youtube.com/watch?v=CACHED"
        assert calls["n"] == 1  # the underlying search ran only once

    def test_last_resort_sentinel_is_not_cached(self, monkeypatch):
        import core.match_cache as mc
        import core.scraper as scraper

        cache = MatchCache(":memory:")
        monkeypatch.setattr(mc, "_SINGLETON", cache)

        calls = {"n": 0}

        def fake_resolve(
            title, artist, duration_sec, cookies_file=None,
            cancel_check=None, **_kwargs,
        ):
            calls["n"] += 1
            return "ytsearch1:Artist Song audio"

        monkeypatch.setattr(scraper, "_resolve_to_ytm_url", fake_resolve)

        td = {"spotify_id": "sid2", "title": "Song", "artist": "Artist", "duration_sec": 200}
        scraper.resolve_track_to_youtube(td)
        scraper.resolve_track_to_youtube(td)

        # A ytsearch* sentinel is not a real match, so it must not be cached —
        # the search re-runs every time until a confident match is found.
        assert calls["n"] == 2
        assert cache.count() == 0

    def test_force_refresh_skips_cache_without_unconditional_delete(self, monkeypatch):
        import core.match_cache as mc
        import core.scraper as scraper
        from core.spotify_match_scorer import MATCH_ALGO_VERSION

        cache = MatchCache(":memory:")
        monkeypatch.setattr(mc, "_SINGLETON", cache)
        cache.put(
            "sid-refresh", "https://youtube.test/concurrent-newer", None,
            MATCH_ALGO_VERSION,
        )

        def fail_delete(*_args, **_kwargs):
            raise AssertionError("refresh must not delete a concurrent mapping")

        monkeypatch.setattr(cache, "delete", fail_delete)
        monkeypatch.setattr(
            scraper,
            "_resolve_to_ytm_url",
            lambda *args, **kwargs: "https://youtube.test/refreshed",
        )
        td = {
            "spotify_id": "sid-refresh",
            "title": "Song",
            "artist": "Artist",
            "duration_sec": 200,
        }

        assert scraper.resolve_track_to_youtube(td, force_refresh=True) == (
            "https://youtube.test/refreshed"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Plugin-registration race fix
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _restore_plugin_flag():
    """Save/restore yt-dlp's ``all_plugins_loaded`` flag around a test."""
    from yt_dlp import plugins as P
    saved = P.all_plugins_loaded.value
    try:
        yield P
    finally:
        P.all_plugins_loaded.value = saved


class TestPluginRaceFix:
    def test_double_load_reproduces_the_error(self, _restore_plugin_flag):
        """Sanity: re-running yt-dlp's loader really does double-register the
        bundled bgutil plugins (this is the bug we are guarding against).

        Skips gracefully if the bgutil PO-token plugins are not installed in
        this environment — the race can only manifest when they are present.
        """
        P = _restore_plugin_flag
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            P.all_plugins_loaded.value = False
            P.load_all_plugins()
            P.all_plugins_loaded.value = False
            P.load_all_plugins()
        out = buf.getvalue()
        if "bgutil" not in out and "already registered" not in out:
            pytest.skip("bgutil PO-token plugins not installed; race not reproducible")
        assert "already registered" in out

    def test_warm_up_prevents_concurrent_reload_errors(self, _restore_plugin_flag):
        """After a single-threaded warm-up, concurrent YoutubeDL construction
        must not re-run the plugin loader, so no 'already registered' storm."""
        import yt_dlp
        from core.runtime_components import warm_up_plugins

        P = _restore_plugin_flag
        P.all_plugins_loaded.value = False
        warm_up_plugins()  # loads once, flag now True

        buf = io.StringIO()

        def build():
            with yt_dlp.YoutubeDL(
                {"quiet": True, "no_warnings": True, "skip_download": True}
            ):
                pass

        with contextlib.redirect_stderr(buf):
            threads = [threading.Thread(target=build) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert "already registered" not in buf.getvalue()

    def test_warm_up_is_thread_safe_and_never_raises(self):
        from core.runtime_components import (
            ensure_plugin_dir_registered,
            warm_up_plugins,
        )

        errors: list = []

        def hammer(fn):
            try:
                fn()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(warm_up_plugins,)) for _ in range(15)]
        threads += [
            threading.Thread(target=hammer, args=(ensure_plugin_dir_registered,))
            for _ in range(15)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
