"""
core/scraper.py – Deeply Isolated & Hyper-Optimized Media Scraper
==================================================================
Dedicated extraction functions for every platform and content type.
Optimized for high-speed artist discography scraping using continuous accumulation.

Playwright is imported lazily inside the scraping entry points so the
``core.scraper`` module itself still imports on a Playwright-less
install (e.g. headless CI or a user who skipped the post-install
``scripts/install_playwright.ps1``). Each entry point calls
``require_playwright_or_raise`` first and surfaces a clean error.
"""
from __future__ import annotations

from typing import Callable, Optional, Dict, List, Tuple, TYPE_CHECKING
import logging
import re
import threading
import yt_dlp
from utils.yt_dlp_opts import build_parse_ydl_opts as _build_parse_ydl_opts
from utils.logger import SilentLogger as _SilentLogger
from utils.playwright_check import require_playwright_or_raise

if TYPE_CHECKING:
    # Imports for type-checkers only; runtime defers the real import to
    # the per-function call sites guarded by require_playwright_or_raise.
    from playwright.sync_api import Page  # noqa: F401

logger = logging.getLogger(__name__)


def _emit_pending_track(
    track_dict: Dict,
    on_item: Optional[Callable[[Dict], None]] = None,
) -> None:
    """Publish one scraped track *immediately* as an unmatched stage-1 item.

    Called from inside the scrape loop (not after it) so a large catalog fills
    the UI progressively instead of appearing all at once when the scrape ends.
    The track gets a ``ytsearch*`` placeholder URL (still playable as a last
    resort) and is tagged ``match_status="pending"`` so the download path
    resolves it lazily. No network calls happen here.
    """
    if track_dict.get("match_status") == "metadata_invalid":
        track_dict["url"] = ""
        if on_item:
            on_item(track_dict)
        return
    if not track_dict.get("url"):
        q = f"{track_dict.get('artist', '')} {track_dict.get('title', '')}".strip()
        track_dict["url"] = f"ytsearch1:{q} audio"
    track_dict["match_status"] = "pending"
    if on_item:
        on_item(track_dict)


def _parallel_resolve_urls(
    items: List[Dict],
    on_item: Optional[Callable[[Dict], None]] = None,
    max_workers: int = 5,
    cookies_file: Optional[str] = None,
) -> None:
    """
    Resolve empty "url" fields in-place using ytmusicapi search, running up to
    max_workers searches concurrently.  Order is preserved.

    When an ``on_item`` callback is supplied it is fired progressively, in
    scrape order, as each item's URL becomes ready — so the UI fills in
    incrementally instead of staying frozen until every search has finished.
    Called after the Playwright browser session closes so the browser and the
    network calls never compete for the same thread / event loop.
    """
    if not items:
        return
    from concurrent.futures import ThreadPoolExecutor
    from core.runtime_components import warm_up_plugins

    # Force yt-dlp's one-time plugin load on THIS thread before any worker
    # starts.  Workers build fresh YoutubeDL() instances concurrently; if the
    # first loads race inside yt-dlp's own load_all_plugins() they re-exec the
    # bundled PO-token plugins and raise "PoTokenProvider ... already
    # registered" in a storm.  Warming up single-threaded here leaves the flag
    # set so every worker's construction skips the loader entirely.
    warm_up_plugins()

    def _resolve_one(td: Dict) -> str:
        return resolve_track_to_youtube(td, cookies_file=cookies_file)

    # NOTE: results are consumed in submission order (not as_completed) on
    # purpose — cards are *created* by this callback, and the display index is
    # assigned by arrival order downstream, so firing out of order would
    # reorder the visible list.  Pipelined, completion-order resolution lands
    # in the two-stage flow, where rows pre-exist and are updated by index.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_resolve_one, td) for td in items]
        for td, fut in zip(items, futures):
            td["url"] = fut.result()
            if on_item:
                on_item(td)


def _resolve_to_ytm_url(
    title: str, artist: str, duration_sec: int = 0,
    cookies_file: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    exclude_urls: Optional[set[str]] = None,
    album: str = "",
) -> str:
    """
    Resolve a track to a YouTube Music URL via ytmusicapi search.

    Uses recording-identity gates first, then the existing broad search and a
    ranked reasonable fallback. If discovery yields no usable candidate,
    valid Spotify metadata remains downloadable through a legacy ``ytsearch1``
    request; malformed metadata is still rejected before any search.

    ``cancel_check`` (if given) is polled between the cheap YTM search and the
    heavier yt-dlp fallback so a cancel doesn't let an already-doomed second
    network call run to completion.
    """
    from utils.spotify_resolver import validate_spotify_track_metadata

    title, artist_credits = validate_spotify_track_metadata(title, [artist])
    artist = ", ".join(artist_credits)
    query = f"{artist} {title}" if artist else title
    excluded = exclude_urls or set()
    ytm_matches = []
    best_ytm_match = None
    best_general_match = None

    try:
        from ytmusicapi import YTMusic
        from core.spotify_match_scorer import match_from_metadata

        yt = YTMusic()
        results = yt.search(query, filter="songs", limit=5)
        if results:
            def _dur_secs(r: dict) -> int:
                d = r.get("duration_seconds")
                if d:
                    return int(d)
                d_str = r.get("duration", "")
                if d_str and ":" in d_str:
                    parts = d_str.split(":")
                    try:
                        if len(parts) == 2:
                            return int(parts[0]) * 60 + int(parts[1])
                        if len(parts) == 3:
                            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    except (ValueError, TypeError):
                        pass
                return 0

            for r in results:
                vid = r.get("videoId")
                if not vid:
                    continue

                yt_title = r.get("title") or ""
                artists_list = r.get("artists") or []
                artist_names = [
                    item.get("name") or "" for item in artists_list
                    if isinstance(item, dict)
                ]
                yt_channel = artist_names[0] if artist_names else ""
                yt_dur = _dur_secs(r)

                spotify_dur = duration_sec if duration_sec > 0 else None

                candidate_url = f"https://music.youtube.com/watch?v={vid}"
                if candidate_url in excluded:
                    continue
                match = match_from_metadata(
                    url=candidate_url,
                    title=title,
                    artist=artist,
                    duration_sec=spotify_dur,
                    yt_title=yt_title,
                    yt_channel=yt_channel,
                    yt_duration_sec=yt_dur if yt_dur > 0 else None,
                    yt_artists=artist_names,
                    spotify_album=album,
                    yt_album=(r.get("album") or {}).get("name", "")
                    if isinstance(r.get("album"), dict) else "",
                )
                ytm_matches.append(match)
                if match.safe and (
                    best_ytm_match is None
                    or (match.score, match.url) > (best_ytm_match.score, best_ytm_match.url)
                ):
                    best_ytm_match = match

            if best_ytm_match:
                logger.debug(
                    "[Scraper] Best safe YTM candidate: %s (score=%.1f, confidence=%.2f)",
                    best_ytm_match.url, best_ytm_match.score, best_ytm_match.confidence,
                )
                if best_ytm_match.confidence >= 0.65:
                    return best_ytm_match.url

    except Exception as exc:
        logger.debug("[Scraper] ytmusicapi search or scoring failed: %s", exc)

    # A cancel between the cheap YTM search and the heavier yt-dlp fallback
    # returns no match immediately rather than firing
    # another network call the user has already abandoned.
    if cancel_check and cancel_check():
        return ""

    # ── Fallback 1: General YouTube Search ──
    try:
        from core.spotify_match_scorer import find_best_youtube_match
        spotify_dur = duration_sec if duration_sec > 0 else None
        match_kwargs = dict(
            title=title, artist=artist, duration_sec=spotify_dur,
            min_confidence=0.55, cookies_file=cookies_file,
            album=album, allow_reasonable_fallback=True,
        )
        if excluded:
            match_kwargs["exclude_urls"] = excluded
        yt_match = find_best_youtube_match(**match_kwargs)
        if yt_match:
            if yt_match.safe:
                logger.info(
                    "[Scraper] YTM confidence was low. Found strong general "
                    "YouTube match: %s (confidence=%.2f)",
                    yt_match.youtube_title, yt_match.confidence,
                )
                return yt_match.url
            best_general_match = yt_match
    except Exception as exc:
        logger.debug("[Scraper] General YouTube fallback search failed: %s", exc)

    # The strict YTM threshold is deliberately high. Retain a lower-confidence
    # safe YTM result before considering an approximate candidate.
    if best_ytm_match and best_ytm_match.confidence >= 0.55:
        logger.info(
            "[Scraper] General fallback was inconclusive; using the closest "
            "identity-safe structured YTM candidate: %s (confidence=%.2f)",
            best_ytm_match.youtube_title, best_ytm_match.confidence,
        )
        return best_ytm_match.url

    # The broad resolver has already had its opportunity. If it produced no
    # result, a structured YTM candidate may still be the closest reasonable
    # recording when only independent artist proof is missing (for example
    # Latin Spotify credits versus Hebrew YouTube Music credits).
    try:
        from core.spotify_match_scorer import _rank, is_reasonable_fallback_match
        reasonable_candidates = [
            item for item in _rank([
                *ytm_matches,
                *([best_general_match] if best_general_match else []),
            ])
            if is_reasonable_fallback_match(item)
        ]
        if reasonable_candidates:
            best = reasonable_candidates[0]
            logger.info(
                "[Scraper] Using closest reasonable YouTube candidate: "
                "%s (ranking_score=%.1f)",
                best.youtube_title,
                float(best.breakdown.get("ranking_score", best.score)),
            )
            return best.url
    except Exception as exc:
        logger.debug("[Scraper] Reasonable YTM ranking failed: %s", exc)

    fallback = spotify_legacy_search_request(title, artist)
    logger.warning(
        "[Scraper] No direct reasonable candidate for title=%r artist=%r; "
        "delegating to legacy search request",
        title, artist,
    )
    return fallback


def spotify_legacy_search_request(title: str, artist: str) -> str:
    """Build the final yt-dlp search request for valid Spotify metadata."""
    from utils.spotify_resolver import validate_spotify_track_metadata

    clean_title, artist_credits = validate_spotify_track_metadata(title, [artist])
    clean_artist = ", ".join(artist_credits)
    query = " ".join(part for part in (clean_artist, clean_title, "audio") if part)
    return f"ytsearch1:{query}"


