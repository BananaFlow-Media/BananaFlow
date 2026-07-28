"""
core/match_cache.py  –  Persistent Spotify→YouTube match cache
==============================================================
Resolving a Spotify track to a playable YouTube URL is expensive: a
YouTube-Music search plus, on low confidence, a yt-dlp ``ytsearchN:``
query — one or two real network round-trips *per track*.  A prolific
artist's discography is 100+ tracks, so re-importing the same artist (or
overlapping playlists/albums) repeats the same searches every time.

This module persists the resolved mapping so a repeat import is a local
lookup instead of a network search.

Design
------
* Zero GUI imports – pure stdlib (sqlite3, threading, hashlib).
* Keyed by a **stable Spotify track id** when one is available, falling
  back to a **composite** hash of ``artist|title|duration`` when it is
  not (e.g. a DOM scrape that could not read the track href).  The
  ``key_kind`` column records which was used so the two key spaces never
  collide.
* Keyed *also* by an integer ``algo_version`` supplied by the caller
  (see ``core.spotify_match_scorer.MATCH_ALGO_VERSION``): bumping the
  matcher's scoring logic bumps the version, which transparently
  invalidates every stale row without a migration.
* Connection pattern mirrors :class:`core.history_db.HistoryDB`: one
  ``sqlite3.connect(..., check_same_thread=False, isolation_level=None)``
  opened for the object's lifetime, WAL journal mode, and a single
  ``threading.Lock`` wrapping every use.  Safe to call from the resolver
  thread pool.
* Caching is strictly best-effort: any failure (corrupt file, locked
  disk, …) degrades to "no cache" — ``get`` returns None, ``put`` is a
  no-op — and never propagates to the download path.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS yt_match_cache (
    spotify_key   TEXT    NOT NULL,
    key_kind      TEXT    NOT NULL,
    algo_version  INTEGER NOT NULL,
    youtube_url   TEXT    NOT NULL,
    confidence    REAL,
    matched_at    TEXT    NOT NULL,
    PRIMARY KEY (spotify_key, algo_version)
);
"""

_GET_SQL = (
    "SELECT youtube_url FROM yt_match_cache "
    "WHERE spotify_key = ? AND algo_version = ?"
)

_PUT_SQL = """
INSERT INTO yt_match_cache
    (spotify_key, key_kind, algo_version, youtube_url, confidence, matched_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(spotify_key, algo_version) DO UPDATE SET
    key_kind    = excluded.key_kind,
    youtube_url = excluded.youtube_url,
    confidence  = excluded.confidence,
    matched_at  = excluded.matched_at
"""

_DELETE_SQL = (
    "DELETE FROM yt_match_cache "
    "WHERE spotify_key = ? AND algo_version = ?"
)


class MatchCache:
    """Thread-safe SQLite cache of Spotify-key → YouTube-URL matches.

    Parameters
    ----------
    db_path : str | None
        Absolute path to the SQLite file.  Pass None to use the default
        (``<app-data>/match_cache.db``), or ``":memory:"`` for tests.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None or db_path == "":
            db_path = self._default_path()

        self._path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

        try:
            if db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                db_path,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.execute("PRAGMA journal_mode=WAL;")
            with conn:
                conn.execute(_CREATE_TABLE_SQL)
            self._conn = conn
            logger.info("[MatchCache] Opened %s (%d entries)", db_path, self.count())
        except Exception as exc:
            # Caching is optional — a broken cache must never block downloads.
            logger.warning("[MatchCache] Disabled (open failed): %s", exc)
            self._conn = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, spotify_key: str, algo_version: int) -> Optional[str]:
        """Return the cached YouTube URL for this key/version, or None."""
        if not self._conn or not spotify_key:
            return None
        try:
            with self._lock:
                cursor = self._conn.execute(_GET_SQL, (spotify_key, algo_version))
                row = cursor.fetchone()
            return row[0] if row else None
        except Exception as exc:
            logger.debug("[MatchCache] get failed: %s", exc)
            return None

    def put(
        self,
        spotify_key: str,
        youtube_url: str,
        confidence: Optional[float],
        algo_version: int,
        key_kind: str = "spotify_id",
    ) -> None:
        """Store (or update) a resolved match.  Best-effort; never raises."""
        if not self._conn or not spotify_key or not youtube_url:
            return
        try:
            with self._lock:
                with self._conn:
                    self._conn.execute(
                        _PUT_SQL,
                        (
                            spotify_key,
                            key_kind,
                            algo_version,
                            youtube_url,
                            confidence,
                            _utc_now(),
                        ),
                    )
        except Exception as exc:
            logger.debug("[MatchCache] put failed: %s", exc)

    def delete(
        self,
        spotify_key: str,
        algo_version: int,
        *,
        expected_url: Optional[str] = None,
    ) -> bool:
        """Invalidate one mapping, optionally only if its URL still matches.

        The compare-and-delete form prevents a late failure from deleting a
        newer mapping another resolver has already installed.
        """
        if not self._conn or not spotify_key:
            return False
        try:
            with self._lock:
                with self._conn:
                    if expected_url is None:
                        cursor = self._conn.execute(
                            _DELETE_SQL, (spotify_key, algo_version),
                        )
                    else:
                        cursor = self._conn.execute(
                            _DELETE_SQL + " AND youtube_url = ?",
                            (spotify_key, algo_version, expected_url),
                        )
            return bool(cursor.rowcount)
        except Exception as exc:
            logger.debug("[MatchCache] delete failed: %s", exc)
            return False

    def count(self) -> int:
        """Return the number of cached entries (0 if the cache is disabled)."""
        if not self._conn:
            return 0
        try:
            with self._lock:
                cursor = self._conn.execute("SELECT COUNT(*) FROM yt_match_cache")
                row = cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ── Key construction ──────────────────────────────────────────────────────

    @staticmethod
    def composite_key(artist: str, title: str, duration_sec: Optional[int]) -> str:
        """Build a stable fallback key when no Spotify track id is available.

        Normalizes artist/title (lowercase, collapsed whitespace) and buckets
        the duration to the nearest 3 seconds so minor scrape jitter does not
        fragment the cache, then hashes the result.  Prefixed ``c:`` so it can
        never collide with a raw Spotify track id.
        """
        a = " ".join((artist or "").lower().split())
        t = " ".join((title or "").lower().split())
        bucket = int(round((duration_sec or 0) / 3.0)) * 3
        raw = f"{a}|{t}|{bucket}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return f"c:{digest}"

    def __repr__(self) -> str:
        state = "disabled" if not self._conn else f"entries={self.count()}"
        return f"MatchCache(path={self._path!r}, {state})"

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _default_path() -> str:
        """Canonical default path inside the app-data dir (sibling of the
        download-history DB, but a separate file so cache churn never touches
        history)."""
        from utils.paths import get_app_data_dir
        return str(get_app_data_dir() / "match_cache.db")


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Process-wide singleton ─────────────────────────────────────────────────────
# The resolver runs the cache from a worker pool; every thread must share one
# connection (and its lock) rather than opening its own.
_SINGLETON: Optional[MatchCache] = None
_SINGLETON_LOCK = threading.Lock()


def get_match_cache() -> MatchCache:
    """Return the shared process-wide :class:`MatchCache` instance."""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = MatchCache()
    return _SINGLETON
