"""
core/youtube_reliability.py  –  YouTube-only conservative reliability mode
=============================================================================
YouTube is far stricter about automated traffic than Spotify or generic
sites: several parallel downloads with near-zero delay is exactly what
invites 403s, rate-limiting, and PO Token/bot challenges. This module
defines the conservative defaults (serialized downloads, a cooldown
between them, single-fragment concurrency) and the URL check used to
scope them to YouTube only.

Detection is done on the URL, not the UI's source-platform tag: a track
discovered via Spotify search still gets downloaded from a resolved
YouTube watch URL (Spotify itself is never downloaded directly — see
DownloadEngine.download's early Spotify-URL rejection), so checking
``req.platform`` alone would miss most real YouTube traffic.

Zero GUI imports.
"""

from __future__ import annotations

from urllib.parse import urlparse

# Conservative defaults for YouTube downloads. These are intentionally
# not user-configurable in this phase — only the on/off switch
# (AppConfig.youtube_reliability_mode: "conservative" | "fast") is exposed.
CONSERVATIVE_MAX_PARALLEL_YOUTUBE = 1
CONSERVATIVE_DELAY_RANGE          = (5.0, 10.0)   # seconds, between YouTube jobs
CONSERVATIVE_FRAGMENT_CONCURRENCY = 1

# Exact hostnames only — checked against the parsed URL's hostname, not
# via substring search. A substring check would treat
# "https://example.com/?redirect=youtube.com", "https://notyoutube.com",
# and "https://youtube.com.evil.test" as YouTube URLs; this doesn't.
_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
})


def is_youtube_url(url: str) -> bool:
    """
    True only when ``url`` parses to one of YouTube's known hostnames
    (youtube.com, www.youtube.com, m.youtube.com, music.youtube.com,
    youtu.be). False for Spotify, generic sites, look-alike domains, or
    empty/malformed input.
    """
    if not url:
        return False
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return False
    return bool(hostname) and hostname.lower() in _YOUTUBE_HOSTS
