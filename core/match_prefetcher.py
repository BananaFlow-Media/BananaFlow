"""
core/match_prefetcher.py  –  Cancellable fast-start match prefetch
==================================================================
Two-stage Spotify import shows the whole catalog instantly and defers each
track's YouTube match to download time (see core/download_orchestrator.py).
That keeps the catalog fast, but on a cache-miss the very first download pays
the full match round-trip *after* the click — the visible "click → first
download" gap.

This module closes that gap without pre-matching the whole catalog and without
lowering match quality:

* **(A) plugin warm-up** — yt-dlp's one-time ``load_all_plugins()`` is forced
  once, single-threaded, off the click path.  The ``metadata_only`` stage-1
  path never calls it (``_resolve_all`` is skipped), so otherwise the first
  yt-dlp op at download time would pay that cost on the critical path.
* **(B) fast-start pre-resolve** — the first ``limit`` pending tracks are
  resolved via the *exact same* ``resolve_track_to_youtube`` path (full match
  quality) while the catalog is on screen.  Results land in the persistent
  match cache, so the download-time resolver returns them instantly.

The work runs on a daemon background thread, is cancellable at any time, and
never blocks or requires the catalog.  It is idempotent with the cache: a
track already cached is a no-op, and racing the download worker on the same
track only wastes a lookup (both store the same result).

Zero Qt imports — driven by the UI layer via start()/cancel().
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

# How many leading tracks to pre-resolve. The window is intentionally small —
# the download worker pool already keeps a ~(max_workers-1) resolved lookahead
# at the conservative gate, so this only needs to cover the batch's opening.
DEFAULT_PREFETCH_LIMIT = 8

# Concurrency for the pre-resolve pass. Matches the stage-1 resolve fan-out;
# the searches are network-bound and independent.
_PREFETCH_WORKERS = 3
_prefetched_matches_lock = threading.Lock()
_prefetched_match_keys: set[str] = set()


def _prefetch_key(track: dict) -> str:
    return str(track.get("spotify_id") or track.get("spotify_url") or "")


def mark_prefetched_match(track: dict) -> None:
    """Remember this session's prefetch hits for ETA provenance only."""
    key = _prefetch_key(track)
    if key:
        with _prefetched_matches_lock:
            _prefetched_match_keys.add(key)


def was_prefetched_match(spotify_key: str) -> bool:
    with _prefetched_matches_lock:
        return spotify_key in _prefetched_match_keys


class MatchPrefetcher:
    """Warms yt-dlp plugins once, then pre-resolves the first N pending tracks
    into the match cache on a cancellable background thread.

    A single instance is meant to be reused: each :meth:`start` cancels any
    prior run first, so a new fetch/catalog cleanly supersedes the old one.
    """

    def __init__(
        self,
        cookies_file: Optional[str] = None,
        limit: int = DEFAULT_PREFETCH_LIMIT,
        max_workers: int = _PREFETCH_WORKERS,
    ) -> None:
        self._cookies = cookies_file
        self._limit = max(0, limit)
        self._max_workers = max(1, max_workers)
        self._cancel = threading.Event()
        self._cancel.set()  # idle until the first start()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ── Public API (call from any thread) ─────────────────────────────────────

    def start(self, tracks: list[dict], cookies_file: Optional[str] = None) -> None:
        """Begin warming plugins and pre-resolving up to ``limit`` tracks.

        ``tracks`` are track dicts (``spotify_id``/``title``/``artist``/
        ``duration_sec``) — the same shape ``resolve_track_to_youtube`` consumes.
        ``cookies_file``, when given, updates the cookies used for this and
        later runs (the instance is reused across fetches). Non-blocking:
        returns immediately while work proceeds in the background. A prior run,
        if any, is cancelled first.
        """
        with self._lock:
            self._cancel_locked()
            if cookies_file is not None:
                self._cookies = cookies_file
            subset = [t for t in tracks if t][: self._limit]
            cancel = threading.Event()
            self._cancel = cancel
            self._thread = threading.Thread(
                target=self._run,
                args=(subset, cancel),
                name="match-prefetch",
                daemon=True,
            )
            self._thread.start()

    def warm_up_async(self) -> None:
        """Warm yt-dlp's plugin registry once, off the caller's thread.

        Kicked at fetch start so the one-time ``load_all_plugins()`` cost is
        already paid by the time the first download runs — independent of, and
        earlier than, the per-track pre-resolve. Idempotent and cheap: a second
        call after the flag is set is a no-op inside ``warm_up_plugins``.
        """
        def _warm() -> None:
            try:
                from core.runtime_components import warm_up_plugins
                warm_up_plugins()
            except Exception as exc:  # noqa: BLE001 - warm-up must never raise out
                logger.debug("[prefetch] async warm-up failed: %s", exc)

        threading.Thread(target=_warm, name="plugin-warmup", daemon=True).start()

    def cancel(self) -> None:
        """Signal the background run to stop as soon as possible.

        Returns without joining — the worker thread is a daemon and the
        resolver honours the cancel check between network calls.
        """
        with self._lock:
            self._cancel_locked()

    def _cancel_locked(self) -> None:
        self._cancel.set()

    # ── Background worker ─────────────────────────────────────────────────────

    def _run(self, tracks: list[dict], cancel: threading.Event) -> None:
        t0 = time.monotonic()

        # (A) One-time plugin load, single-threaded, before any pool exists.
        try:
            from core.runtime_components import warm_up_plugins
            warm_up_plugins()
            logger.info(
                "[timing][prefetch] plugins warmed in %.2fs", time.monotonic() - t0
            )
        except Exception as exc:  # noqa: BLE001 - warm-up must never sink prefetch
            logger.debug("[prefetch] warm_up_plugins failed: %s", exc)

        if not tracks or cancel.is_set():
            return

        from core.scraper import resolve_track_to_youtube

        def _resolve_one(td: dict) -> None:
            if cancel.is_set():
                return
            title = td.get("title", "")
            started = time.monotonic()
            try:
                resolve_track_to_youtube(
                    td,
                    cookies_file=self._cookies,
                    cancel_check=cancel.is_set,
                )
            except Exception as exc:  # noqa: BLE001 - a bad match must not sink prefetch
                logger.debug("[prefetch] resolve failed for %r: %s", title, exc)
                return
            mark_prefetched_match(td)
            logger.debug(
                "[timing][prefetch] resolved %r in %.2fs",
                title, time.monotonic() - started,
            )

        n = min(self._max_workers, len(tracks))
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="prefetch") as pool:
            # map() drains the iterable; each _resolve_one bails immediately once
            # cancelled, so an in-flight cancel completes the pass promptly.
            list(pool.map(_resolve_one, tracks))

        if not cancel.is_set():
            logger.info(
                "[timing][prefetch] fast-start pre-resolved %d track(s) in %.2fs",
                len(tracks), time.monotonic() - t0,
            )