def _spotify_id_from_url(url: str) -> str:
    """Extract the bare Spotify track id from a track URL, or "" if absent."""
    m = re.search(r"/track/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else ""


def _spotify_album_id_from_url(url: str) -> str:
    """Extract the bare Spotify album id from an album URL, or "" if absent."""
    match = re.search(r"/album/([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else ""


def _spotify_cache_key(td: Dict) -> Tuple[str, str]:
    """Return ``(spotify_key, key_kind)`` for a track dict.

    Prefers a stable Spotify track id (from ``spotify_id`` or parsed out of
    ``spotify_url``); falls back to a composite ``artist|title|duration`` hash
    when no id is available.  ``key_kind`` records which was used so the two
    key spaces never collide in the cache.
    """
    from core.match_cache import MatchCache

    sid = (td.get("spotify_id") or "").strip()
    if not sid:
        sid = _spotify_id_from_url(td.get("spotify_url") or "")
    if sid:
        return sid, "spotify_id"
    return (
        MatchCache.composite_key(
            td.get("artist", ""), td.get("title", ""), td.get("duration_sec") or 0
        ),
        "composite",
    )


class _ResolutionFlight:
    """One process-local cold resolution shared by prefetch and download."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = ""
        self.error: Optional[BaseException] = None


_resolution_flights_lock = threading.Lock()
_resolution_flights: dict[tuple, _ResolutionFlight] = {}


def resolve_track_to_youtube(
    td: Dict,
    cookies_file: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    *,
    force_refresh: bool = False,
    exclude_urls: Optional[set[str]] = None,
) -> str:
    """Cache-aware resolution of one track dict to a YouTube URL.

    Consults the persistent match cache first (keyed by stable Spotify track
    id when available, else a composite key).  On a miss it runs the existing
    :func:`_resolve_to_ytm_url` search chain and stores a *real* match before
    returning.  The last-resort ``ytsearch1:`` sentinel is never cached — it
    is not a real match, and persisting it would pin a bad result across runs.

    ``cancel_check`` is forwarded to the search chain so a download-time cancel
    stops an in-flight match promptly.
    """
    from core.match_cache import get_match_cache
    from core.spotify_match_scorer import MATCH_ALGO_VERSION

    spotify_key, key_kind = _spotify_cache_key(td)
    cache = get_match_cache()

    # A refresh skips the current row but does not delete it here. Stale-target
    # recovery already uses compare-and-delete with the failed URL; an
    # unconditional second delete would race with another resolver that may
    # have installed a newer mapping in the meantime.
    cached = None if force_refresh else cache.get(spotify_key, MATCH_ALGO_VERSION)
    if cached and cached not in (exclude_urls or set()):
        try:
            from core.match_prefetcher import was_prefetched_match
            td["_match_source"] = (
                "prefetched" if was_prefetched_match(spotify_key) else "cache"
            )
        except Exception:
            td["_match_source"] = "cache"
        return cached

    # The fetch prefetcher and the download pipeline can miss the same cache
    # row before either has written it. Share that cold operation in-process;
    # refreshes remain separate because they deliberately exclude old URLs.
    flight_key = (
        spotify_key,
        MATCH_ALGO_VERSION,
        bool(force_refresh),
        tuple(sorted(exclude_urls or ())),
    )
    with _resolution_flights_lock:
        flight = _resolution_flights.get(flight_key)
        owner = flight is None
        if owner:
            flight = _ResolutionFlight()
            _resolution_flights[flight_key] = flight

    if not owner:
        while not flight.done.wait(0.05):
            if cancel_check and cancel_check():
                return ""
        if flight.error is not None:
            raise flight.error
        td["_match_source"] = "shared"
        return flight.result

    try:
        resolve_kwargs = dict(
            cookies_file=cookies_file,
            cancel_check=cancel_check,
            album=td.get("album") or td.get("album_name") or "",
        )
        if exclude_urls:
            resolve_kwargs["exclude_urls"] = exclude_urls
        url = _resolve_to_ytm_url(
            td.get("title", ""),
            td.get("artist", ""),
            td.get("duration_sec") or 0,
            **resolve_kwargs,
        )

        if url and not url.startswith("ytsearch"):
            cache.put(spotify_key, url, None, MATCH_ALGO_VERSION, key_kind=key_kind)
        td["_match_source"] = "live"
        flight.result = url
        return url
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        flight.done.set()
        with _resolution_flights_lock:
            if _resolution_flights.get(flight_key) is flight:
                _resolution_flights.pop(flight_key, None)


def invalidate_track_match(td: Dict, expected_url: Optional[str] = None) -> bool:
    """Invalidate one cached match after a proven media-unavailable failure."""
    from core.match_cache import get_match_cache
    from core.spotify_match_scorer import MATCH_ALGO_VERSION
    from utils.spotify_resolver import validate_spotify_track_metadata

    clean_title, artist_credits = validate_spotify_track_metadata(
        td.get("title", ""), [td.get("artist", "")]
    )
    td["title"] = clean_title
    td["artist"] = ", ".join(artist_credits)

    spotify_key, _key_kind = _spotify_cache_key(td)
    return get_match_cache().delete(
        spotify_key, MATCH_ALGO_VERSION, expected_url=expected_url,
    )


def track_match_source_hint(td: Dict) -> str:
    """Inspect local state only; never perform a network match."""
    from core.match_cache import get_match_cache
    from core.spotify_match_scorer import MATCH_ALGO_VERSION

    spotify_key, _key_kind = _spotify_cache_key(td)
    if not get_match_cache().get(spotify_key, MATCH_ALGO_VERSION):
        return "live"
    try:
        from core.match_prefetcher import was_prefetched_match
        if was_prefetched_match(spotify_key):
            return "prefetched"
    except Exception:
        pass
    return "cache"


def _sync_playwright_for(feature: str):
    """Resolve playwright.sync_api.sync_playwright with a friendly precheck.

    Raises :class:`utils.playwright_check.PlaywrightNotAvailable` with
    a localised English/Hebrew message before the deep import fails,
    so callers (workers, controllers) can show a clean MessageBox
    instead of a stack trace.
    """
    require_playwright_or_raise(feature)
    from playwright.sync_api import sync_playwright  # noqa: WPS433
    return sync_playwright
# ── Private Internal Helpers (Shared common logic) ────────────────────────────
# Real Desktop User Agent to avoid bot-detection
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"


def _spotify_context_kwargs(locale: str = "en-US", **kwargs) -> dict:
    """Build a Spotify browser context in the selected application locale.

    Spotify's full page payload localizes artist display names, while the
    public embed payload may retain a market-neutral Latin representation.
    The locale therefore has to be explicit before metadata is emitted.
    """
    locale = "he-IL" if str(locale).lower().startswith("he") else "en-US"
    return {
        "user_agent": _USER_AGENT,
        "locale": locale,
        "extra_http_headers": {
            "Accept-Language": (
                "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7"
                if locale == "he-IL"
                else "en-US,en;q=0.9"
            ),
        },
        **kwargs,
    }


def _read_spotify_artist_credits(artist_links, page=None) -> list[str]:
    """Read the ordered credits from one track row after hydration settles.

    Spotify can insert all artist anchors before their text nodes are hydrated.
    Reading that transient state produced values such as ``"אודיה,"`` when a
    collaborator anchor was present but still empty.  Retry only that narrow
    row-scoped condition for a bounded 600 ms, then return the usable credits;
    downstream metadata validation remains authoritative for malformed input.
    """
    links = list(artist_links or [])
    for attempt in range(5):
        names: list[str] = []
        missing = False
        seen: set[str] = set()
        for link in links:
            name = ""
            for reader_name in ("inner_text", "text_content"):
                try:
                    value = getattr(link, reader_name)()
                except Exception:
                    value = ""
                name = str(value or "").strip()
                if name:
                    break
            if not name:
                missing = True
                continue
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(name)
        if not missing or page is None or attempt == 4:
            return names
        try:
            page.wait_for_timeout(150)
        except Exception:
            return names
    return []


def _validated_spotify_display_metadata(
    title: str, artist_credits: list[str],
) -> tuple[str, str, list[str]]:
    """Validate Spotify-owned display metadata without generic title cleanup.

    ``clean_title_and_artist`` is intended for noisy video titles.  Applying it
    to authoritative Spotify credits removed Latin collaborators from mixed-
    script rows (``"אודיה, Shir Koren"`` became ``"אודיה,"``).
    """
    from utils.spotify_resolver import validate_spotify_track_metadata

    clean_title, clean_credits = validate_spotify_track_metadata(
        title, artist_credits,
    )
    return clean_title, ", ".join(clean_credits), clean_credits


def _block_heavy_resources(route):
    """
    Aborts requests for heavy media while keeping CSS and XHR/Scripts active.
    This is essential for high-speed reliable scraping in modern SPAs.
    """
    if route.request.resource_type in ["font", "media"]:
        route.abort()
    elif route.request.resource_type == "image":
        # Always allow Spotify images to ensure we get metadata thumbnails
        if "i.scdn.co" in route.request.url:
            route.continue_()
        else:
            route.abort()
    else:
        route.continue_()
def _ensure_high_res_spotify_image(url: str) -> str:
    """
    Ensures Spotify image URLs point to the highest resolution (640x640).
    Spotify uses 00004851 for 64x64, 00001e02 for 300x300, and 0000b273 for 640x640.
    """
    if not url or "i.scdn.co/image" not in url:
        return url
    # Replace size codes with b273 (640x640)
    # Common codes: 4851, 1e02, b273
    url = re.sub(r"/image/ab67616d0000[a-f0-9]{4}", "/image/ab67616d0000b273", url)
    return url
def _scrape_standard_ydl(url: str, platform_label: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Generic internal wrapper for yt-dlp based extraction."""
    items = []
    ydl_opts = _build_parse_ydl_opts(logger=_SilentLogger())

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info: return "Unknown", []

        # Save playlist/album title BEFORE the loop — the entry loop must not overwrite it
        raw_title = info.get("title") or info.get("playlist_title") or "Unknown"
        # Strip YouTube Music's "Album - " prefix that yt-dlp returns verbatim
        playlist_title = re.sub(r"^Album\s*-\s*", "", raw_title, flags=re.IGNORECASE).strip() if platform_label == "ytmusic" else raw_title
        entries = info.get("entries") or [info]

        for idx, entry in enumerate(entries, 1):
            if not entry: continue
            artist = entry.get("artist") or entry.get("uploader") or entry.get("creator") or ""
            track_title = entry.get("title") or entry.get("fulltitle") or f"Item {idx}"

            # YTM fallback: resolve via ytmusicapi search + duration filter for accuracy
            if platform_label == "ytmusic":
                target_url = _resolve_to_ytm_url(track_title, artist, entry.get("duration") or 0)
            else:
                target_url = entry.get("webpage_url") or entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"

            track_dict = {
                "title": track_title,
                "artist": artist,
                "album": playlist_title,
                "url": target_url,
                "thumbnail_url": _scraper_best_thumbnail(entry) or "",
                "duration_sec": entry.get("duration"),
                "platform": platform_label,
                "album_index": entry.get("playlist_index") or idx
            }
            items.append(track_dict)
            if on_item: on_item(track_dict)

    return playlist_title, items
def _scrape_spotify_grid_on_page(page: Page, url: str, content_type_label: str, on_item: Optional[Callable[[Dict], None]] = None, cancel_check: Optional[Callable[[], bool]] = None, metadata_only: bool = False) -> Tuple[str, List[Dict]]:
    """
    CORE LOGIC: Scrape a Spotify grid on a PRE-INITIALIZED page.
    Handles virtualized lists by scrolling.  ``cancel_check`` (if given) is
    polled each scroll pass so a user cancel stops the scroll promptly.

    When ``metadata_only`` is set, each track is published via ``on_item`` the
    moment it is discovered (progressive stage-1 catalog) instead of being
    collected for a post-scrape resolve pass.
    """
    items = []
    seen = set()
    page.goto(url, wait_until="load", timeout=30000)

    # Try embedded JSON extraction fallback first (more resilient, faster)
    try:
        html = page.content()
        parsed = _parse_spotify_json_fallback(html, content_type_label)
        if parsed:
            logger.info(f"[SpotifyScraper] Parsed {content_type_label} tracks using JSON fallback successfully.")
            if metadata_only and on_item:
                for item in parsed[1]:
                    _emit_pending_track(item, on_item)
            return parsed
    except Exception as exc:
        logger.debug(f"[SpotifyScraper] JSON fallback failed for {url}: {exc}")

    is_album = content_type_label == "Album"
    try:
        # Wait for grid or track rows
        page.wait_for_selector("main div[role='grid'], main div[data-testid='tracklist-row']", timeout=15000)

        # Get title from entity header
        scraped_title = page.evaluate("() => document.querySelector('h1[data-testid=\"entityTitle\"], main h1')?.innerText") or f"Unknown Spotify {content_type_label}"

        # Get higher-res entity image from header (Album/Playlist cover)
        header_thumb = ""
        try:
            h_img = page.locator("main img[data-testid='entity-image'], main img").first
            if h_img.count():
                header_thumb = _ensure_high_res_spotify_image(h_img.get_attribute("src") or "")
        except: pass
        # Isolate main grid
        main_grid = page.locator("main div[role='grid'], main div[data-testid='track-list']").first
        stagnant_count = 0
        while stagnant_count < 6:
            if cancel_check and cancel_check(): break
            tracks = main_grid.locator("div[data-testid='tracklist-row']").all()
            if not tracks: break
            added_in_pass = 0
            for track_row in tracks:
                try:
                    row_idx = track_row.get_attribute("aria-rowindex") or track_row.get_attribute("data-testid")
                    track_link = track_row.locator("a[data-testid='internal-track-link']").first
                    title_el = track_link.locator("div").first
                    if not title_el.count(): title_el = track_row.locator("div[dir='auto']").first
                    track_title = title_el.inner_text().strip()
                    # Capture the stable Spotify track id from the row's own
                    # link so it can key the match cache (falls back to "").
                    track_href = ""
                    try:
                        if track_link.count():
                            track_href = track_link.get_attribute("href") or ""
                    except: pass
                    track_spotify_id = _spotify_id_from_url(track_href)
                    uid = f"{row_idx}_{track_title}"
                    if uid in seen: continue
                    seen.add(uid)
                    added_in_pass += 1

                    artist_links = track_row.locator("a[href*='/artist/']").all()
                    artist_names = _read_spotify_artist_credits(artist_links, page)
                    track_title, artists, artist_names = _validated_spotify_display_metadata(
                        track_title, artist_names,
                    )

                    # Extract thumbnail from row
                    track_thumb = ""
                    try:
                        row_img = track_row.locator("img").first
                        if row_img.count():
                            track_thumb = _ensure_high_res_spotify_image(row_img.get_attribute("src") or "")
                    except: pass

                    # For ALBUMS, we ALWAYS prefer the header/album cover over single-track covers
                    final_thumb = track_thumb
                    if is_album and header_thumb:
                        final_thumb = header_thumb
                    elif not final_thumb:
                        final_thumb = header_thumb

                    duration_sec = 0
                    duration_str = ""
                    try:
                        # Find element matching M:SS or HH:MM:SS
                        dur_el = track_row.locator("div, span").filter(has_text=re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")).first
                        if dur_el.count():
                            duration_str = dur_el.inner_text().strip()
                            parts = [int(p) for p in duration_str.split(":")]
                            if len(parts) == 2:
                                duration_sec = parts[0] * 60 + parts[1]
                            elif len(parts) == 3:
                                duration_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                    except: pass
                    track_dict = {
                        "title": track_title, "artist": artists, "album": scraped_title,
                        "url": "",  # resolved in parallel after browser closes
                        "album_index": len(seen), "thumbnail_url": final_thumb,
                        "duration_sec": duration_sec, "duration_str": duration_str or "??:??",
                        "platform": "spotify", "release_type": content_type_label.lower(),
                        "spotify_id": track_spotify_id,
                        "spotify_url": (
                            f"https://open.spotify.com/track/{track_spotify_id}"
                            if track_spotify_id else ""
                        ),
                    }
                    items.append(track_dict)
                    # Stage 1: publish this track immediately so the catalog
                    # fills the UI as it is scraped, not after the whole scroll.
                    if metadata_only:
                        _emit_pending_track(track_dict, on_item)
                except: pass

            if added_in_pass == 0: stagnant_count += 1
            else: stagnant_count = 0
            try:
                tracks[-1].scroll_into_view_if_needed()
                page.wait_for_timeout(500)
            except: break

        # Back-fill total_tracks now that we know the full count (used by EP grouping)
        if items:
            total = len(items)
            for td in items:
                td["total_tracks"] = total
    except Exception as e:
        logger.error(f"Error in _scrape_spotify_grid_on_page for {url}: {e}")
        return "Unknown", []
    return scraped_title, items
# ── Spotify Isolated Functions ────────────────────────────────────────────────
def scrape_spotify_playlist(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cookies_file: Optional[str] = None,
    metadata_only: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
    locale: str = "en-US",
) -> Tuple[str, List[Dict]]:
    """Dedicated entry for Spotify Playlists.

    When ``metadata_only`` is set the tracks are published immediately with
    metadata only and the YouTube match is deferred to download time.
    """
    sync_playwright = _sync_playwright_for("Spotify playlist scraping")
    title, items = "Unknown Playlist", []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(**_spotify_context_kwargs(locale))
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)
        try:
            title, items = _scrape_spotify_grid_on_page(
                page, url, "Playlist",
                on_item=on_item, cancel_check=cancel_check, metadata_only=metadata_only,
            )
        finally:
            browser.close()
    # metadata_only already published each track progressively inside the scrape.
    if not metadata_only:
        _parallel_resolve_urls(items, on_item, cookies_file=cookies_file)
    return title, items
def scrape_spotify_album(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cookies_file: Optional[str] = None,
    metadata_only: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
    locale: str = "en-US",
) -> Tuple[str, List[Dict]]:
    """Dedicated entry for Spotify Albums.

    When ``metadata_only`` is set the tracks are published immediately with
    metadata only and the YouTube match is deferred to download time.
    """
    sync_playwright = _sync_playwright_for("Spotify album scraping")
    title, items = "Unknown Album", []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(**_spotify_context_kwargs(locale))
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)
        try:
            title, items = _scrape_spotify_grid_on_page(
                page, url, "Album",
                on_item=on_item, cancel_check=cancel_check, metadata_only=metadata_only,
            )
        finally:
            browser.close()
    # metadata_only already published each track progressively inside the scrape.
    if not metadata_only:
        _parallel_resolve_urls(items, on_item, cookies_file=cookies_file)
    return title, items


def _spotify_metadata_invalid_item(
    url: str, reason: str, *, title: str = "Spotify track",
) -> Dict:
    """Build a visible but non-downloadable row for untrustworthy metadata."""
    return {
        "title": title or "Spotify track",
        "artist": "",
        "album": title or "Spotify track",
        "url": "",
        "platform": "spotify",
        "release_type": "single",
        "category": "סינגלים ו-EP",
        "total_tracks": 1,
        "spotify_id": _spotify_id_from_url(url),
        "spotify_url": url,
        "match_status": "metadata_invalid",
        "resolution_error": "spotify_metadata_invalid_card",
        "metadata_error": reason,
    }


def scrape_spotify_track(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cookies_file: Optional[str] = None,
    locale: str = "en-US",
) -> Tuple[str, List[Dict]]:
    """Extract one Spotify track, structured-first and match it lazily.

    Locale-aware structured page data is preferred, followed by exact
    track-scoped embed JSON. The DOM fallback is limited to the entity header
    and ``track-artist-link-card`` credits; it never scans all artist links in
    ``main`` where recommendations and discography sections also live.
    """
    del cookies_file  # Spotify page metadata is public; YouTube cookies are used later.
    from core.match_errors import SpotifyMetadataInvalid
    from utils.spotify_resolver import SpotifyResolver, validate_spotify_track_metadata

    spotify_id = _spotify_id_from_url(url)
    title = "Unknown Track"
    items: List[Dict] = []

    # Spotify's full page initialState is both structured and locale-aware.
    # Prefer it for Hebrew display metadata and constrain traversal to the
    # requested track id; the track-scoped embed remains the resilient
    # credential-free fallback when that payload is unavailable.
    if str(locale).lower().startswith("he"):
        try:
            html = SpotifyResolver._localized_page_html("track", spotify_id, locale)
            parsed = _parse_spotify_json_fallback(
                html, "Track", expected_spotify_id=spotify_id,
            )
            if parsed:
                title, items = parsed
                items = items[:1]
                logger.info(
                    "[SpotifyScraper] Parsed localized track %s from scoped "
                    "initialState JSON", spotify_id,
                )
        except SpotifyMetadataInvalid as exc:
            logger.warning(
                "[SpotifyScraper] Invalid localized metadata for track %s: %s",
                spotify_id, exc,
            )
            items = [_spotify_metadata_invalid_item(url, str(exc), title=title)]
        except Exception as exc:
            logger.debug("[SpotifyScraper] Localized track metadata unavailable: %s", exc)

    # Exact, credential-free Spotify embed JSON fallback.
    if not items:
        try:
            rows = SpotifyResolver._embed_fallback("track", spotify_id, locale=locale)
            if not rows:
                raise RuntimeError("Spotify embed returned no track")
            metadata = dict(rows[0])
            title, artist, artist_names = _validated_spotify_display_metadata(
                metadata.get("title"), metadata.get("artist_credits") or [metadata.get("artist", "")]
            )
            metadata.update({
                "title": title,
                "artist": artist,
                "album": title,
                "parent_artist": artist_names[0],
                "category": "סינגלים ו-EP",
                "release_type": "single",
                "total_tracks": 1,
                "platform": "spotify",
                "spotify_id": spotify_id,
                "spotify_url": url,
                "url": "",
            })
            items = [metadata]
            logger.info("[SpotifyScraper] Parsed track %s from scoped embed JSON", spotify_id)
        except SpotifyMetadataInvalid as exc:
            logger.warning("[SpotifyScraper] Invalid structured metadata for track %s: %s", spotify_id, exc)
            items = [_spotify_metadata_invalid_item(url, str(exc), title=title)]
        except Exception as exc:
            logger.debug("[SpotifyScraper] Structured track metadata unavailable: %s", exc)

    # Narrowly scoped DOM fallback only when structured data was unavailable,
    # never when the structured source was present but clearly malformed.
    if not items:
        sync_playwright = _sync_playwright_for("Spotify track scraping")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(**_spotify_context_kwargs(locale))
            page = context.new_page()
            page.route("**/*", _block_heavy_resources)
            try:
                page.goto(url, wait_until="load", timeout=30000)
                try:
                    html = page.content()
                    parsed = _parse_spotify_json_fallback(
                        html, "Track", expected_spotify_id=spotify_id,
                    )
                    if parsed:
                        title, items = parsed
                        items = items[:1]
                except SpotifyMetadataInvalid as exc:
                    items = [_spotify_metadata_invalid_item(url, str(exc), title=title)]
                except Exception as exc:
                    logger.debug("[SpotifyScraper] Track page JSON unavailable: %s", exc)

                if not items:
                    page.wait_for_selector(
                        "main [data-testid='entityTitle'], main h1", timeout=12000,
                    )
                    title = page.locator(
                        "main [data-testid='entityTitle'], main h1"
                    ).first.inner_text().strip()
                    credit_links = page.locator(
                        "main [data-testid='track-artist-link-card'] a[href*='/artist/']"
                    ).all()
                    artist_names = _read_spotify_artist_credits(credit_links, page)
                    clean_title, artist, artist_names = _validated_spotify_display_metadata(
                        title, artist_names,
                    )

                    thumb_url = ""
                    img_el = page.locator("main img[data-testid='entity-image']").first
                    if img_el.count():
                        thumb_url = _ensure_high_res_spotify_image(img_el.get_attribute("src") or "")
                    items = [{
                        "title": clean_title,
                        "artist": artist,
                        "album": clean_title,
                        "parent_artist": artist_names[0],
                        "category": "סינגלים ו-EP",
                        "url": "",
                        "thumbnail_url": thumb_url,
                        "platform": "spotify",
                        "release_type": "single",
                        "total_tracks": 1,
                        "spotify_id": spotify_id,
                        "spotify_url": url,
                    }]
                    title = clean_title
            except SpotifyMetadataInvalid as exc:
                logger.warning("[SpotifyScraper] Invalid DOM metadata for track %s: %s", spotify_id, exc)
                items = [_spotify_metadata_invalid_item(url, str(exc), title=title)]
            finally:
                browser.close()

    for item in items:
        item.setdefault("category", "סינגלים ו-EP")
        item.setdefault("release_type", "single")
        item.setdefault("total_tracks", 1)
        _emit_pending_track(item, on_item)
    if items:
        title = items[0].get("title") or title
    return title, items


def _scrape_spotify_artist_legacy(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cookies_file: Optional[str] = None,
    metadata_only: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
    locale: str = "en-US",
) -> Tuple[str, List[Dict]]:
    """
    Dedicated entry for Spotify Artist discographies.
    Iterates over /album and /single URLs for categorical accuracy.

    When ``metadata_only`` is set the whole catalog is published immediately
    with metadata only and the YouTube match is deferred to download time.
    ``cancel_check`` is polled in the scroll/category loops so a user cancel
    stops the (potentially long) scrape promptly.
    """
    from utils.spotify_resolver import validate_spotify_track_metadata

    items = []
    artist_name = ""
    seen_track_uids = set()
    # Normalize URL: strip trailing slashes and common sub-paths to get the base artist URL
    artist_url = re.sub(r"/discography/.*$", "", url.rstrip("/"))
    artist_url = re.sub(r"/all/?$", "", artist_url)
    sync_playwright = _sync_playwright_for("Spotify artist discography scraping")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(**_spotify_context_kwargs(
            locale, viewport={"width": 1280, "height": 1000},
        ))
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)

        urls = [
            (artist_url.rstrip("/") + "/discography/album", "אלבומים"),
            (artist_url.rstrip("/") + "/discography/single", "סינגלים ו-EP")
        ]

        for target_url, cat_name in urls:
            if cancel_check and cancel_check(): break
            try:
                page.goto(target_url, wait_until="load", timeout=30000)

                # 1. Fetch Artist Name (only once)
                if not artist_name:
                    try:
                        header_selector = "main h1, [data-testid='artist-page-header-name'], [data-testid='artist-name']"
                        page.wait_for_selector(header_selector, timeout=8000)
                        artist_name = page.locator(header_selector).first.inner_text().strip()
                    except:
                        artist_name = page.title().split("|")[0].strip()

                    artist_name = re.sub(r"^Spotify\s*[-–]\s*", "", artist_name, flags=re.IGNORECASE)
                    artist_name = re.sub(r"\s*[-–]\s*(דיסקוגraphic|Discography)\s*$", "", artist_name, flags=re.IGNORECASE)
                    artist_name = re.sub(r"\s*[-–]\s*Discography.*$", "", artist_name, flags=re.IGNORECASE)
                    artist_name = re.sub(r"\s*\(Discography\).*$", "", artist_name, flags=re.IGNORECASE)
                    artist_name = artist_name.strip()
                # 2. Toggle to List View
                try:
                    view_toggle = page.locator("[aria-controls='sort-and-view-picker']").first
                    if view_toggle.count():
                        view_toggle.click()
                        list_option = page.locator("button[role='menuitemradio'] >> text=/List|רשימה/").first
                        if list_option.count():
                            list_option.click()
                            page.wait_for_timeout(800)
                except: pass
                # 3. Targeted Accumulation Loop for this URL
                stagnant_count = 0
                try: page.wait_for_selector("main div[data-testid='track-list']", timeout=12000)
                except: pass

                while stagnant_count < 10:
                    if cancel_check and cancel_check(): break
                    grids = page.locator("main div[data-testid='track-list']").all()
                    if not grids: break
                    added_any = False
                    last_track = None
                    for grid in grids:
                        release_title = grid.get_attribute("aria-label") or ""
                        if not release_title or release_title == artist_name:
                             release_title = grid.evaluate("el => el.previousElementSibling?.innerText") or release_title
                        if not release_title: release_title = "Unknown Release"
                        visible_tracks = grid.locator("div[data-testid='tracklist-row']").all()
                        if not visible_tracks: continue

                        # Metadata for this grid
                        try:
                            thumb_url = grid.evaluate("""el => {
                                let curr = el;
                                for(let i=0; i<6; i++) {
                                    if(!curr) break;
                                    let img = curr.querySelector('img');
                                    if(img) return img.src;
                                    let sibling = curr.previousElementSibling;
                                    while(sibling) {
                                        let sibImg = sibling.querySelector('img') || (sibling.tagName === 'IMG' ? sibling : null);
                                        if(sibImg) return sibImg.src;
                                        sibling = sibling.previousElementSibling;
                                    }
                                    curr = curr.parentElement;
                                }
                                return "";
                            }""") or ""
                            rel_container = grid.evaluate_handle("el => el.closest('div:has(h1), div:has(h2), div:has(img), [class*=\"contentSpacing\"]') || el.parentElement")
                            # Release label and track count (e.g. "אלבום • 2023 • 16 שירים")
                            # We collect all text from the header container to ensure we find the song count
                            meta_str = rel_container.evaluate("el => el.innerText || ''")
                            m_low = meta_str.lower()

                            # Extract REAL track count from metadata string (e.g. "16 שירים" or "3 songs")
                            track_count_match = re.search(r"(\d+)\s*(שיר|שירים|song|track)", m_low)
                            total_tracks_stable = int(track_count_match.group(1)) if track_count_match else len(visible_tracks)
                            # Final site_label logic
                            site_label = "album" if "album" in target_url else \
                                         "ep" if ("ep" in m_low or total_tracks_stable > 1) else \
                                         "single" if ("single" in m_low or "single" in target_url) else "release"
                        except:
                            site_label = "album" if "album" in target_url else "release"
                            thumb_url = ""
                            total_tracks_stable = len(visible_tracks)
                        for t_idx, track_row in enumerate(visible_tracks, 1):
                            last_track = track_row
                            try:
                                track_link = track_row.locator("a[data-testid='internal-track-link']").first
                                title_el = track_link.locator("div").first
                                if not title_el.count(): title_el = track_row.locator("div[dir='auto']").first
                                track_title = title_el.inner_text().strip()
                                # Stable Spotify track id from the row's own link,
                                # for keying the match cache (falls back to "").
                                track_href = ""
                                try:
                                    if track_link.count():
                                        track_href = track_link.get_attribute("href") or ""
                                except: pass
                                track_spotify_id = _spotify_id_from_url(track_href)

                                uid = f"{cat_name}_{release_title}_{track_title}"
                                if uid in seen_track_uids: continue
                                seen_track_uids.add(uid)
                                added_any = True

                                artist_links = track_row.locator("a[href*='/artist/']").all()
                                artist_names = _read_spotify_artist_credits(
                                    artist_links, page,
                                )
                                track_title, artists, artist_names = _validated_spotify_display_metadata(
                                    track_title, artist_names or [artist_name],
                                )
                                duration_sec, duration_str = 0, ""
                                try:
                                    dur_el = track_row.locator("[data-testid='track-duration']").first
                                    if not dur_el.count():
                                        dur_el = track_row.locator("div, span").filter(has_text=re.compile(r"^\d{1,2}:\d{2}$")).first
                                    if dur_el.count():
                                        duration_str = dur_el.inner_text().strip()
                                        if ":" in duration_str:
                                            parts = [int(p) for p in duration_str.split(":")]
                                            duration_sec = parts[0]*60 + parts[1] if len(parts)==2 else parts[0]*3600 + parts[1]*60 + parts[2]
                                except: pass
                                try:
                                    row_img = track_row.locator("img").first
                                    final_thumb = _ensure_high_res_spotify_image(row_img.get_attribute("src")) if row_img.count() else thumb_url
                                except: final_thumb = thumb_url

                                # Final sweep for safety
                                final_thumb = _ensure_high_res_spotify_image(final_thumb)
                                track_dict = {
                                    "title": track_title, "artist": artists, "album": release_title,
                                    "parent_artist": artist_name, "category": cat_name,
                                    "release_type": site_label, "album_index": t_idx,
                                    "total_tracks": total_tracks_stable,
                                    "url": "",  # resolved in parallel after browser closes
                                    "platform": "spotify", "thumbnail_url": final_thumb,
                                    "duration_sec": duration_sec, "duration_str": duration_str or "??:??",
                                    "spotify_id": track_spotify_id,
                                    "spotify_url": (
                                        f"https://open.spotify.com/track/{track_spotify_id}"
                                        if track_spotify_id else ""
                                    ),
                                }
                                items.append(track_dict)
                                # Stage 1: publish each track the moment it is
                                # scraped so a large discography fills the UI
                                # progressively, not only after the full scroll.
                                if metadata_only:
                                    _emit_pending_track(track_dict, on_item)
                            except: pass
                    if added_any: stagnant_count = 0
                    else: stagnant_count += 1

                    if last_track:
                        last_track.scroll_into_view_if_needed()
                        page.wait_for_timeout(600)
            except Exception as e:
                logger.error(f"Error scraping {target_url}: {e}")

        browser.close()

    # metadata_only already published each track progressively inside the scrape.
    if not metadata_only:
        _parallel_resolve_urls(items, on_item, cookies_file=cookies_file)
    return artist_name or "Unknown Artist", items


