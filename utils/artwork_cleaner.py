"""
utils/artwork_cleaner.py  –  Thumbnail URL sanitization
=======================================================
Transforms raw platform thumbnail URLs into high-resolution, square (1:1)
versions to ensure consistent UI display and high-quality embedding.
"""

import re
from urllib.parse import parse_qs, urlparse

from utils.url_cleaner import host_matches_domain


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_STANDARD_THUMBNAILS = (
    "maxresdefault.jpg",
    "sddefault.jpg",
    "hqdefault.jpg",
    "mqdefault.jpg",
    "default.jpg",
)


def _valid_youtube_id(value: str) -> str:
    value = (value or "").strip()
    return value if _YOUTUBE_ID_RE.fullmatch(value) else ""


def extract_youtube_video_id(url_or_id: str) -> str:
    """Return a YouTube video ID only from a trusted YouTube/thumbnail URL.

    The old implementation searched for strings such as ``v=`` or
    ``youtu.be/`` anywhere in an arbitrary URL. That made a non-YouTube URL
    with a matching path/query look like a YouTube thumbnail. Parse the
    hostname first and accept only known YouTube/ytimg URL shapes.
    """
    if not url_or_id:
        return ""

    raw = url_or_id.strip()
    direct = _valid_youtube_id(raw)
    if direct:
        return direct

    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""

    host = (parsed.hostname or "").lower()
    if not host:
        return ""

    if host_matches_domain(host, "youtu.be"):
        return _valid_youtube_id(parsed.path.strip("/").split("/", 1)[0])

    if host_matches_domain(host, "youtube.com", "youtube-nocookie.com"):
        query_id = _valid_youtube_id((parse_qs(parsed.query).get("v") or [""])[0])
        if query_id:
            return query_id

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() in {"shorts", "embed", "live", "v"}:
            return _valid_youtube_id(parts[1])
        return ""

    if host_matches_domain(host, "ytimg.com"):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].lower() in {"vi", "vi_webp"}:
            return _valid_youtube_id(parts[1])

    return ""


def get_youtube_thumbnail_candidates(url_or_id: str) -> list[str]:
    """Return unique YouTube thumbnail candidates in reliability order.

    Standard static variants always stay highest-quality-first. If yt-dlp
    supplied a non-standard ytimg variant (for example a live/special image),
    keep that exact URL first so the fallback layer does not discard useful
    extractor knowledge.
    """
    video_id = extract_youtube_video_id(url_or_id)
    if not video_id:
        return [url_or_id] if url_or_id else []

    standard = [
        f"https://i.ytimg.com/vi/{video_id}/{name}"
        for name in _YOUTUBE_STANDARD_THUMBNAILS
    ]
    candidates: list[str] = []

    try:
        parsed = urlparse(url_or_id)
        if parsed.hostname and host_matches_domain(parsed.hostname, "ytimg.com"):
            basename = parsed.path.rsplit("/", 1)[-1].lower()
            # A standard lower-quality URL must not jump ahead of maxres.
            # Preserve only extractor-specific variants before the normal
            # quality ladder.
            if basename not in _YOUTUBE_STANDARD_THUMBNAILS:
                candidates.append(url_or_id)
    except ValueError:
        pass

    candidates.extend(standard)
    return list(dict.fromkeys(candidates))


def clean_artwork_url(url: str, platform) -> str:
    """
    Transform a raw thumbnail URL into a high-res square version if possible.

    Rules
    -----
    YouTube Music (lh3.googleusercontent / yt3.ggpht):
        Replace size suffixes like =w120-h120-l90-rj with =w1024-h1024-p-rj.

    YouTube (i.ytimg.com):
        Prefer maxresdefault.jpg.

    Spotify:
        Usually already square, return as-is.
    """
    from core.playlist_parser import SourcePlatform
    if not url:
        return ""

    if platform == SourcePlatform.YOUTUBE_MUSIC:
        pattern = r'(=|-)(w|s)\d+(-b\d+)?(-c)?(-h\d+)?(-[a-z0-9]+)*$'
        if re.search(pattern, url):
            return re.sub(pattern, r'\1w1200-h1200-p-rj', url)

        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""
        if host_matches_domain(host, "googleusercontent.com", "ggpht.com") and "=" not in url:
            return f"{url}=w1200-h1200-p-rj"

    elif platform == SourcePlatform.YOUTUBE:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
        except ValueError:
            host = ""

        if host_matches_domain(host, "ytimg.com"):
            url = url.split("?", 1)[0]
            for quality in ["hqdefault.jpg", "mqdefault.jpg", "sddefault.jpg", "default.jpg", "hq720.jpg"]:
                if quality in url:
                    return url.replace(quality, "maxresdefault.jpg")

    return url
