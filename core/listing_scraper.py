"""
core/listing_scraper.py  –  Generic paginated video listing scraper
====================================================================
Scrapes video listing pages (user pages, channel pages, category pages)
on any site to extract individual video metadata: title, URL, thumbnail, duration.

Handles pagination automatically (numbered pages: /videos/1, /videos/2, ...).

Zero Qt imports.  Uses Playwright (synchronous API).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# Real Desktop User Agent to avoid bot-detection
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _parse_duration_str(dur_text: str) -> Tuple[str, int]:
    """
    Parse a duration string like '26:00' or '1:30:45' into
    (formatted_str, total_seconds).
    Returns ('', 0) on failure.
    """
    dur_text = (dur_text or "").strip()
    if not dur_text:
        return "", 0
    parts = dur_text.split(":")
    try:
        if len(parts) == 2:
            secs = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return dur_text, 0
        return dur_text, secs
    except (ValueError, TypeError):
        return dur_text, 0


def _base_url(url: str) -> str:
    """Return scheme + netloc (e.g. 'https://example.com')."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _page_url(base_listing_url: str, page: int) -> str:
    """
    Build the paginated URL for a listing page.
    Handles the common pattern of appending /2, /3, etc. to the base URL.
    """
    url = base_listing_url.rstrip("/")
    # Remove trailing page number if already present
    url = re.sub(r"/(\d+)$", "", url)
    if page > 1:
        return f"{url}/{page}"
    return url


def _block_media(route) -> None:
    """Block heavy media and font resources; allow images so thumbnails load."""
    if route.request.resource_type in ("media", "font"):
        route.abort()
    else:
        route.continue_()