_SPOTIFY_SECTION_NAMES = {
    "all": ("All", "הכול"),
    "album": ("Albums", "אלבומים"),
    "single": ("Singles and EPs", "סינגלים ו-EP"),
    "compilation": ("Compilations", "אוספים"),
    "appears_on": ("Appears On", "מופיע ב"),
}

_SPOTIFY_CATEGORY_NAMES = {
    "all": "דיסקוגרפיה",
    "album": "אלבומים",
    "single": "סינגלים ו-EP",
    "ep": "סינגלים ו-EP",
    "compilation": "אוספים",
    "appears_on": "מופיע באוספים",
}


def _spotify_artist_base_url(url: str) -> str:
    """Return the canonical public artist URL for any discography sub-route."""
    match = re.search(r"https?://open\.spotify\.com/artist/[^/?#]+", url)
    return match.group(0) if match else re.sub(r"/discography/.*$", "", url.rstrip("/"))


def _spotify_section_key(label: str) -> str:
    normalized = re.sub(r"\s+", " ", str(label or "")).strip().casefold()
    aliases = {
        "all": "all", "הכול": "all", "הכל": "all",
        "albums": "album", "album": "album", "אלבומים": "album",
        "singles and eps": "single", "singles & eps": "single",
        "singles": "single", "סינגלים ו-ep": "single", "סינגלים": "single",
        "compilations": "compilation", "compilation": "compilation",
        "אוספים": "compilation",
        "appears on": "appears_on", "מופיע ב": "appears_on",
    }
    return aliases.get(normalized, "")


