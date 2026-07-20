"""
core/spotify_match_scorer.py  –  Confidence-scored Spotify→YouTube matching
============================================================================
When downloading a Spotify track, the app searches YouTube for the best
match.  Previously it used ``ytsearch1:<query>`` and blindly took the
first result.  This module scores multiple candidates and picks the best.

Scoring factors
---------------
* **Title similarity** (0–40 pts) — Normalized Levenshtein-like ratio
  between the Spotify title and the YouTube title, ignoring case, parens,
  brackets, and common suffixes like "Official Audio".
* **Duration match** (0–30 pts) — Full score within ±3s, linear decay to 0
  at ±15s difference, 0 beyond that.
* **Artist match** (0–20 pts) — Whether the Spotify artist name appears in
  the YouTube title or channel name.
* **Channel quality** (0–10 pts) — Bonus for official, topic, or
  artist-named channels.

Usage
-----
    from core.spotify_match_scorer import find_best_youtube_match

    result = find_best_youtube_match(
        title="Example Song",
        artist="Example Artist",
        duration_sec=240,
        max_candidates=5,
        cookies_file=None,
    )
    if result and result.confidence >= 0.5:
        download(result.url)

Zero GUI imports.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """A scored YouTube candidate for a Spotify track."""
    url:            str
    youtube_title:  str
    channel:        str
    duration_sec:   Optional[int]
    score:          float           # 0–100
    confidence:     float           # 0.0–1.0  (score / 100)
    breakdown:      dict            # individual factor scores


# ──────────────────────────────────────────────────────────────────────────────
# Text normalization
# ──────────────────────────────────────────────────────────────────────────────

# Patterns stripped before comparing titles
_STRIP_PATTERNS = [
    re.compile(r"\(official\s*(audio|video|music\s*video|lyric\s*video|visualizer)?\)", re.I),
    re.compile(r"\[official\s*(audio|video|music\s*video|lyric\s*video|visualizer)?\]", re.I),
    re.compile(r"\(lyrics?\)", re.I),
    re.compile(r"\[lyrics?\]", re.I),
    re.compile(r"\(audio\)", re.I),
    re.compile(r"\[audio\]", re.I),
    re.compile(r"\(feat\.?[^)]*\)", re.I),
    re.compile(r"\[feat\.?[^\]]*\]", re.I),
    re.compile(r"\(ft\.?[^)]*\)", re.I),
    re.compile(r"\(with\s+[^)]*\)", re.I),
    re.compile(r"[\u200b\u00a0]"),          # zero-width and non-breaking spaces
    re.compile(r"\s+"),                      # collapse whitespace
]

_BRANDED_CHANNEL_RE = re.compile(r"vevo", re.I)
_TOPIC_RE           = re.compile(r" - topic$", re.I)
_OFFICIAL_RE        = re.compile(r"official", re.I)


def _normalize(text: str) -> str:
    """Lowercase + strip noise patterns for comparison."""
    t = text.lower().strip()
    for pat in _STRIP_PATTERNS:
        t = pat.sub(" ", t)
    return t.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Scoring functions
# ──────────────────────────────────────────────────────────────────────────────

def _title_score(spotify_title: str, youtube_title: str, max_pts: float = 40.0) -> float:
    """Fuzzy title similarity score (0 – max_pts)."""
    a = _normalize(spotify_title)
    b = _normalize(youtube_title)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    return round(ratio * max_pts, 2)


def _duration_score(
    spotify_dur: Optional[int],
    youtube_dur: Optional[int],
    max_pts: float = 30.0,
) -> float:
    """
    Duration match score (0 – max_pts).
    Full score within ±3s, linear decay to 0 at ±15s.
    """
    if spotify_dur is None or youtube_dur is None:
        return max_pts * 0.3   # unknown → partial credit
    diff = abs(spotify_dur - youtube_dur)
    if diff <= 3:
        return max_pts
    if diff >= 15:
        return 0.0
    # Linear decay between 3 and 15
    return round(max_pts * (1.0 - (diff - 3) / 12.0), 2)


def _artist_score(
    spotify_artist: str,
    youtube_title: str,
    channel: str,
    max_pts: float = 20.0,
) -> float:
    """
    Artist presence score (0 – max_pts).
    Checks if the artist name appears in the YT title or channel name.
    """
    artist_lower   = spotify_artist.lower().strip()
    artist_nospace = artist_lower.replace(" ", "")
    if not artist_lower:
        return 0.0

    yt_title_lower = youtube_title.lower()
    chan_lower     = channel.lower()
    chan_nospace   = chan_lower.replace(" ", "")

    in_title   = artist_lower in yt_title_lower
    in_channel = artist_lower in chan_lower or (len(artist_nospace) > 3 and artist_nospace in chan_nospace)

    if in_title and in_channel:
        return max_pts
    if in_title or in_channel:
        return max_pts * 0.8  # Increased from 0.7 for better "perfect match" score

    # Partial: check if first word of artist appears
    words = artist_lower.split()
    first_word = words[0] if words else ""
    if first_word and len(first_word) > 2:
        if first_word in youtube_title.lower() or first_word in channel.lower():
            return max_pts * 0.3
    return 0.0


def _channel_score(channel: str, spotify_artist: str, max_pts: float = 10.0) -> float:
    """
    Channel quality bonus (0 – max_pts).
    Branded, topic, official, or artist-named channels get points.
    """
    if not channel:
        return 0.0
    pts = 0.0
    if _BRANDED_CHANNEL_RE.search(channel):
        pts += max_pts * 0.5
    if _TOPIC_RE.search(channel):
        pts += max_pts * 0.5
    if _OFFICIAL_RE.search(channel):
        pts += max_pts * 0.3
    if spotify_artist.lower().strip() in channel.lower():
        pts += max_pts * 0.3
    return min(pts, max_pts)


def score_candidate(
    spotify_title:  str,
    spotify_artist: str,
    spotify_dur:    Optional[int],
    yt_title:       str,
    yt_channel:     str,
    yt_dur:         Optional[int],
) -> tuple[float, dict]:
    """
    Score a single YouTube candidate against Spotify metadata.

    Returns (total_score, breakdown_dict).
    """
    # ── Precision Title Score ─────────────────────────────────────────
    # If the YT title starts with "Artist - ", strip it for a better match
    yt_title_clean = yt_title
    artist_prefix = f"{spotify_artist} -"
    if yt_title.lower().startswith(artist_prefix.lower()):
        yt_title_clean = yt_title[len(artist_prefix):].strip()
    elif " - " in yt_title:
        # Check if the title is "Title - Artist"
        parts = yt_title.split(" - ")
        if spotify_artist.lower() in parts[1].lower():
            yt_title_clean = parts[0].strip()

    t = _title_score(spotify_title, yt_title_clean)
    d = _duration_score(spotify_dur, yt_dur)
    a = _artist_score(spotify_artist, yt_title, yt_channel)
    c = _channel_score(yt_channel, spotify_artist)
    total = t + d + a + c
    breakdown = {
        "title": t,
        "duration": d,
        "artist": a,
        "channel": c,
    }
    return total, breakdown


# ──────────────────────────────────────────────────────────────────────────────
# High-level resolver
# ──────────────────────────────────────────────────────────────────────────────

def find_best_youtube_match(
    title:          str,
    artist:         str,
    duration_sec:   Optional[int] = None,
    max_candidates: int = 5,
    cookies_file:   Optional[str] = None,
    min_confidence: float = 0.35,
) -> Optional[MatchResult]:
    """
    Search YouTube for multiple candidates and return the best-scoring one.

    Uses yt-dlp's ``ytsearchN:`` prefix to fetch N results, then scores
    each and returns the highest.

    Parameters
    ----------
    title, artist    : Spotify track metadata.
    duration_sec     : Expected duration (improves accuracy significantly).
    max_candidates   : How many YouTube results to evaluate (1–10).
    cookies_file     : Optional cookies for authenticated searches.
    min_confidence   : Minimum confidence (0–1) to accept a match.

    Returns
    -------
    MatchResult with the best candidate, or None if nothing meets
    min_confidence.
    """
    import yt_dlp
    from utils.yt_dlp_opts import build_base_ydl_opts, temp_cookies_copy
    from utils.logger import SilentLogger

    query = f"ytsearch{max_candidates}:{artist} {title} audio"
    logger.debug("[MatchScorer] Searching: %s", query)

    # find_best_youtube_match() runs concurrently (up to 5 at once, see
    # _parallel_resolve_urls) — a private copy of cookies_file avoids
    # racing other threads on the same shared, yt-dlp-rewritten file.
    with temp_cookies_copy(cookies_file) as cf:
        opts = build_base_ydl_opts(
            cookies_file=cf,
            logger=SilentLogger(),
            quiet=True,
            retries=1,
            socket_timeout=8,
        )
        opts.update({
            "extract_flat": False,
            "skip_download": True,
            "no_warnings": True,
            "extractor_retries": 1,
            # One blocked/age-gated candidate must not sink the other 4 — we
            # only need a single confident match out of max_candidates.
            "ignoreerrors": True,
        })

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
        except Exception as exc:
            # Routine — the caller always has a further fallback (last-resort
            # ytsearch1), so this is expected traffic, not an actionable error.
            logger.debug("[MatchScorer] yt-dlp search failed: %s", exc)
            return None

    entries = info.get("entries") or []
    if not entries:
        logger.debug("[MatchScorer] No YouTube results for: %s - %s", artist, title)
        return None

    best: Optional[MatchResult] = None

    for entry in entries:
        if not entry:
            continue
        yt_title   = entry.get("title") or ""
        yt_channel = entry.get("channel") or entry.get("uploader") or ""
        yt_dur     = None
        raw_dur    = entry.get("duration")
        if raw_dur is not None:
            try:
                yt_dur = int(raw_dur)
            except (TypeError, ValueError):
                pass

        yt_url = (
            entry.get("webpage_url")
            or entry.get("url")
            or (f"https://www.youtube.com/watch?v={entry['id']}" if entry.get("id") else "")
        )
        if not yt_url:
            continue

        total, breakdown = score_candidate(
            title, artist, duration_sec,
            yt_title, yt_channel, yt_dur,
        )
        confidence = total / 100.0

        logger.debug(
            "[MatchScorer]   %.0f pts (conf=%.2f) — %s [%s] dur=%s",
            total, confidence, yt_title[:50], yt_channel[:20], yt_dur,
        )

        if best is None or total > best.score:
            best = MatchResult(
                url=yt_url,
                youtube_title=yt_title,
                channel=yt_channel,
                duration_sec=yt_dur,
                score=total,
                confidence=confidence,
                breakdown=breakdown,
            )

    if best is None:
        return None

    if best.confidence < min_confidence:
        logger.debug(
            "[MatchScorer] Best match (%.0f%%) below threshold (%.0f%%) for: %s - %s",
            best.confidence * 100, min_confidence * 100, artist, title,
        )
        return None

    logger.info(
        "[MatchScorer] Best match: %.0f%% — \"%s\" by [%s]",
        best.confidence * 100, best.youtube_title[:60], best.channel[:30],
    )
    return best
