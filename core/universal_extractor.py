"""
core/universal_extractor.py  –  Playwright-based universal media interceptor
=============================================================================
Intercepts HLS (.m3u8), DASH (.mpd), and direct media URLs (.mp4, .webm, .ts)
that a page loads, exactly like the HLS Downloader browser extension.

Used as a fallback when yt-dlp cannot extract media from a URL.

Zero Qt imports.  Async core with a sync wrapper for non-async callers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Patterns that identify a URL as a media stream
_HLS_RE   = re.compile(r'\.m3u8(\?|$|#)', re.I)
_DASH_RE  = re.compile(r'\.mpd(\?|$|#)', re.I)
_MEDIA_RE = re.compile(r'\.(mp4|webm|ts|m4s|mkv|mov|avi|m4v|flv|f4v)(\?|$|#)', re.I)

# Content-Types that indicate a media resource
_MEDIA_CONTENT_TYPES = frozenset({
    "video/mp4", "video/webm", "video/mpeg", "video/ogg",
    "video/x-flv", "video/x-m4v", "video/quicktime",
    "audio/mpeg", "audio/mp4", "audio/webm", "audio/ogg",
    "audio/x-m4a", "audio/aac",
    "application/x-mpegurl",          # HLS
    "application/vnd.apple.mpegurl",  # HLS (Apple)
    "application/dash+xml",           # DASH
    "application/octet-stream",       # generic binary (many video CDNs)
})


@dataclass
class InterceptedStream:
    url:          str
    stream_type:  Literal["hls", "dash", "mp4", "webm", "ts", "unknown"]
    content_type: str   = ""
    size_hint:    int   = 0   # bytes from Content-Length header; 0 = unknown
    page_title:   str   = ""


def _classify_url(url: str) -> Literal["hls", "dash", "mp4", "webm", "ts", "unknown"]:
    if _HLS_RE.search(url):
        return "hls"
    if _DASH_RE.search(url):
        return "dash"
    m = _MEDIA_RE.search(url)
    if m:
        ext = m.group(1).lower()
        if ext == "mp4":    return "mp4"
        if ext == "webm":   return "webm"
        if ext in ("ts", "m4s"): return "ts"
    return "unknown"


def _classify_content_type(ct: str) -> Literal["hls", "dash", "mp4", "webm", "ts", "unknown"] | None:
    ct = ct.lower().split(";")[0].strip()
    if ct in ("application/x-mpegurl", "application/vnd.apple.mpegurl"):
        return "hls"
    if ct == "application/dash+xml":
        return "dash"
    if ct.startswith("video/mp4"):  return "mp4"
    if ct.startswith("video/webm"): return "webm"
    if any(ct.startswith(m) for m in ("video/", "audio/")):
        return "ts"
    return None


def _url_base(url: str) -> str:
    """Strip query string for deduplication (segment URLs share a base path)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


async def intercept_page(
    page_url:   str,
    timeout_ms: int = 20_000,
) -> list[InterceptedStream]:
    """
    Launch a headless Chromium page, intercept all media-related network
    requests, and return the deduplicated list of streams found.

    Sorted largest-first (by size_hint) so callers can pick the best stream.
    """
    from utils.playwright_check import is_playwright_available
    if not is_playwright_available():
        logger.warning(
            "[universal_extractor] Playwright Chromium not installed — "
            "stream interception is disabled. Run "
            "scripts/install_playwright.ps1 to enable it."
        )
        return []
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    found: dict[str, InterceptedStream] = {}   # base_url → stream (dedup)
    page_title = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = await ctx.new_page()

        # ── Request listener (fires before fetch) ─────────────────────────────
        async def _on_request(request) -> None:
            url = request.url
            st = _classify_url(url)
            if st != "unknown":
                base = _url_base(url)
                if base not in found:
                    found[base] = InterceptedStream(
                        url=url,
                        stream_type=st,
                    )

        # ── Response listener (fires after headers arrive) ────────────────────
        async def _on_response(response) -> None:
            ct = response.headers.get("content-type", "")
            ct_base = ct.lower().split(";")[0].strip()
            if ct_base not in _MEDIA_CONTENT_TYPES:
                return
            url = response.url
            st = _classify_content_type(ct_base) or _classify_url(url)
            if st != "unknown":
                base = _url_base(url)
                size = int(response.headers.get("content-length", 0) or 0)
                if base not in found:
                    found[base] = InterceptedStream(
                        url=url,
                        stream_type=st,
                        content_type=ct_base,
                        size_hint=size,
                    )
                else:
                    # Upgrade existing entry with real content-type/size
                    s = found[base]
                    s.content_type = ct_base
                    if size:
                        s.size_hint = size
                    if s.stream_type == "unknown":
                        s.stream_type = st

        page.on("request",  _on_request)
        page.on("response", _on_response)

        try:
            await page.goto(page_url, wait_until="networkidle", timeout=timeout_ms)
        except PWTimeout:
            logger.debug("universal_extractor: networkidle timeout, using partial results")
        except Exception as exc:
            logger.warning("universal_extractor: page.goto failed: %s", exc)
        finally:
            try:
                page_title = await page.title()
            except Exception:
                pass
            await browser.close()

    streams = list(found.values())
    for s in streams:
        s.page_title = page_title

    # Sort: HLS/DASH first (most useful), then by size descending
    def _sort_key(s: InterceptedStream):
        priority = {"hls": 0, "dash": 1, "mp4": 2, "webm": 3, "ts": 4, "unknown": 5}
        return (priority.get(s.stream_type, 5), -(s.size_hint or 0))

    streams.sort(key=_sort_key)
    return streams