def _spotify_artist_name_from_page(page) -> str:
    selector = (
        "main h1, [data-testid='artist-page-header-name'], "
        "[data-testid='artist-name']"
    )
    try:
        if page.locator(selector).count():
            return page.locator(selector).first.inner_text().strip()
    except Exception:
        pass
    try:
        title = page.title().split("|")[0].strip()
        title = re.sub(r"^Spotify\s*[-–]\s*", "", title, flags=re.IGNORECASE)
        return re.sub(r"\s*[-–]\s*Discography.*$", "", title, flags=re.IGNORECASE).strip()
    except Exception:
        return ""


def _click_spotify_link(page, href_fragment: str) -> bool:
    link = page.locator(f"main a[href*='{href_fragment}']").first
    try:
        link.wait_for(state="attached", timeout=12000)
    except Exception:
        return False
    link.click()
    page.wait_for_timeout(1000)
    return True


def _open_spotify_discography(page, artist_url: str) -> bool:
    page.goto(artist_url, wait_until="load", timeout=30000)
    try:
        page.wait_for_selector("main h1", timeout=12000)
    except Exception:
        pass
    return _click_spotify_link(page, "/discography/")


def _spotify_discography_filters(page) -> list[tuple[str, str]]:
    """Read the currently available filter options from Spotify's combobox."""
    combo = page.locator("button[role='combobox']").first
    if not combo.count():
        return []
    combo.click()
    page.wait_for_timeout(250)
    result: list[tuple[str, str]] = []
    for option in page.locator("[role='option'], [role='menuitemradio']").all():
        try:
            label = option.inner_text().strip()
        except Exception:
            continue
        key = _spotify_section_key(label)
        if key and (key, label) not in result:
            result.append((key, label))
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return result


