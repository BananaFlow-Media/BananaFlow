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
) -> str:
    """
    Resolve a track to a YouTube Music URL via ytmusicapi search.

    Uses duration-based and text-similarity confidence scoring to pick the best match.
    Falls back to a general YouTube search if YTM confidence is low, and finally
    to a plain ytsearch1: query as a last resort.

    ``cancel_check`` (if given) is polled between the cheap YTM search and the
    heavier yt-dlp fallback so a cancel doesn't let an already-doomed second
    network call run to completion.
    """
    query = f"{artist} {title}" if artist else title
    best_ytm_candidate = None
    best_ytm_score = -1.0

    try:
        from ytmusicapi import YTMusic
        from core.spotify_match_scorer import score_candidate

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

            best_ytm_breakdown = {}
            for r in results:
                vid = r.get("videoId")
                if not vid:
                    continue

                yt_title = r.get("title") or ""
                artists_list = r.get("artists") or []
                yt_channel = artists_list[0].get("name") if artists_list else ""
                yt_dur = _dur_secs(r)

                spotify_dur = duration_sec if duration_sec > 0 else None

                score, breakdown = score_candidate(
                    spotify_title=title,
                    spotify_artist=artist,
                    spotify_dur=spotify_dur,
                    yt_title=yt_title,
                    yt_channel=yt_channel,
                    yt_dur=yt_dur if yt_dur > 0 else None
                )

                if score > best_ytm_score:
                    best_ytm_score = score
                    best_ytm_candidate = (vid, score)
                    best_ytm_breakdown = breakdown

            if best_ytm_candidate:
                vid, score = best_ytm_candidate
                confidence = score / 100.0
                if artist and best_ytm_breakdown.get("artist", 0.0) == 0.0:
                    confidence = min(confidence, 0.50)
                logger.debug(
                    "[Scraper] Best YTM candidate: %s (score=%.1f, confidence=%.2f)",
                    vid, score, confidence
                )
                if confidence >= 0.65:
                    return f"https://music.youtube.com/watch?v={vid}"

    except Exception as exc:
        logger.debug("[Scraper] ytmusicapi search or scoring failed: %s", exc)

    # A cancel between the cheap YTM search and the heavier yt-dlp fallback
    # returns the last-resort placeholder immediately rather than firing
    # another network call the user has already abandoned.
    if cancel_check and cancel_check():
        return f"ytsearch1:{query} audio"

    # ── Fallback 1: General YouTube Search ──
    try:
        from core.spotify_match_scorer import find_best_youtube_match
        spotify_dur = duration_sec if duration_sec > 0 else None
        yt_match = find_best_youtube_match(
            title=title,
            artist=artist,
            duration_sec=spotify_dur,
            min_confidence=0.55,
            cookies_file=cookies_file,
        )
        if yt_match:
            logger.info(
                "[Scraper] YTM confidence was low. Found strong general YouTube match: %s (confidence=%.2f)",
                yt_match.youtube_title, yt_match.confidence
            )
            return yt_match.url
    except Exception as exc:
        logger.debug("[Scraper] General YouTube fallback search failed: %s", exc)

    # ── Fallback 2: Best YTM candidate (even if low confidence) ──
    if best_ytm_candidate:
        vid, score = best_ytm_candidate
        if score >= 35.0:
            logger.debug(
                "[Scraper] Falling back to low confidence YTM candidate: %s (score=%.1f)",
                vid, score
            )
            return f"https://music.youtube.com/watch?v={vid}"

    # ── Fallback 3: Last Resort ytsearch1 ──
    # Routine — every track always resolves to *something* playable via this
    # last-resort search; nothing here is actionable by the user.
    logger.debug("[Scraper] No confident match found for '%s'. Using last resort ytsearch1.", query)
    return f"ytsearch1:{query} audio"