def scrape_listing_page(
    url: str,
    on_item: Optional[Callable[[Dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    max_pages: int = 200,
) -> Tuple[str, List[Dict]]:
    """
    Scrape a paginated video listing page on any site.

    Parameters
    ----------
    url          : The listing URL (e.g. https://example.com/users/X/videos)
    on_item      : Called incrementally with a track dict for each video found
    cancel_check : Called each iteration; if it returns True, scraping stops
    max_pages    : Safety cap on number of pages to scrape

    Returns
    -------
    (playlist_title, list_of_track_dicts)

    Each track dict has:
        title, url, thumbnail_url, duration_str, duration_sec, artist,
        album, platform, category
    """
    from utils.playwright_check import is_playwright_available
    if not is_playwright_available():
        logger.error(
            "[ListingScraper] Playwright Chromium not installed — "
            "run scripts/install_playwright.ps1 to enable listing scraping."
        )
        return "Listing", []
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    items: List[Dict] = []
    seen_urls: set = set()
    playlist_title = urlparse(url).netloc  # default: site domain

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.route("**/*", _block_media)

        try:
            for page_num in range(1, max_pages + 1):
                if cancel_check and cancel_check():
                    logger.info("[ListingScraper] Scraping cancelled at page %d", page_num)
                    break

                page_url_str = _page_url(url, page_num)
                logger.debug("[ListingScraper] Fetching page %d: %s", page_num, page_url_str)

                try:
                    page.goto(page_url_str, wait_until="domcontentloaded", timeout=30_000)
                except PWTimeout:
                    logger.debug("[ListingScraper] Page %d timed out, trying with partial load", page_num)
                except Exception as exc:
                    logger.warning("[ListingScraper] Failed to load page %d: %s", page_num, exc)
                    break

                # Extract playlist title from the first page
                if page_num == 1:
                    try:
                        h1 = page.locator("h1").first
                        if h1.count():
                            playlist_title = h1.inner_text().strip() or playlist_title
                        if not playlist_title or playlist_title == urlparse(url).netloc:
                            playlist_title = page.title().strip() or playlist_title
                        # Strip common site-branding suffixes like " - SiteName | Watch Videos"
                        playlist_title = re.sub(
                            r"\s*[–\-|]\s*(Videos?|Watch|Clips?|Streaming).*$",
                            "",
                            playlist_title,
                            flags=re.IGNORECASE,
                        ).strip()
                    except Exception:
                        pass

                # Wait for video cards to appear
                try:
                    page.wait_for_selector(".video-thumb, .thumb, [class*='video-card'], article", timeout=10_000)
                except PWTimeout:
                    logger.debug("[ListingScraper] No video cards found on page %d", page_num)
                    break

                # Scroll to bottom so that lazy-loaded thumbnails enter the viewport
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(300)
                except Exception:
                    pass

                # Extract thumbnail URLs via JS (faster than per-card Playwright locators)
                js_thumb_map = page.evaluate("""
                () => {
                    const result = {};
                    const selectors = '.video-thumb, .thumb, [class*="video-card"], article';
                    document.querySelectorAll(selectors).forEach((card) => {
                        const a = card.querySelector('a[href*="/video"]')
                               || card.querySelector('a[href]');
                        if (!a) return;
                        const href = a.href;
                        const img  = card.querySelector('img');
                        if (!img) { result[href] = ''; return; }
                        let src = img.src || '';
                        if (!src || src.startsWith('data:')) {
                            src = img.getAttribute('data-src')
                               || img.getAttribute('data-lazy-src')
                               || img.getAttribute('data-original')
                               || '';
                        }
                        result[href] = src;
                    });
                    return result;
                }
                """)

                # Scrape all video cards on this page
                cards = page.locator(".video-thumb").all()
                if not cards:
                    cards = page.locator(".thumb").all()
                if not cards:
                    cards = page.locator("[class*='video-card'], article").all()

                if not cards:
                    logger.debug("[ListingScraper] No cards on page %d — stopping", page_num)
                    break

                page_added = 0
                for card in cards:
                    if cancel_check and cancel_check():
                        break
                    try:
                        # Title + URL: from the main title anchor
                        title_anchor = card.locator("a[class*='name']").first
                        if not title_anchor.count():
                            title_anchor = card.locator("a[class*='title']").first
                        if not title_anchor.count():
                            title_anchor = card.locator("a").first

                        title = ""
                        video_url = ""
                        if title_anchor.count():
                            title = (
                                title_anchor.get_attribute("title")
                                or title_anchor.inner_text()
                                or ""
                            ).strip()
                            href = title_anchor.get_attribute("href") or ""
                            if href:
                                video_url = (
                                    href
                                    if href.startswith("http")
                                    else urljoin(_base_url(url), href)
                                )

                        if not video_url or video_url in seen_urls:
                            continue
                        seen_urls.add(video_url)

                        if not title:
                            title = video_url.rstrip("/").split("/")[-1].replace("-", " ")

                        # Thumbnail – prefer the JS-prebuilt map, fall back to DOM query
                        thumbnail_url = js_thumb_map.get(video_url, "")
                        if not thumbnail_url or thumbnail_url.startswith("data:"):
                            try:
                                thumb_img = card.locator(
                                    "img[data-src], img[src]"
                                ).first
                                if thumb_img.count():
                                    thumbnail_url = (
                                        thumb_img.get_attribute("src")
                                        or thumb_img.get_attribute("data-src")
                                        or thumb_img.get_attribute("data-lazy-src")
                                        or thumb_img.get_attribute("data-original")
                                        or ""
                                    )
                                    if thumbnail_url.startswith("data:"):
                                        thumbnail_url = (
                                            thumb_img.get_attribute("data-src")
                                            or thumb_img.get_attribute("data-lazy-src")
                                            or ""
                                        )
                            except Exception:
                                pass

                        # Duration
                        duration_str = ""
                        duration_sec = 0
                        try:
                            dur_el = card.locator("[class*='duration']").first
                            if dur_el.count():
                                duration_str, duration_sec = _parse_duration_str(
                                    dur_el.inner_text()
                                )
                        except Exception:
                            pass

                        track_dict: Dict = {
                            "title": title,
                            "url": video_url,
                            "thumbnail_url": thumbnail_url,
                            "duration_str": duration_str,
                            "duration_sec": duration_sec if duration_sec else None,
                            "artist": "",
                            "album": playlist_title,
                            "platform": "generic",
                            # At download time, open this video page and intercept its stream
                            "category": "stream_intercept",
                        }
                        items.append(track_dict)
                        page_added += 1

                        if on_item:
                            try:
                                on_item(track_dict)
                            except Exception:
                                pass

                    except Exception as exc:
                        logger.debug("[ListingScraper] Card parse error: %s", exc)

                logger.info(
                    "[ListingScraper] Page %d: found %d new videos (total: %d)",
                    page_num, page_added, len(items),
                )

                if page_added == 0:
                    logger.info("[ListingScraper] No new videos on page %d — stopping", page_num)
                    break

                # Small delay between pages
                time.sleep(0.5)

        finally:
            browser.close()

    logger.info("[ListingScraper] Scraping complete: %d videos found", len(items))
    return playlist_title, items