def discover_spotify_artist_sections(
    url: str,
    *,
    locale: str = "en-US",
) -> tuple[str, list[dict]]:
    """Discover only the category tabs Spotify exposes for this artist."""
    artist_url = _spotify_artist_base_url(url)
    sync_playwright = _sync_playwright_for("Spotify artist category discovery")
    artist_name = ""
    sections: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(**_spotify_context_kwargs(
            locale, viewport={"width": 1280, "height": 1000},
        ))
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)
        try:
            page.goto(artist_url, wait_until="load", timeout=30000)
            try:
                page.wait_for_selector("main h1", timeout=12000)
            except Exception:
                pass
            artist_name = _spotify_artist_name_from_page(page)
            has_appears_on = bool(
                page.locator("main a[href*='/appears-on']").count()
            )
            if _click_spotify_link(page, "/discography/"):
                filters = _spotify_discography_filters(page)
                specific = [(key, label) for key, label in filters if key != "all"]
                visible_filters = specific or filters
                if not visible_filters:
                    visible_filters = [(
                        "all",
                        _SPOTIFY_SECTION_NAMES["all"][
                            1 if str(locale).lower().startswith("he") else 0
                        ],
                    )]
                for key, label in visible_filters:
                    sections.append({
                        "key": key,
                        "provider_label": label,
                        "item_count": -1,
                    })
            if has_appears_on:
                label = _SPOTIFY_SECTION_NAMES["appears_on"][
                    1 if str(locale).lower().startswith("he") else 0
                ]
                sections.append({
                    "key": "appears_on",
                    "provider_label": label,
                    "item_count": -1,
                })
        finally:
            browser.close()
    return artist_name or "Unknown Artist", sections


def _select_spotify_filter(page, provider_label: str) -> bool:
    combo = page.locator("button[role='combobox']").first
    if not combo.count():
        return False
    combo.click()
    page.wait_for_timeout(200)
    options = page.locator("[role='option'], [role='menuitemradio']")
    for option in options.all():
        try:
            if option.inner_text().strip() == provider_label.strip():
                option.click()
                page.wait_for_timeout(700)
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _spotify_release_type(section_key: str, metadata: str, total_tracks: int) -> str:
    lower = str(metadata or "").casefold()
    explicit_labels = (
        ("compilation", r"(?:^|\n)\s*(?:compilation|אוסף)\s*(?:[·•|]|$)"),
        ("album", r"(?:^|\n)\s*(?:album|אלבום)\s*(?:[·•|]|$)"),
        ("single", r"(?:^|\n)\s*(?:single|סינגל)\s*(?:[·•|]|$)"),
        ("ep", r"(?:^|\n)\s*ep\s*(?:[·•|]|$)"),
    )
    for release_type, pattern in explicit_labels:
        if re.search(pattern, lower):
            return release_type
    if section_key == "compilation":
        return "compilation"
    if section_key == "album":
        return "album"
    if section_key in {"single", "all"}:
        return "ep" if "ep" in lower or total_tracks > 1 else "single"
    return section_key


def _spotify_release_id_from_grid(grid) -> str:
    """Read the closest release link that owns one Spotify track-list grid."""
    try:
        href = grid.evaluate(
            """el => {
                let node = el.parentElement;
                while (node && node.tagName !== 'MAIN') {
                    const links = [...node.querySelectorAll('a[href*="/album/"]')]
                        .map(link => link.getAttribute('href'))
                        .filter(Boolean);
                    const unique = [...new Set(links)];
                    if (unique.length === 1) return unique[0];
                    if (unique.length > 1) return '';
                    node = node.parentElement;
                }
                return '';
            }"""
        ) or ""
    except Exception:
        return ""
    return _spotify_album_id_from_url(href)


def _spotify_release_occurrence_key(
    *,
    section_key: str,
    release_id: str,
    release_title: str,
    spotify_id: str,
    position: int,
    track_title: str,
) -> tuple[str, int, str]:
    """Identify one placement without merging legitimate repeated tracks."""
    if release_id:
        release_scope = f"id:{release_id}"
    else:
        normalized_release = re.sub(
            r"\s+", " ", str(release_title or "").strip().casefold(),
        )
        release_scope = f"fallback:{section_key}:{normalized_release}"
    normalized_track = re.sub(
        r"\s+", " ", str(track_title or "").strip().casefold(),
    )
    return release_scope, int(position or 0), spotify_id or normalized_track


def _spotify_album_position(item: dict, fallback: int) -> int:
    """Keep the original album position after filtering an embedded release."""
    try:
        position = int(item.get("album_index") or fallback)
    except (TypeError, ValueError):
        return fallback
    return position if position > 0 else fallback


def _spotify_duration_text(duration_sec: object) -> str:
    try:
        seconds = int(duration_sec or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}"
        if hours else f"{minutes}:{seconds:02d}"
    )


