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


# Version of the Spotify→YouTube matching logic.  It is part of the match
# cache key (see ``core.match_cache``): bump it whenever the scoring weights,
# thresholds, or candidate-selection logic below change, so previously cached
# matches produced by the old algorithm are transparently treated as misses
# and recomputed instead of served stale.
MATCH_ALGO_VERSION = 3


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

    Uses yt-dlp's ``ytsearchN:`` prefix to fetch N *flat* results, scores
    them, then deep-validates only an ambiguous leading candidate (or the top
    two when they are close).  Search cards normally carry the title, channel,
    duration, id, and URL needed by this scorer; selective validation retains
    that fast path without accepting a weak flat result merely because its
    missing metadata earned partial credit.

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
            enable_po_token_provider=False,
        )
        opts.update({
            # A search result is sufficient for matching: the YoutubeTab
            # extractor supplies id, URL, title, duration, channel and
            # uploader without resolving every candidate's watch page.
            "extract_flat": True,
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

    candidates: list[tuple[dict, MatchResult]] = []

    def scored(entry: dict, *, fallback_url: str = "") -> Optional[MatchResult]:
        if not entry:
            return None
        yt_title   = entry.get("title") or ""
        yt_channel = entry.get("channel") or entry.get("uploader") or ""
        yt_dur     = None
        raw_dur    = entry.get("duration")
        if raw_dur is not None:
            try:
                yt_dur = int(raw_dur)
            except (TypeError, ValueError):
                pass

        yt_url = entry.get("webpage_url") or entry.get("url") or fallback_url
        # Flat extractors normally return a canonical watch URL, but an
        # extractor may expose an opaque URL token.  The stable video id is
        # enough to form a portable URL and is safer than passing that token to
        # the download engine.
        if not yt_url.startswith(("http://", "https://")):
            video_id = entry.get("id")
            yt_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        if not yt_url:
            return None

        total, breakdown = score_candidate(
            title, artist, duration_sec,
            yt_title, yt_channel, yt_dur,
        )
        confidence = total / 100.0

        logger.debug(
            "[MatchScorer]   %.0f pts (conf=%.2f) — %s [%s] dur=%s",
            total, confidence, yt_title[:50], yt_channel[:20], yt_dur,
        )

        return MatchResult(
            url=yt_url,
            youtube_title=yt_title,
            channel=yt_channel,
            duration_sec=yt_dur,
            score=total,
            confidence=confidence,
            breakdown=breakdown,
        )

    for entry in entries:
        result = scored(entry)
        if result is not None:
            candidates.append((entry, result))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[1].score, reverse=True)
    best = candidates[0][1]

    # Flat results are the ordinary path.  Deep extraction is deliberately
    # limited to the cases where the flat ranking lacks decisive evidence:
    # absent duration/channel, a score near the acceptance boundary, or two
    # close leaders.  Validate both close leaders because validating only the
    # current winner cannot reveal that the runner-up has the better real
    # duration/channel data.
    validate_indices = {0}
    if (
        best.duration_sec is not None
        and best.channel
        and best.confidence >= min_confidence + 0.10
        and (len(candidates) == 1 or best.score - candidates[1][1].score > 8.0)
    ):
        validate_indices.clear()
    if len(candidates) > 1 and best.score - candidates[1][1].score <= 8.0:
        validate_indices.add(1)

    for index in sorted(validate_indices):
        flat_entry, flat_result = candidates[index]
        try:
            # The flat-search cookie copy has been released above.  Deep
            # validation must therefore obtain its own short-lived copy rather
            # than retaining a now-deleted cookiefile path in ``opts``.
            with temp_cookies_copy(cookies_file) as deep_cf:
                deep_opts = build_base_ydl_opts(
                    cookies_file=deep_cf,
                    logger=SilentLogger(),
                    quiet=True,
                    retries=1,
                    socket_timeout=8,
                )
                deep_opts.update({
                    "extract_flat": False,
                    "skip_download": True,
                    "ignoreerrors": True,
                })
                with yt_dlp.YoutubeDL(deep_opts) as ydl:
                    deep_info = ydl.extract_info(flat_result.url, download=False)
        except Exception as exc:  # noqa: BLE001 - retain the flat candidate
            logger.debug("[MatchScorer] Deep validation failed for %s: %s", flat_result.url, exc)
            continue
        if not isinstance(deep_info, dict) or not deep_info.get("id"):
            continue
        refined = scored(deep_info, fallback_url=flat_result.url)
        if refined is not None:
            candidates[index] = (flat_entry, refined)

    candidates.sort(key=lambda item: item[1].score, reverse=True)
    best = candidates[0][1]

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