def find_streams(
    page_url:   str,
    timeout_ms: int = 20_000,
) -> list[InterceptedStream]:
    """
    Synchronous wrapper around intercept_page().
    Safe to call from any thread (creates its own event loop).
    Returns [] if Playwright is not installed.
    """
    try:
        return asyncio.run(intercept_page(page_url, timeout_ms=timeout_ms))
    except Exception as exc:
        logger.warning("universal_extractor.find_streams failed: %s", exc)
        return []


def find_best_stream_with_title(
    page_url:   str,
    timeout_ms: int = 30_000,
) -> tuple[str, str, str]:
    """
    Open a single video page, intercept its best media stream, and return
    the stream URL, stream type, and the page title.

    This is the per-video equivalent of mpmux.com staticdownloader:
    open page → intercept HLS/DASH/mp4 stream → return for ffmpeg download.

    Returns
    -------
    (stream_url, stream_type, page_title)
    On failure, returns ('', 'unknown', '')
    """
    try:
        streams = asyncio.run(intercept_page(page_url, timeout_ms=timeout_ms))
    except Exception as exc:
        logger.warning("universal_extractor.find_best_stream_with_title failed: %s", exc)
        return "", "unknown", ""

    if not streams:
        return "", "unknown", ""

    # Best stream is already sorted: HLS/DASH first, then largest by size
    best = streams[0]
    return best.url, best.stream_type, best.page_title


# ── Generic video listing scraper ─────────────────────────────────────────────

def scrape_generic_video_listing(
    page_url:   str,
    timeout_ms: int = 30_000,
) -> list[dict]:
    """
    Generic Playwright scraper for any video listing page.

    Opens the page and looks for video page links using universal patterns:
    - href matching /video/, /watch/, /videos/, /embed/ etc.
    - Extracts title from: anchor[title], nearby h2/h3, aria-label, img[alt]
    - Extracts thumbnail from: img[src/data-src] near the anchor

    Returns a list of dicts:
        { 'title': str, 'url': str, 'thumbnail_url': str }

    Returns [] when:
    - Playwright not available
    - Page has no recognisable video links (likely a single video page)
    - Network/timeout error
    """
    from utils.playwright_check import is_playwright_available
    if not is_playwright_available():
        logger.warning(
            "[generic_listing] Playwright Chromium not installed — "
            "generic listing extraction is disabled."
        )
        return []
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    # URL path patterns that strongly indicate a video page link
    _VIDEO_PATH_RE = re.compile(
        r"/(?:video|videos|watch|v|embed|clip|movie|film|porn|tube|play|stream|item|content)"
        r"[s]?[/\-_]",
        re.IGNORECASE,
    )

    def _block_media(route):
        if route.request.resource_type in ("media", "font"):
            route.abort()
        else:
            route.continue_()

    def _title_from_element(el) -> str:
        """Try multiple attributes/inner text to get a meaningful title."""
        for method in (
            lambda: el.get_attribute("title") or "",
            lambda: el.get_attribute("aria-label") or "",
            lambda: el.inner_text().strip(),
        ):
            try:
                val = method()
                if val and len(val) > 2:
                    return val.strip()
            except Exception:
                pass
        return ""

    results: list[dict] = []
    seen: set = set()

    parsed = urlparse(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=_USER_AGENT, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.route("**/*", _block_media)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            logger.warning("[generic_listing] Page load failed: %s", exc)
            browser.close()
            return []

        # Short wait for JS to render video cards
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass

        try:
            # Find ALL anchor tags on the page
            anchors = page.locator("a[href]").all()
        except Exception:
            browser.close()
            return []

        for anchor in anchors:
            try:
                href = anchor.get_attribute("href") or ""
                if not href:
                    continue

                # Make absolute
                if href.startswith("/"):
                    href = base + href
                elif not href.startswith("http"):
                    continue

                # Skip same-page anchors, JS, mailto, etc.
                if href == page_url or "#" in href.split("?")[0]:
                    continue

                # Must look like a video page URL
                link_path = urlparse(href).path
                if not _VIDEO_PATH_RE.search(link_path):
                    continue

                # Deduplicate
                if href in seen:
                    continue
                seen.add(href)

                # Extract title
                title = _title_from_element(anchor)
                if not title:
                    # Try nearest heading sibling/parent
                    for selector in ("h2", "h3", "h4", ".title", "[class*='title']", "[class*='name']"):
                        try:
                            el = anchor.locator(f"xpath=./ancestor-or-self::*/descendant::{selector}[1]").first
                            if el.count():
                                t = el.inner_text().strip()
                                if t and len(t) > 2:
                                    title = t
                                    break
                        except Exception:
                            pass

                if not title:
                    # Fall back to URL slug
                    slug = link_path.rstrip("/").split("/")[-1]
                    title = re.sub(r"[-_]", " ", re.sub(r"[^a-zA-Z0-9\-_ ]", "", slug)).strip()

                if not title:
                    continue

                # Extract thumbnail (img near the anchor)
                thumbnail_url = ""
                try:
                    img = anchor.locator("img").first
                    if img.count():
                        thumbnail_url = (
                            img.get_attribute("data-src")
                            or img.get_attribute("src")
                            or ""
                        )
                except Exception:
                    pass

                results.append({
                    "title": title,
                    "url": href,
                    "thumbnail_url": thumbnail_url,
                })

            except Exception as exc:
                logger.debug("[generic_listing] anchor error: %s", exc)

        browser.close()

    logger.info("[generic_listing] Found %d video links on %s", len(results), page_url)
    return results