def _hydrate_spotify_artist_release_metadata(
    items: list[dict],
    *,
    locale: str,
    cancel_check: Optional[Callable[[], bool]] = None,
    max_workers: int = 5,
) -> None:
    """Fill Spotify artwork and duration from release-scoped embed data.

    Artist discography grids currently expose stable track/release IDs but do
    not render cover art or duration in their row DOM.  Public release embeds
    contain both.  Fetch each selected stable release once, in a bounded pool,
    then join by track ID (or release position as a conservative fallback).
    Existing non-empty metadata is never replaced.
    """
    groups: dict[str, list[dict]] = {}
    for item in items:
        release_id = str(item.get("source_release_id") or "").strip()
        needs_artwork = not str(item.get("thumbnail_url") or "").strip()
        try:
            needs_duration = int(item.get("duration_sec") or 0) <= 0
        except (TypeError, ValueError):
            needs_duration = True
        if release_id and (needs_artwork or needs_duration):
            groups.setdefault(release_id, []).append(item)
    if not groups or (cancel_check and cancel_check()):
        return

    from concurrent.futures import ThreadPoolExecutor
    from utils.spotify_resolver import SpotifyResolver

    worker_count = max(1, min(int(max_workers or 1), len(groups)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            release_id: pool.submit(
                SpotifyResolver._embed_fallback,
                "album",
                release_id,
                locale=locale,
            )
            for release_id in groups
        }
        for release_id, release_items in groups.items():
            if cancel_check and cancel_check():
                for future in futures.values():
                    future.cancel()
                break
            try:
                metadata_rows = futures[release_id].result()
            except Exception as exc:
                logger.warning(
                    "Spotify release metadata %s failed: %s", release_id, exc,
                )
                continue

            by_track_id = {
                str(row.get("spotify_id") or "").strip(): row
                for row in metadata_rows
                if str(row.get("spotify_id") or "").strip()
            }
            by_position = {
                _spotify_album_position(row, position): row
                for position, row in enumerate(metadata_rows, start=1)
            }
            for item in release_items:
                spotify_id = str(item.get("spotify_id") or "").strip()
                position = _spotify_album_position(item, 0)
                metadata = by_track_id.get(spotify_id) or by_position.get(position)
                if not metadata:
                    continue
                if not item.get("thumbnail_url") and metadata.get("thumbnail_url"):
                    item["thumbnail_url"] = _ensure_high_res_spotify_image(
                        str(metadata["thumbnail_url"]),
                    )
                try:
                    current_duration = int(item.get("duration_sec") or 0)
                except (TypeError, ValueError):
                    current_duration = 0
                try:
                    provider_duration = int(metadata.get("duration_sec") or 0)
                except (TypeError, ValueError):
                    provider_duration = 0
                if current_duration <= 0 and provider_duration > 0:
                    item["duration_sec"] = provider_duration
                    item["duration_str"] = _spotify_duration_text(provider_duration)


class _SpotifyArtistReleaseRegistry:
    """Canonicalize stable releases across artist discovery roles.

    A complete release is expanded only under its first selected role.  An
    incomplete release may be revisited under another role so a transient or
    virtualized partial grid cannot turn deduplication into missing tracks.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    @staticmethod
    def _sync_roles(entry: dict) -> None:
        roles = list(entry["discovery_roles"])
        for item in entry["items"]:
            item["discovery_roles"] = list(roles)

    def begin(
        self,
        release_id: str,
        discovery_role: str,
    ) -> tuple[bool, str, list[str]]:
        """Return whether to scan plus canonical section and known roles."""
        stable_id = str(release_id or "").strip()
        if not stable_id:
            return True, discovery_role, [discovery_role]
        entry = self._entries.get(stable_id)
        if entry is None:
            return True, discovery_role, [discovery_role]

        if discovery_role not in entry["discovery_roles"]:
            entry["discovery_roles"].append(discovery_role)
            self._sync_roles(entry)
        expected = int(entry["expected_total"] or 0)
        complete = expected > 0 and len(entry["items"]) >= expected
        should_scan = entry["canonical_section"] == discovery_role or not complete
        return (
            should_scan,
            str(entry["canonical_section"]),
            list(entry["discovery_roles"]),
        )

    def commit(
        self,
        release_id: str,
        canonical_section: str,
        discovery_roles: list[str],
        items: list[dict],
        *,
        expected_total: int = 0,
    ) -> None:
        stable_id = str(release_id or "").strip()
        if not stable_id:
            return
        entry = self._entries.get(stable_id)
        if entry is None:
            if not items:
                return
            entry = {
                "canonical_section": canonical_section,
                "discovery_roles": [],
                "items": [],
                "expected_total": 0,
            }
            self._entries[stable_id] = entry
        for role in discovery_roles:
            if role not in entry["discovery_roles"]:
                entry["discovery_roles"].append(role)
        entry["items"].extend(items)
        entry["expected_total"] = max(
            int(entry["expected_total"] or 0), int(expected_total or 0),
        )
        self._sync_roles(entry)


def _collect_spotify_discography_tracks(
    page,
    *,
    artist_name: str,
    section_key: str,
    seen_occurrences: set[tuple[str, int, str]],
    release_registry: _SpotifyArtistReleaseRegistry,
    cancel_check: Optional[Callable[[], bool]],
) -> list[dict]:
    """Collect grids while canonicalizing stable releases across sections."""
    items: list[dict] = []
    stagnant_count = 0
    try:
        page.wait_for_selector("main div[data-testid='track-list']", timeout=12000)
    except Exception:
        pass

    while stagnant_count < 8:
        if cancel_check and cancel_check():
            break
        grids = page.locator("main div[data-testid='track-list']").all()
        if not grids:
            break
        added_any = False
        last_track = None
        for grid in grids:
            release_title = grid.get_attribute("aria-label") or ""
            release_id = _spotify_release_id_from_grid(grid)
            if not release_title or release_title == artist_name:
                try:
                    release_title = grid.evaluate(
                        "el => el.previousElementSibling?.innerText || ''"
                    ) or release_title
                except Exception:
                    pass
            release_title = release_title or "Unknown Release"
            rows = grid.locator("div[data-testid='tracklist-row']").all()
            if not rows:
                continue
            last_track = rows[-1]
            should_scan, canonical_section, discovery_roles = release_registry.begin(
                release_id, section_key,
            )
            if not should_scan:
                continue
            try:
                container = grid.evaluate_handle(
                    "el => el.closest('div:has(h1), div:has(h2), div:has(img), "
                    "[class*=\"contentSpacing\"]') || el.parentElement"
                )
                metadata = container.evaluate("el => el.innerText || ''")
                count_match = re.search(
                    r"(\d+)\s*(שיר|שירים|song|songs|track|tracks)",
                    metadata.casefold(),
                )
                total_tracks = int(count_match.group(1)) if count_match else len(rows)
                declared_total_tracks = (
                    int(count_match.group(1)) if count_match else 0
                )
                thumb = grid.evaluate(
                    "el => el.closest('section, div')?.querySelector('img')?.src || ''"
                ) or ""
            except Exception:
                metadata, total_tracks, declared_total_tracks, thumb = "", len(rows), 0, ""
            release_type = _spotify_release_type(
                canonical_section, metadata, total_tracks,
            )
            catalog_section = (
                "single"
                if release_type == "ep"
                else release_type
                if release_type in _SPOTIFY_CATEGORY_NAMES
                else canonical_section
            )
            category = _SPOTIFY_CATEGORY_NAMES.get(
                catalog_section, catalog_section,
            )
            if catalog_section == "all":
                category = _SPOTIFY_CATEGORY_NAMES.get(release_type, "דיסקוגרפיה")

            grid_items: list[dict] = []
            for position, row in enumerate(rows, start=1):
                last_track = row
                try:
                    link = row.locator("a[data-testid='internal-track-link']").first
                    title_el = link.locator("div").first
                    if not title_el.count():
                        title_el = row.locator("div[dir='auto']").first
                    track_title = title_el.inner_text().strip()
                    href = (link.get_attribute("href") or "") if link.count() else ""
                    spotify_id = _spotify_id_from_url(href)
                    occurrence = _spotify_release_occurrence_key(
                        section_key=canonical_section,
                        release_id=release_id,
                        release_title=release_title,
                        spotify_id=spotify_id,
                        position=position,
                        track_title=track_title,
                    )
                    if occurrence in seen_occurrences:
                        continue
                    artist_links = row.locator("a[href*='/artist/']").all()
                    artist_credits = _read_spotify_artist_credits(artist_links, page)
                    track_title, artist, artist_credits = _validated_spotify_display_metadata(
                        track_title, artist_credits or [artist_name],
                    )
                    duration_sec, duration_str = 0, ""
                    duration_el = row.locator("[data-testid='track-duration']").first
                    if duration_el.count():
                        duration_str = duration_el.inner_text().strip()
                        parts = [int(part) for part in duration_str.split(":")]
                        if len(parts) == 2:
                            duration_sec = parts[0] * 60 + parts[1]
                        elif len(parts) == 3:
                            duration_sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
                    item = {
                        "title": track_title,
                        "artist": artist,
                        "artist_credits": artist_credits,
                        "album": release_title,
                        "parent_artist": artist_name,
                        "category": category,
                        "catalog_section": catalog_section,
                        "discovery_roles": list(discovery_roles),
                        "source_release_id": release_id,
                        "release_type": release_type,
                        "album_index": position,
                        "total_tracks": total_tracks,
                        "url": "",
                        "platform": "spotify",
                        "thumbnail_url": _ensure_high_res_spotify_image(thumb),
                        "duration_sec": duration_sec,
                        "duration_str": duration_str,
                        "spotify_id": spotify_id,
                        "spotify_url": (
                            f"https://open.spotify.com/track/{spotify_id}"
                            if spotify_id else ""
                        ),
                    }
                    seen_occurrences.add(occurrence)
                    items.append(item)
                    grid_items.append(item)
                    added_any = True
                except Exception as exc:
                    logger.debug("Spotify artist row skipped: %s", exc)
            release_registry.commit(
                release_id,
                canonical_section,
                discovery_roles,
                grid_items,
                expected_total=declared_total_tracks,
            )
        stagnant_count = 0 if added_any else stagnant_count + 1
        if last_track:
            last_track.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
    return items


def _spotify_credit_matches_artist(item: dict, artist_name: str) -> bool:
    target = re.sub(r"\W+", "", artist_name, flags=re.UNICODE).casefold()
    credits = item.get("artist_credits") or re.split(
        r"\s*(?:,|&|feat\.?|ft\.?)\s*", str(item.get("artist") or ""),
        flags=re.IGNORECASE,
    )
    return any(
        re.sub(r"\W+", "", str(credit), flags=re.UNICODE).casefold() == target
        for credit in credits
    )


def _collect_spotify_appears_on(
    page,
    *,
    artist_url: str,
    artist_name: str,
    locale: str,
    seen_occurrences: set[tuple[str, int, str]],
    release_registry: _SpotifyArtistReleaseRegistry,
    cancel_check: Optional[Callable[[], bool]],
) -> list[dict]:
    """Expand Appears On release cards and retain tracks credited to the artist."""
    from utils.spotify_resolver import SpotifyResolver

    page.goto(artist_url, wait_until="load", timeout=30000)
    if not _click_spotify_link(page, "/appears-on"):
        return []
    album_ids: dict[str, str] = {}
    stagnant = 0
    while stagnant < 5:
        before = len(album_ids)
        for link in page.locator("main a[href*='/album/']").all():
            try:
                href = link.get_attribute("href") or ""
                album_id = _spotify_album_id_from_url(href)
                label = (link.get_attribute("aria-label") or link.inner_text() or "").strip()
                label = next((line.strip() for line in label.splitlines() if line.strip()), label)
                if album_id:
                    album_ids.setdefault(album_id, label or "Compilation")
            except Exception:
                continue
        stagnant = 0 if len(album_ids) > before else stagnant + 1
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(450)

    items: list[dict] = []
    for album_id, album_title in album_ids.items():
        if cancel_check and cancel_check():
            break
        should_scan, canonical_section, discovery_roles = release_registry.begin(
            album_id, "appears_on",
        )
        if not should_scan:
            continue
        try:
            release_tracks = SpotifyResolver._embed_fallback(
                "album", album_id, locale=locale,
            )
        except Exception as exc:
            logger.warning("Spotify Appears On album %s failed: %s", album_id, exc)
            continue
        matching = [
            item for item in release_tracks
            if _spotify_credit_matches_artist(item, artist_name)
        ]
        release_items: list[dict] = []
        for fallback_position, item in enumerate(matching, start=1):
            position = _spotify_album_position(item, fallback_position)
            occurrence = _spotify_release_occurrence_key(
                section_key=canonical_section,
                release_id=album_id,
                release_title=album_title,
                spotify_id=str(item.get("spotify_id") or ""),
                position=position,
                track_title=str(item.get("title") or ""),
            )
            if occurrence in seen_occurrences:
                continue
            seen_occurrences.add(occurrence)
            release_type = (
                "compilation"
                if canonical_section == "appears_on"
                else _spotify_release_type(canonical_section, "", len(matching))
            )
            category = _SPOTIFY_CATEGORY_NAMES.get(
                canonical_section, canonical_section,
            )
            if canonical_section == "all":
                category = _SPOTIFY_CATEGORY_NAMES.get(
                    release_type, "דיסקוגרפיה",
                )
            item.update({
                "album": album_title,
                "parent_artist": artist_name,
                "category": category,
                "catalog_section": canonical_section,
                "discovery_roles": list(discovery_roles),
                "source_release_id": album_id,
                "release_type": release_type,
                "album_index": position,
                "total_tracks": len(matching),
                "platform": "spotify",
                "url": "",
            })
            items.append(item)
            release_items.append(item)
        release_registry.commit(
            album_id,
            canonical_section,
            discovery_roles,
            release_items,
            expected_total=len(matching),
        )
    return items


def scrape_spotify_artist(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cookies_file: Optional[str] = None,
    metadata_only: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
    locale: str = "en-US",
    *,
    selected_sections: Optional[dict[str, str]] = None,
    on_section: Optional[Callable[[str], None]] = None,
) -> Tuple[str, List[Dict]]:
    """Scrape selected Spotify artist categories after explicit discovery."""
    discovered_artist = ""
    if selected_sections is None:
        discovered_artist, discovered_sections = discover_spotify_artist_sections(
            url, locale=locale,
        )
        selected_sections = {
            str(section["key"]): str(section.get("provider_label") or "")
            for section in discovered_sections
        }
    if not selected_sections:
        return discovered_artist or "Unknown Artist", []

    artist_url = _spotify_artist_base_url(url)
    items: list[dict] = []
    artist_name = ""
    seen_occurrences: set[tuple[str, int, str]] = set()
    release_registry = _SpotifyArtistReleaseRegistry()
    sync_playwright = _sync_playwright_for("Spotify selected artist categories")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(**_spotify_context_kwargs(
            locale, viewport={"width": 1280, "height": 1000},
        ))
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)
        try:
            page.goto(artist_url, wait_until="load", timeout=30000)
            try:
                page.wait_for_selector("main h1", timeout=12000)
            except Exception:
                pass
            artist_name = _spotify_artist_name_from_page(page) or "Unknown Artist"
            discography = [
                (key, label) for key, label in selected_sections.items()
                if key != "appears_on"
            ]
            if discography and _open_spotify_discography(page, artist_url):
                for key, label in discography:
                    if cancel_check and cancel_check():
                        break
                    if on_section:
                        on_section(key)
                    if (
                        label
                        and not _select_spotify_filter(page, label)
                        and key != "all"
                    ):
                        logger.warning("Spotify category filter not found: %s", label)
                        continue
                    items.extend(_collect_spotify_discography_tracks(
                        page,
                        artist_name=artist_name,
                        section_key=key,
                        seen_occurrences=seen_occurrences,
                        release_registry=release_registry,
                        cancel_check=cancel_check,
                    ))
            if "appears_on" in selected_sections and not (
                cancel_check and cancel_check()
            ):
                if on_section:
                    on_section("appears_on")
                items.extend(_collect_spotify_appears_on(
                    page,
                    artist_url=artist_url,
                    artist_name=artist_name,
                    locale=locale,
                    seen_occurrences=seen_occurrences,
                    release_registry=release_registry,
                    cancel_check=cancel_check,
                ))
        finally:
            browser.close()

    _hydrate_spotify_artist_release_metadata(
        items,
        locale=locale,
        cancel_check=cancel_check,
    )
    for item in items:
        _emit_pending_track(item, on_item if metadata_only else None)
    if not metadata_only:
        _parallel_resolve_urls(items, on_item, cookies_file=cookies_file)
    return artist_name or "Unknown Artist", items
# ── YouTube Music Isolated Functions ──────────────────────────────────────────
def scrape_ytm_playlist(url: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for YouTube Music Playlists/Albums using native API for 1:1 thumbnails."""
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        playlist_id = url.split("list=")[-1].split("&")[0]
        p = yt.get_playlist(playlist_id)

        title = p.get('title', 'Unknown Playlist')
        items = []
        for idx, track in enumerate(p.get('tracks', []), 1):
            if not track.get('videoId'):
                continue
            track_title = track.get('title', f"Track {idx}")
            artist = ", ".join(a['name'] for a in track.get('artists', []) if 'name' in a)
            album = track.get('album', {}).get('name', title) if track.get('album') else title

            thumb_url = ""
            if track.get('thumbnails'):
                thumb_url = track['thumbnails'][-1]['url']
                from utils.artwork_cleaner import clean_artwork_url
                thumb_url = clean_artwork_url(thumb_url, "ytmusic")

            track_dict = {
                "title": track_title,
                "artist": artist,
                "album": album,
                "url": f"https://music.youtube.com/watch?v={track['videoId']}",
                "thumbnail_url": thumb_url,
                "duration_sec": track.get('duration_seconds') or 0,
                "platform": "ytmusic",
                "album_index": idx
            }
            items.append(track_dict)
            if on_item: on_item(track_dict)
        return title, items
    except Exception as e:
        logger.error(f"[Scraper] ytmusicapi playlist failed: {e}. Falling back to yt-dlp.")
        return _scrape_standard_ydl(url, "ytmusic", on_item)

def scrape_ytm_album(url: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for YouTube Music Albums."""
    # YTM Albums use the exact same playlist endpoint logic
    return scrape_ytm_playlist(url, on_item)

def scrape_ytm_track(url: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for YTM single tracks."""
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        video_id = url.split("v=")[-1].split("&")[0]
        p = yt.get_song(video_id)

        details = p.get('videoDetails', {})
        track_title = details.get('title', 'Unknown Track')
        artist = details.get('author', 'Unknown Artist')

        thumb_url = ""
        if details.get('thumbnail', {}).get('thumbnails'):
            thumb_url = details['thumbnail']['thumbnails'][-1]['url']
            from utils.artwork_cleaner import clean_artwork_url
            thumb_url = clean_artwork_url(thumb_url, "ytmusic")

        track_dict = {
            "title": track_title,
            "artist": artist,
            "album": track_title,
            "url": f"https://music.youtube.com/watch?v={video_id}",
            "thumbnail_url": thumb_url,
            "duration_sec": int(details.get('lengthSeconds', 0)),
            "platform": "ytmusic",
            "album_index": 1
        }
        if on_item: on_item(track_dict)
        return track_title, [track_dict]
    except Exception as e:
        logger.error(f"[Scraper] ytmusicapi track failed: {e}. Falling back to yt-dlp.")
        return _scrape_standard_ydl(url, "ytmusic", on_item)
def scrape_ytm_artist(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    *,
    releases: Optional[List[Dict]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[str, List[Dict]]:
    """Dedicated entry for YTM Artist discographies."""
    from utils.ytm_scraper import fetch_ytm_artist_releases
    from concurrent.futures import ThreadPoolExecutor

    releases = list(releases) if releases is not None else fetch_ytm_artist_releases(url)
    if not releases:
        return "Unknown Artist", []
    artist_name = releases[0].get("parent_artist", "Unknown Artist")

    def _fetch_one(release: Dict) -> Tuple[Dict, List, str]:
        """Fetch tracks for one release in its own YTMusic session (thread-safe)."""
        if cancel_check and cancel_check():
            return release, [], release.get("title", "Unknown Release")
        rel_url = release.get("url", "")
        album_title = release.get("title", "Unknown Release")
        try:
            from ytmusicapi import YTMusic
            yt = YTMusic()
            if "list=" in rel_url:
                playlist_id = rel_url.split("list=")[-1].split("&")[0]
                p = yt.get_playlist(playlist_id)
                return release, p.get("tracks", []), p.get("title") or album_title
            if "v=" in rel_url:
                video_id = rel_url.split("v=")[-1].split("&")[0]
                p = yt.get_song(video_id)
                if p and p.get("videoDetails"):
                    return release, [p["videoDetails"]], p["videoDetails"].get("title") or album_title
            if "/browse/" in rel_url:
                browse_id = rel_url.split("/browse/", 1)[1].split("?", 1)[0]
                if browse_id.startswith("MPRE"):
                    album = yt.get_album(browse_id)
                    return release, album.get("tracks", []), album.get("title") or album_title
                playlist_id = browse_id[2:] if browse_id.startswith("VL") else browse_id
                playlist = yt.get_playlist(playlist_id)
                return release, playlist.get("tracks", []), playlist.get("title") or album_title
        except Exception as exc:
            logger.error("[Scraper] YTM release fetch failed for %s: %s", rel_url, exc, exc_info=True)
        return release, [], album_title

    items: List[Dict] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for release, tracks, album_title in pool.map(_fetch_one, releases):
            if cancel_check and cancel_check():
                break
            total_tracks = len(tracks)
            for t_idx, track in enumerate(tracks, 1):
                if cancel_check and cancel_check():
                    break
                vid = track.get("videoId")
                if not vid:
                    continue
                track_title = track.get("title", f"Track {t_idx}")
                artist = artist_name
                if track.get("artists"):
                    artist = ", ".join(a["name"] for a in track["artists"] if "name" in a)
                elif track.get("author"):
                    artist = track["author"]
                thumb_url = ""
                if track.get("thumbnails"):
                    thumb_url = track["thumbnails"][-1]["url"]
                elif track.get("thumbnail", {}).get("thumbnails"):
                    thumb_url = track["thumbnail"]["thumbnails"][-1]["url"]
                from utils.artwork_cleaner import clean_artwork_url
                thumb_url = clean_artwork_url(thumb_url, "ytmusic")
                track_dict = {
                    "title": track_title,
                    "artist": artist,
                    "album": album_title,
                    "parent_artist": artist_name,
                    "url": f"https://music.youtube.com/watch?v={vid}",
                    "thumbnail_url": thumb_url,
                    "duration_sec": int(track.get("duration_seconds") or track.get("lengthSeconds") or 0),
                    "platform": "ytmusic",
                    "release_type": (
                        "compilation"
                        if release.get("type") == "appears_on"
                        else release.get("type", "album")
                    ),
                    "category": release.get("category_name", ""),
                    "catalog_section": release.get("type", "album"),
                    "discovery_roles": list(
                        release.get("discovery_roles")
                        or [release.get("type", "album")]
                    ),
                    "source_release_id": release.get("id", ""),
                    "source_id": vid,
                    "album_index": t_idx,
                    "total_tracks": total_tracks,
                }
                items.append(track_dict)
                if on_item:
                    on_item(track_dict)

    return artist_name, items
# ── YouTube Isolated Functions ───────────────────────────────────────────────
def scrape_youtube_playlist(url: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for standard YouTube Playlists."""
    return _scrape_standard_ydl(url, "youtube", on_item)
def scrape_youtube_track(url: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for single YouTube videos."""
    return _scrape_standard_ydl(url, "youtube", on_item)
def scrape_youtube_channel(url: str, required_tabs: List[str], on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for YouTube channel browsing."""
    items = []
    sync_playwright = _sync_playwright_for("YouTube channel scraping")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=_USER_AGENT)
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)
        base_url = url.split("/videos")[0].split("/shorts")[0].split("/releases")[0].split("/playlists")[0]
        tab_map = {"סרטונים": "/videos", "קצרים": "/shorts", "פריטי תוכן": "/releases", "פלייליסטים": "/playlists"}
        page.goto(base_url, wait_until="load")
        try: channel_name = page.locator("yt-page-header-renderer h1").first.inner_text().strip()
        except: channel_name = "Unknown Channel"
        for tab_name in required_tabs:
            if tab_name not in tab_map: continue
            tab_url = base_url.rstrip("/") + tab_map[tab_name]
            page.goto(tab_url, wait_until="load")
            for _ in range(5):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
            if tab_name == "פלייליסטים":
                links = page.locator("main a.yt-simple-endpoint.ytd-playlist-thumbnail").evaluate_all("els => els.map(el => el.href)")
                for pl in links:
                    td = {"url": pl, "parent_artist": channel_name, "category": tab_name, "release_type": "playlist", "platform": "youtube"}
                    items.append(td);
                    if on_item: on_item(td)
            else:
                vids = page.locator("main a#video-title, a#video-title-link").all()
                for v in vids:
                    title = v.inner_text().strip()
                    href = v.get_attribute("href")
                    if href:
                        td = {"title": title, "url": "https://www.youtube.com" + href.split("&")[0], "parent_artist": channel_name, "category": tab_name, "release_type": "video", "platform": "youtube"}
                        items.append(td);
                        if on_item: on_item(td)
        browser.close()
    return channel_name, items

def _scraper_best_thumbnail(info: dict) -> str:
    """
    Pick the highest-resolution thumbnail URL from a yt-dlp info dict.
    Falls back gracefully through multiple possible keys.
    """
    # yt-dlp may provide a ranked list of thumbnails
    thumbnails: list[dict] = info.get("thumbnails") or []
    if thumbnails:
        # Sort by resolution (width * height) descending; prefer HTTPS
        def _score(t: dict) -> int:
            w = t.get("width")  or 0
            h = t.get("height") or 0
            return w * h

        ranked = sorted(
            [t for t in thumbnails if t.get("url")],
            key=_score,
            reverse=True,
        )
        if ranked:
            return ranked[0]["url"]

    # Direct thumbnail key as last resort
    return info.get("thumbnail") or ""


def _extract_spotify_data_from_json(
    data: Any, content_type: str, expected_spotify_id: str = "",
) -> Optional[Tuple[str, List[Dict]]]:
    """
    Generic traversal of Spotify initial-state JSON to find tracks and container title.
    """
    tracks_found = []
    container_title = None
    container_thumbnail = ""
    seen_track_ids = set()

    def traverse(node: Any):
        nonlocal container_title, container_thumbnail
        if isinstance(node, dict):
            # Check if this is the playlist/album container title
            node_type = str(node.get("type") or node.get("__typename") or "").lower()
            if (
                container_title is None
                and node_type == content_type.lower()
                and "name" in node
            ):
                container_title = node["name"]
            if node_type == content_type.lower() and not container_thumbnail:
                sources = (node.get("coverArt") or {}).get("sources") or []
                ranked = []
                for image in sources if isinstance(sources, list) else []:
                    if not isinstance(image, dict) or not image.get("url"):
                        continue
                    ranked.append((
                        int(image.get("width") or 0) * int(image.get("height") or 0),
                        image["url"],
                    ))
                if ranked:
                    container_thumbnail = max(ranked)[1]

            # Check if this is a track object
            is_track = node_type == "track"
            uri = str(node.get("uri", ""))
            if "spotify:track:" in uri:
                is_track = True

            if is_track and "name" in node:
                track_id = uri.split(":")[-1] if uri else node.get("id")
                if (
                    track_id and track_id not in seen_track_ids
                    and (not expected_spotify_id or track_id == expected_spotify_id)
                ):
                    seen_track_ids.add(track_id)

                    # Extract artists
                    artists_list = []
                    artists_data = node.get("artists", [])
                    if isinstance(artists_data, dict):
                        artists_data = artists_data.get("items") or []
                    if isinstance(artists_data, list):
                        for art in artists_data:
                            if isinstance(art, dict):
                                artist_name = art.get("name")
                                if not artist_name and isinstance(art.get("profile"), dict):
                                    artist_name = art["profile"].get("name")
                                if artist_name:
                                    artists_list.append(artist_name)
                    if not artists_list:
                        for group_name in ("firstArtist", "otherArtists"):
                            group = node.get(group_name) or {}
                            group_items = group.get("items") if isinstance(group, dict) else []
                            for art in group_items or []:
                                if not isinstance(art, dict):
                                    continue
                                artist_name = art.get("name")
                                if not artist_name and isinstance(art.get("profile"), dict):
                                    artist_name = art["profile"].get("name")
                                if artist_name:
                                    artists_list.append(artist_name)
                    # Extract album name
                    album_name = ""
                    album_data = node.get("album") or node.get("albumOfTrack")
                    if isinstance(album_data, dict) and "name" in album_data:
                        album_name = album_data["name"]

                    # Duration
                    dur_ms = node.get("duration_ms") or node.get("duration") or 0
                    if isinstance(dur_ms, dict):
                        dur_ms = dur_ms.get("totalMilliseconds") or 0
                    duration_sec = int(dur_ms) // 1000 if dur_ms else 0

                    # Thumbnail
                    thumb_url = ""
                    images = []
                    if isinstance(album_data, dict) and isinstance(album_data.get("images"), list):
                        images = album_data["images"]
                    elif isinstance(album_data, dict):
                        images = (album_data.get("coverArt") or {}).get("sources") or []
                    elif isinstance(node.get("images"), list):
                        images = node["images"]

                    if images:
                        if isinstance(images[0], dict) and "url" in images[0]:
                            ranked = []
                            for image in images:
                                if not isinstance(image, dict) or not image.get("url"):
                                    continue
                                ranked.append((
                                    int(image.get("width") or image.get("maxWidth") or 0)
                                    * int(image.get("height") or image.get("maxHeight") or 0),
                                    image["url"],
                                ))
                            thumb_url = max(ranked, default=(0, images[0]["url"]))[1]
                    if not thumb_url:
                        thumb_url = container_thumbnail

                    tracks_found.append({
                        "title": node["name"],
                        "artists": artists_list,
                        "album": album_name,
                        "duration_sec": duration_sec,
                        "thumbnail_url": thumb_url,
                        "track_id": track_id,
                    })

            # Continue traversal
            for val in node.values():
                traverse(val)
        elif isinstance(node, list):
            for item in node:
                traverse(item)

    traverse(data)

    if not tracks_found:
        return None

    scraped_title = container_title or f"Unknown Spotify {content_type}"
    items = []
    total = len(tracks_found)
    for idx, t in enumerate(tracks_found, start=1):
        from core.match_errors import SpotifyMetadataInvalid
        try:
            track_title, cleaned_artist, artist_names = _validated_spotify_display_metadata(
                t["title"], t["artists"],
            )
            cleaned_title = track_title
            match_status = "matched"
            resolution_error = ""
            metadata_error = ""
        except SpotifyMetadataInvalid as exc:
            cleaned_title = str(t.get("title") or "Spotify track")
            cleaned_artist = ""
            match_status = "metadata_invalid"
            resolution_error = "spotify_metadata_invalid_card"
            metadata_error = str(exc)
        items.append({
            "title": cleaned_title,
            "artist": cleaned_artist,
            "parent_artist": (
                artist_names[0]
                if match_status != "metadata_invalid" and artist_names else ""
            ),
            "album": t["album"] or scraped_title,
            "url": "",  # resolved in parallel after browser closes
            "album_index": idx,
            "thumbnail_url": _ensure_high_res_spotify_image(t["thumbnail_url"]) if t["thumbnail_url"] else "",
            "duration_sec": t["duration_sec"],
            "duration_str": f"{t['duration_sec'] // 60}:{t['duration_sec'] % 60:02d}" if t["duration_sec"] > 0 else "??:??",
            "platform": "spotify",
            "release_type": content_type.lower(),
            "total_tracks": total,
            "spotify_id": t.get("track_id") or "",
            "spotify_url": (
                f"https://open.spotify.com/track/{t['track_id']}"
                if t.get("track_id") else ""
            ),
            "match_status": match_status,
            "resolution_error": resolution_error,
            "metadata_error": metadata_error,
        })

    return scraped_title, items


def _parse_spotify_json_fallback(
    html: str, content_type: str, expected_spotify_id: str = "",
) -> Optional[Tuple[str, List[Dict]]]:
    """Parse Spotify tracks directly from embedded JSON in the HTML if available."""
    import base64
    import re
    import json
    import urllib.parse

    # Spotify's current full page stores a base64-encoded JSON payload in a
    # text/plain ``initialState`` script. It is locale-aware and exact enough
    # to preserve the same display names the user sees on spotify.com.
    current = re.search(
        r'<script\s+[^>]*id="initialState"[^>]*>(.*?)</script>', html, re.DOTALL,
    )
    if current:
        try:
            data = json.loads(base64.b64decode(current.group(1).strip()))
            res = _extract_spotify_data_from_json(
                data, content_type, expected_spotify_id=expected_spotify_id,
            )
            if res:
                return res
        except Exception as exc:
            logger.debug("[SpotifyScraper] initialState parse error: %s", exc)

    # 1. Search for legacy script tags with initial-state or session
    match = re.search(r'<script\s+[^>]*id="initial-state"[^>]* type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        match = re.search(r'<script\s+[^>]*id="session"[^>]* type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)

    if match:
        try:
            raw_json = match.group(1).strip()
            if raw_json.startswith("%"):
                raw_json = urllib.parse.unquote(raw_json)
            data = json.loads(raw_json)
            res = _extract_spotify_data_from_json(
                data, content_type, expected_spotify_id=expected_spotify_id,
            )
            if res:
                return res
        except Exception as exc:
            logger.debug("[SpotifyScraper] Initial-state parse error: %s", exc)

    # 2. Try window.__INITIAL_STATE__ JS variable assignment
    js_match = re.search(r'(?:window\.)?__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
    if js_match:
        try:
            data = json.loads(js_match.group(1).strip())
            res = _extract_spotify_data_from_json(
                data, content_type, expected_spotify_id=expected_spotify_id,
            )
            if res:
                return res
        except:
            pass

    # 3. Try generic application/json script tags
    for m in re.finditer(r'<script\s+[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            raw_json = m.group(1).strip()
            if raw_json.startswith("%"):
                raw_json = urllib.parse.unquote(raw_json)
            data = json.loads(raw_json)
            res = _extract_spotify_data_from_json(
                data, content_type, expected_spotify_id=expected_spotify_id,
            )
            if res:
                return res
        except:
            pass

    return None