def _spotify_id_from_url(url: str) -> str:
    """Extract the bare Spotify track id from a track URL, or "" if absent."""
    m = re.search(r"/track/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else ""


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


def resolve_track_to_youtube(
    td: Dict,
    cookies_file: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
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

    cached = cache.get(spotify_key, MATCH_ALGO_VERSION)
    if cached:
        return cached

    url = _resolve_to_ytm_url(
        td.get("title", ""),
        td.get("artist", ""),
        td.get("duration_sec") or 0,
        cookies_file=cookies_file,
        cancel_check=cancel_check,
    )

    # Cache only confident matches — never the ytsearch* last-resort sentinel.
    if url and not url.startswith("ytsearch"):
        cache.put(spotify_key, url, None, MATCH_ALGO_VERSION, key_kind=key_kind)
    return url


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
                    artists = ", ".join([a.inner_text().strip() for a in artist_links]) if artist_links else "Unknown Artist"

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
                    from utils.metadata_cleaner import clean_title_and_artist
                    cleaned_title, cleaned_artist = clean_title_and_artist(track_title, artists)
                    track_dict = {
                        "title": cleaned_title, "artist": cleaned_artist, "album": scraped_title,
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
) -> Tuple[str, List[Dict]]:
    """Dedicated entry for Spotify Playlists.

    When ``metadata_only`` is set the tracks are published immediately with
    metadata only and the YouTube match is deferred to download time.
    """
    sync_playwright = _sync_playwright_for("Spotify playlist scraping")
    title, items = "Unknown Playlist", []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_USER_AGENT)
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
) -> Tuple[str, List[Dict]]:
    """Dedicated entry for Spotify Albums.

    When ``metadata_only`` is set the tracks are published immediately with
    metadata only and the YouTube match is deferred to download time.
    """
    sync_playwright = _sync_playwright_for("Spotify album scraping")
    title, items = "Unknown Album", []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_USER_AGENT)
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
def scrape_spotify_track(url: str, on_item: Optional[Callable[[Dict], None]] = None, cookies_file: Optional[str] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for single Spotify track."""
    title = "Unknown Track"
    items = []
    sync_playwright = _sync_playwright_for("Spotify track scraping")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_USER_AGENT)
        page = context.new_page()
        page.route("**/*", _block_heavy_resources)
        try:
            page.goto(url, wait_until="load")

            # Try embedded JSON extraction fallback first
            try:
                html = page.content()
                parsed = _parse_spotify_json_fallback(html, "Track")
                if parsed:
                    title, tracks = parsed
                    tracks = tracks[:1]
                    for t in tracks:
                        t["category"] = "סינגלים ו-EP"
                        t["release_type"] = "single"
                        t["total_tracks"] = 1
                    items = tracks
                    artists = items[0]["artist"]
            except Exception as e:
                logger.debug(f"[SpotifyScraper] Track JSON fallback failed: {e}")

            if not items:
                page.wait_for_selector("main h1", timeout=12000)
                title = page.locator("main h1").first.inner_text().strip()

                # Extract high-res thumbnail
                thumb_url = ""
                try:
                    img_el = page.locator("main img[data-testid='entity-image'], main img").first
                    if img_el.count():
                        thumb_url = _ensure_high_res_spotify_image(img_el.get_attribute("src") or "")
                except: pass
                artist_links = page.locator("main a[href*='/artist/']").all()
                artist_names = [a.inner_text().strip() for a in artist_links] if artist_links else []
                artists = ", ".join(artist_names) if artist_names else "Unknown Artist"
                parent_artist = artist_names[0] if artist_names else ""

                from utils.metadata_cleaner import clean_title_and_artist
                cleaned_title, cleaned_artist = clean_title_and_artist(title, artists)

                track_dict = {
                    "title": cleaned_title, "artist": cleaned_artist, "album": cleaned_title,
                    "parent_artist": parent_artist, "category": "סינגלים ו-EP",
                    "url": "",  # resolved after browser closes
                    "thumbnail_url": thumb_url, "platform": "spotify",
                    "release_type": "single", "total_tracks": 1,
                }
                items.append(track_dict)
        finally: browser.close()
    if items:
        _parallel_resolve_urls(items, on_item, max_workers=1, cookies_file=cookies_file)
    return title, items
def scrape_spotify_artist(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cookies_file: Optional[str] = None,
    metadata_only: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[str, List[Dict]]:
    """
    Dedicated entry for Spotify Artist discographies.
    Iterates over /album and /single URLs for categorical accuracy.

    When ``metadata_only`` is set the whole catalog is published immediately
    with metadata only and the YouTube match is deferred to download time.
    ``cancel_check`` is polled in the scroll/category loops so a user cancel
    stops the (potentially long) scrape promptly.
    """
    items = []
    artist_name = ""
    seen_track_uids = set()
    # Normalize URL: strip trailing slashes and common sub-paths to get the base artist URL
    artist_url = re.sub(r"/discography/.*$", "", url.rstrip("/"))
    artist_url = re.sub(r"/all/?$", "", artist_url)
    sync_playwright = _sync_playwright_for("Spotify artist discography scraping")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            viewport={"width": 1280, "height": 1000},
            user_agent=_USER_AGENT,
            locale="he-IL",
            extra_http_headers={"Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7"}
        )
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
                                artists = ", ".join([a.inner_text().strip() for a in artist_links]) if artist_links else artist_name
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
def scrape_ytm_artist(url: str, on_item: Optional[Callable[[Dict], None]] = None) -> Tuple[str, List[Dict]]:
    """Dedicated entry for YTM Artist discographies."""
    from utils.ytm_scraper import fetch_ytm_artist_releases
    from concurrent.futures import ThreadPoolExecutor

    releases = fetch_ytm_artist_releases(url)
    if not releases:
        return "Unknown Artist", []
    artist_name = releases[0].get("parent_artist", "Unknown Artist")

    def _fetch_one(release: Dict) -> Tuple[Dict, List, str]:
        """Fetch tracks for one release in its own YTMusic session (thread-safe)."""
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
        except Exception as exc:
            logger.error("[Scraper] YTM release fetch failed for %s: %s", rel_url, exc, exc_info=True)
        return release, [], album_title

    items: List[Dict] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for release, tracks, album_title in pool.map(_fetch_one, releases):
            total_tracks = len(tracks)
            for t_idx, track in enumerate(tracks, 1):
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
                    "release_type": release.get("type", "album"),
                    "category": release.get("category_name", ""),
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


def _extract_spotify_data_from_json(data: Any, content_type: str) -> Optional[Tuple[str, List[Dict]]]:
    """
    Generic traversal of Spotify initial-state JSON to find tracks and container title.
    """
    tracks_found = []
    container_title = None
    seen_track_ids = set()

    def traverse(node: Any):
        nonlocal container_title
        if isinstance(node, dict):
            # Check if this is the playlist/album container title
            node_type = str(node.get("type", "")).lower()
            if node_type == content_type.lower() and "name" in node:
                container_title = node["name"]

            # Check if this is a track object
            is_track = node_type == "track"
            uri = str(node.get("uri", ""))
            if "spotify:track:" in uri:
                is_track = True

            if is_track and "name" in node:
                track_id = uri.split(":")[-1] if uri else node.get("id")
                if track_id and track_id not in seen_track_ids:
                    seen_track_ids.add(track_id)

                    # Extract artists
                    artists_list = []
                    artists_data = node.get("artists", [])
                    if isinstance(artists_data, list):
                        for art in artists_data:
                            if isinstance(art, dict) and "name" in art:
                                artists_list.append(art["name"])
                    artists_str = ", ".join(artists_list) if artists_list else "Unknown Artist"

                    # Extract album name
                    album_name = ""
                    album_data = node.get("album")
                    if isinstance(album_data, dict) and "name" in album_data:
                        album_name = album_data["name"]

                    # Duration
                    dur_ms = node.get("duration_ms", 0)
                    duration_sec = int(dur_ms) // 1000 if dur_ms else 0

                    # Thumbnail
                    thumb_url = ""
                    images = []
                    if isinstance(album_data, dict) and isinstance(album_data.get("images"), list):
                        images = album_data["images"]
                    elif isinstance(node.get("images"), list):
                        images = node["images"]

                    if images:
                        if isinstance(images[0], dict) and "url" in images[0]:
                            thumb_url = images[0]["url"]

                    tracks_found.append({
                        "title": node["name"],
                        "artist": artists_str,
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
        from utils.metadata_cleaner import clean_title_and_artist
        cleaned_title, cleaned_artist = clean_title_and_artist(t["title"], t["artist"])
        items.append({
            "title": cleaned_title,
            "artist": cleaned_artist,
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
        })

    return scraped_title, items


def _parse_spotify_json_fallback(html: str, content_type: str) -> Optional[Tuple[str, List[Dict]]]:
    """Parse Spotify tracks directly from embedded JSON in the HTML if available."""
    import re
    import json
    import urllib.parse

    # 1. Search for script tags with initial-state or session
    match = re.search(r'<script\s+[^>]*id="initial-state"[^>]* type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        match = re.search(r'<script\s+[^>]*id="session"[^>]* type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL)

    if match:
        try:
            raw_json = match.group(1).strip()
            if raw_json.startswith("%"):
                raw_json = urllib.parse.unquote(raw_json)
            data = json.loads(raw_json)
            res = _extract_spotify_data_from_json(data, content_type)
            if res:
                return res
        except Exception as exc:
            logger.debug("[SpotifyScraper] Initial-state parse error: %s", exc)

    # 2. Try window.__INITIAL_STATE__ JS variable assignment
    js_match = re.search(r'(?:window\.)?__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>', html, re.DOTALL)
    if js_match:
        try:
            data = json.loads(js_match.group(1).strip())
            res = _extract_spotify_data_from_json(data, content_type)
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
            res = _extract_spotify_data_from_json(data, content_type)
            if res:
                return res
        except:
            pass

    return None
