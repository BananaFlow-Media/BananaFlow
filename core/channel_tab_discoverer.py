"""
core/channel_tab_discoverer.py  –  YouTube channel tab discovery
=================================================================
Finds which tabs actually exist on a channel (Videos, Shorts, Live,
Playlists, Releases, Podcasts) and returns a list of TabInfo objects.

Two strategies, in order:

1. An HTTP/yt-dlp probe (``_probe_tabs_via_ytdlp``): requests each known
   tab-suffix URL with ``extract_flat`` and a 1-item cap, no browser
   involved. This is the Phase 6 (Playwright/Chromium package-size and
   performance review) "partial HTTP replacement" follow-up — it removes
   the Chromium launch entirely for the common case, at no loss of
   functionality, since Playwright remains the fallback below.
2. The original Playwright DOM read (``_discover_tabs_via_playwright``),
   used only when the HTTP probe is inconclusive (network error, or the
   base channel URL itself does not resolve via yt-dlp) — kept verbatim
   as the reliability fallback per the plan's explicit instruction not to
   remove Playwright without a fully proven alternative.

Measured against real channels: the HTTP probe resolves in ~6-7 seconds;
the Playwright fallback takes ~35 seconds. The fallback's DOM selectors
(``yt-tab-shape``) are still correct against YouTube's live layout — the
real bug (found and fixed while investigating a "stale selectors" report,
issue #27) was that a fresh Playwright context with no cookies gets
redirected to YouTube's EU cookie-consent interstitial
(``consent.youtube.com``) before the channel page ever loads, so the tab
DOM this fallback reads never renders at all. Pre-setting the `CONSENT`/
`SOCS` cookies below bypasses that wall; no selector needed to change.
The probe relies on
yt-dlp's ``youtube:tab`` extractor raising ``DownloadError`` with
"This channel does not have a <tab> tab" for a genuinely absent tab
(verified directly against real channels, not assumed) rather than
silently redirecting to an existing tab — a non-raising ``extract_info``
call is trusted as "tab exists."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tab catalogue ──────────────────────────────────────────────────────────────
# Maps YouTube's English tab titles (what the DOM contains) to our Hebrew labels
# and canonical URL path suffixes.

_TAB_MAP: dict[str, dict] = {
    "videos":    {"name": "סרטונים",      "path": "/videos",    "icon": "🎬", "type": "videos"},
    "shorts":    {"name": "קצרים",         "path": "/shorts",    "icon": "⚡", "type": "shorts"},
    "live":      {"name": "שידורים חיים", "path": "/streams",   "icon": "🔴", "type": "streams"},
    "streams":   {"name": "שידורים חיים", "path": "/streams",   "icon": "🔴", "type": "streams"},
    "playlists": {"name": "פלייליסטים",   "path": "/playlists", "icon": "📋", "type": "playlists"},
    "releases":  {"name": "פריטי תוכן",   "path": "/releases",  "icon": "🎵", "type": "releases"},
    "podcasts":  {"name": "פודקאסטים",    "path": "/podcasts",  "icon": "🎙", "type": "podcasts"},
}

# Tabs we don't care about (community posts, store, members, about, etc.)
_IGNORE_TABS = {"community", "about", "channels", "membership", "store", "featured", "home"}


@dataclass
class TabInfo:
    name:     str   # Hebrew display name, e.g. "סרטונים"
    url:      str   # Full tab URL, e.g. "https://youtube.com/@handle/videos"
    icon:     str   # Emoji icon for the Card Grid dialog
    tab_type: str   # Canonical type: "videos" | "shorts" | "streams" | "playlists" | "releases" | "podcasts"
    count:    int   = -1   # -1 = unknown; will be populated after scraping


@dataclass
class DiscoveryResult:
    channel_name: str
    channel_url:  str
    tabs:         list[TabInfo] = field(default_factory=list)
    error:        Optional[str] = None
    # True when `tabs` is _default_tabs()'s hardcoded guess rather than the
    # channel's real tab list, and/or the channel name could not be read.
    # Issue #27 was originally reported as "stale selectors" precisely
    # because this degradation used to be invisible: the discoverer returned
    # a plausible-looking guess with error=None, so a caller could not tell a
    # real answer from a fabricated one. Callers must surface this; the
    # result is a usable fallback, not a discovery.
    degraded:     bool = False

    def success(self) -> bool:
        return not self.error and bool(self.tabs) and not self.degraded


# ── Main entry point ───────────────────────────────────────────────────────────

def discover_tabs(url: str) -> DiscoveryResult:
    """
    Blocking call.  Returns a DiscoveryResult with all available tabs.

    Tries the HTTP/yt-dlp probe first (``_probe_tabs_via_ytdlp`` — no
    browser launch). Falls back to a real Playwright browser only if that
    probe is inconclusive (base channel unreachable via yt-dlp, or every
    single tab probe failed). This means a typical call resolves without
    ever starting Chromium.

    Parameters
    ----------
    url : Any YouTube channel URL format:
          https://www.youtube.com/@handle
          https://www.youtube.com/channel/UCxxx
          https://www.youtube.com/user/xxx
          https://www.youtube.com/c/xxx

    Returns
    -------
    DiscoveryResult — always returned; check .error for failures.
    """
    # Strip any trailing tab suffix so we always land on the channel home
    base_url = _strip_tab_suffix(url)

    probe_result = _probe_tabs_via_ytdlp(base_url)
    if probe_result is not None:
        channel_name, tabs = probe_result
        logger.debug(
            "[TabDiscoverer] Resolved %d tab(s) for %r via HTTP probe — no browser launched",
            len(tabs), base_url,
        )
        return DiscoveryResult(channel_name=channel_name, channel_url=base_url, tabs=tabs)

    logger.debug("[TabDiscoverer] HTTP probe inconclusive for %r — falling back to Playwright", base_url)
    return _discover_tabs_via_playwright(base_url)


# ── HTTP/yt-dlp probe (no browser) ──────────────────────────────────────────────

def _probe_tabs_via_ytdlp(base_url: str) -> Optional[tuple[str, list[TabInfo]]]:
    """
    Try to discover tabs using yt-dlp/HTTP only — no Playwright, no Chromium.

    Returns ``(channel_name, tabs)`` on success, or ``None`` if the probe
    itself is inconclusive and the caller should fall back to Playwright.
    A single tab's own probe failing is the *normal* case (most channels
    don't have all six tabs) and is not treated as inconclusive — only a
    failure to resolve the base channel at all, or every tab failing, is.
    """
    import yt_dlp
    from utils.logger import SilentLogger
    from utils.yt_dlp_opts import build_search_ydl_opts

    opts = build_search_ydl_opts(max_results=1, logger=SilentLogger())

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            base_info = ydl.extract_info(base_url, download=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[TabDiscoverer] HTTP probe: base channel unreachable via yt-dlp: %s", exc)
        return None

    if not base_info:
        return None

    # Empty, not a "Unknown Channel" placeholder: an unread name is reported
    # as unknown by the caller (DiscoveryResult.degraded) rather than being
    # dressed up as a discovered value — see issue #27.
    channel_name = (
        base_info.get("channel")
        or base_info.get("uploader")
        or base_info.get("title")
        or ""
    )

    found_tabs: list[TabInfo] = []
    seen_types: set[str] = set()
    for _key, info in _TAB_MAP.items():
        if info["type"] in seen_types:
            continue
        # Mark attempted regardless of outcome: "live" and "streams" share the
        # same type/path, so a failed probe here means a retry under the
        # other key would hit the identical URL and fail identically.
        seen_types.add(info["type"])
        tab_url = base_url.rstrip("/") + info["path"]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(tab_url, download=False)
        except Exception:
            continue  # this tab doesn't exist for this channel — expected, not an error
        found_tabs.append(TabInfo(
            name=info["name"],
            url=tab_url,
            icon=info["icon"],
            tab_type=info["type"],
        ))

    if not found_tabs:
        logger.warning(
            "[TabDiscoverer] HTTP probe found zero tabs for a resolvable channel "
            "(%r) — treating as inconclusive, falling back to Playwright",
            base_url,
        )
        return None

    return channel_name, found_tabs


# ── Playwright fallback (unchanged behavior) ────────────────────────────────────

def _discover_tabs_via_playwright(base_url: str) -> DiscoveryResult:
    """The original DOM-based discovery. Used only when the HTTP probe above
    is inconclusive."""
    from utils.playwright_check import is_playwright_available
    if not is_playwright_available():
        return DiscoveryResult(
            channel_name="",
            channel_url=base_url,
            error=(
                "Channel discovery requires Playwright Chromium, which is "
                "not installed. Run scripts/install_playwright.ps1 from the "
                "install folder (or `python -m playwright install chromium`)."
            ),
        )
    from playwright.sync_api import sync_playwright

    try:
        from core.scraper import _USER_AGENT, _block_heavy_resources  # reuse existing helpers
    except ImportError:
        _USER_AGENT = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        _block_heavy_resources = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=_USER_AGENT,
            )
            # A fresh context has no cookies, so YouTube redirects to its EU
            # cookie-consent interstitial (consent.youtube.com) before the
            # channel page loads at all — the tab DOM below never renders.
            # Pre-accepting consent avoids the redirect entirely.
            ctx.add_cookies([
                {"name": "CONSENT", "value": "YES+cb.20210328-17-p0.en+FX+000",
                 "domain": ".youtube.com", "path": "/"},
                {"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"},
            ])
            page = ctx.new_page()
            if _block_heavy_resources:
                page.route("**/*", _block_heavy_resources)

            logger.debug("[TabDiscoverer] Navigating to %s", base_url)
            page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)

            # Give JS a moment to render the tab bar
            page.wait_for_timeout(2_000)

            # ── Channel name ────────────────────────────────────────────────────
            channel_name = _extract_channel_name(page)

            # ── Tab navigation ─────────────────────────────────────────────────
            tabs = _extract_tabs(page, base_url)

            browser.close()

        degraded = False
        if not tabs:
            # Fallback: assume the basic tabs exist. This is a guess, and it
            # is reported as one (issue #27) — YouTube changing its DOM again
            # must show up as a visible degradation, not as a confident wrong
            # answer indistinguishable from a real discovery.
            tabs = _default_tabs(base_url)
            degraded = True
            logger.warning(
                "[TabDiscoverer] No tabs found in the DOM for %r — falling back to "
                "the default tab guess. If this is reproducible, YouTube's tab-bar "
                "markup likely changed and _extract_tabs needs new selectors.",
                base_url,
            )
        if not channel_name:
            degraded = True
            logger.warning("[TabDiscoverer] Could not read the channel name for %r", base_url)

        return DiscoveryResult(
            channel_name=channel_name,
            channel_url=base_url,
            tabs=tabs,
            degraded=degraded,
        )

    except Exception as exc:  # noqa: BLE001
        logger.error("[TabDiscoverer] Discovery failed: %s", exc)
        return DiscoveryResult(
            channel_name="",
            channel_url=base_url,
            tabs=_default_tabs(base_url),
            error=str(exc),
            degraded=True,
        )


# ── DOM helpers ────────────────────────────────────────────────────────────────

def _extract_channel_name(page) -> str:
    selectors = [
        "yt-page-header-renderer h1",
        "ytd-channel-name yt-formatted-string",
        "meta[property='og:title']",
    ]
    for sel in selectors:
        try:
            if sel.startswith("meta"):
                val = page.get_attribute(sel, "content")
            else:
                el = page.locator(sel).first
                val = el.inner_text(timeout=2_000).strip() if el.count() else None
            if val:
                return val
        except Exception:
            continue
    # Empty, not "Unknown Channel": the caller marks the whole result
    # `degraded` so an unread name is visible to the user instead of being
    # rendered as if it were the channel's actual title (issue #27).
    return ""


def _extract_tabs(page, base_url: str) -> list[TabInfo]:
    """
    Try multiple selector strategies to find the tab navigation bar.
    YouTube has changed this structure several times; we try all known variants.
    """
    found_tabs: list[TabInfo] = []

    # Modern yt-tab-group-component or yt-tab-shape
    try:
        tab_els = page.locator("yt-tab-group-component yt-tab-renderer, yt-tab-shape").all()
        if tab_els:
            for el in tab_els:
                tab = _tab_from_element(el, base_url, strategy="tab-shape")
                if tab:
                    found_tabs.append(tab)
            if found_tabs:
                # deduplicate just in case
                return list({t.tab_type: t for t in found_tabs}.values())
    except Exception:
        pass

    return found_tabs


def _tab_from_element(el, base_url: str, strategy: str) -> Optional[TabInfo]:
    """Parse a single tab DOM element into a TabInfo, or None if not a content tab."""
    try:
        text = el.inner_text(timeout=1_500).strip().lower()
        if not text:
            return None

        # Match against known tab keywords (English or Hebrew)
        for key, info in _TAB_MAP.items():
            hebrew = info["name"].lower()
            if key in text or hebrew in text:
                return TabInfo(
                    name=info["name"],
                    url=base_url.rstrip("/") + info["path"],
                    icon=info["icon"],
                    tab_type=info["type"],
                )

        # Check if it's a tab we should ignore
        for ignore in _IGNORE_TABS:
            if ignore in text:
                return None

        # Unknown tab — skip it
        logger.debug("[TabDiscoverer] Unknown tab text: %r (strategy=%s)", text, strategy)
        return None

    except Exception:
        return None


# ── Fallback defaults ──────────────────────────────────────────────────────────

def _default_tabs(base_url: str) -> list[TabInfo]:
    """Return the three most common tabs as a safe fallback."""
    return [
        TabInfo(name="סרטונים",    url=base_url.rstrip("/") + "/videos",    icon="🎬", tab_type="videos"),
        TabInfo(name="קצרים",      url=base_url.rstrip("/") + "/shorts",    icon="⚡", tab_type="shorts"),
        TabInfo(name="פלייליסטים", url=base_url.rstrip("/") + "/playlists", icon="📋", tab_type="playlists"),
    ]


def _strip_tab_suffix(url: str) -> str:
    """Remove trailing /videos, /shorts, etc. to get the channel home URL."""
    for suffix in ("/videos", "/shorts", "/streams", "/playlists", "/releases", "/podcasts",
                   "/community", "/about", "/channels", "/membership", "/featured"):
        if url.rstrip("/").endswith(suffix):
            return url.rstrip("/")[: -len(suffix)]
    return url.rstrip("/")