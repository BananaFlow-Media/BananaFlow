"""Deterministic, recording-aware Spotify -> YouTube matching.

The matcher deliberately separates *recording identity* (artist, title,
duration and version such as live/remix/acoustic) from presentation labels
such as "official audio" or "lyric video".  A candidate is never accepted on
score alone: the identity gates in :func:`assess_candidate` must also pass.

The network path starts with a flat yt-dlp search.  Decisive results avoid the
old eager extraction of every result; ambiguous results receive bounded deep
validation.  This keeps the common path cheap without making speed a reason to
accept an unproved cover, remix, or performance.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Cache rows made by older matchers are intentionally invisible.
MATCH_ALGO_VERSION = 3


@dataclass(frozen=True)
class RecordingIntent:
    base_title: str
    versions: frozenset[str]
    qualifiers: frozenset[str]


@dataclass
class MatchResult:
    url: str
    youtube_title: str
    channel: str
    duration_sec: Optional[int]
    score: float
    confidence: float
    breakdown: dict
    safe: bool = True
    evidence_quality: str = "complete"


_PRESENTATION_RE = re.compile(
    r"\b(?:official(?:\s+music)?\s+(?:audio|video)|official|audio|music\s+video|"
    r"lyric(?:s)?(?:\s+video)?|visuali[sz]er|hd|4k)\b",
    re.I,
)
_FEAT_RE = re.compile(r"\b(?:feat(?:uring)?|ft|with)\.?\s+[^\]\[()]+", re.I)
_TRAILING_YEAR_RE = re.compile(r"(?:\s*[-–—]\s*)?(?:19|20)\d{2}\s*$")
_VERSION_YEAR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "remaster",
        re.compile(
            r"(?:\b((?:19|20)\d{2})\s+re-?master(?:ed)?\b|"
            r"\bre-?master(?:ed)?\s+((?:19|20)\d{2})\b)",
            re.I,
        ),
    ),
    (
        "remix",
        re.compile(
            r"(?:\b((?:19|20)\d{2})\s+(?:re-?mix(?:ed)?|mix)\b|"
            r"\b(?:re-?mix(?:ed)?|mix)\s+((?:19|20)\d{2})\b)",
            re.I,
        ),
    ),
)

# Patterns use token boundaries so, for example, "live" does not match a
# fragment inside an artist or song name. Hebrew cover labels are included for
# the regression that motivated the withdrawn experiment.
_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cover", re.compile(r"(?:\bcover(?:ed|s)?\b|קאבר)", re.I)),
    ("live", re.compile(r"\b(?:live|concert|in\s+concert)\b", re.I)),
    ("remix", re.compile(r"\b(?:re-?mix(?:ed)?|mix)\b", re.I)),
    ("acoustic", re.compile(r"\b(?:acoustic|unplugged)\b", re.I)),
    ("instrumental", re.compile(r"\binstrumental\b", re.I)),
    ("karaoke", re.compile(r"\bkaraoke\b", re.I)),
    ("remaster", re.compile(r"\b(?:re-?master(?:ed)?|\d{4}\s+remaster)\b", re.I)),
    ("demo", re.compile(r"\bdemo\b", re.I)),
    ("radio_edit", re.compile(r"\bradio\s+(?:edit|version)\b", re.I)),
    ("extended", re.compile(r"\bextended(?:\s+(?:edit|version|mix))?\b", re.I)),
    ("sped_up", re.compile(r"\b(?:sped\s*up|nightcore)\b", re.I)),
    ("slowed", re.compile(r"\b(?:slowed|slow\s+version|reverb(?:ed)?)\b", re.I)),
    ("mono", re.compile(r"\bmono(?:phonic)?\b", re.I)),
)

_BRANDED_CHANNEL_RE = re.compile(r"\bvevo\b", re.I)
_TOPIC_RE = re.compile(r"\s+-\s+topic$", re.I)
_OFFICIAL_RE = re.compile(r"\bofficial\b", re.I)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    text = text.replace("\u200b", " ").replace("\u00a0", " ")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def recording_intent(title: str) -> RecordingIntent:
    raw = unicodedata.normalize("NFKC", title or "")
    versions = frozenset(name for name, pattern in _VERSION_PATTERNS if pattern.search(raw))
    qualifiers = set()
    for version, pattern in _VERSION_YEAR_PATTERNS:
        match = pattern.search(raw)
        if match:
            year = next((group for group in match.groups() if group), "")
            if year:
                qualifiers.add(f"{version}:{year}")
    base = _PRESENTATION_RE.sub(" ", raw)
    base = _FEAT_RE.sub(" ", base)
    for _name, pattern in _VERSION_PATTERNS:
        base = pattern.sub(" ", base)
    base = _TRAILING_YEAR_RE.sub(" ", base)
    return RecordingIntent(_fold(base), versions, frozenset(qualifiers))


def _normalize(text: str) -> str:
    """Compatibility helper used by existing tests and callers."""
    return recording_intent(text).base_title


def _tokens(text: str) -> set[str]:
    return {token for token in _fold(text).split() if token}


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    seq = SequenceMatcher(None, left, right).ratio()
    lt, rt = _tokens(left), _tokens(right)
    overlap = len(lt & rt) / max(1, min(len(lt), len(rt)))
    return max(seq, overlap)


def _artist_variants(artist: str) -> list[str]:
    # Keep the full credit string first, then individual credited artists.
    folded = _fold(artist)
    parts = re.split(r"\s*(?:,|&|\band\b|\bx\b|\bfeat(?:uring)?\b|\bft\b|/|;)\s*", folded)
    values = [folded] + [part.strip() for part in parts if len(part.strip()) >= 2]
    return list(dict.fromkeys(value for value in values if value))


def _artist_evidence(
    spotify_artist: str,
    youtube_title: str,
    channel: str,
    yt_artists: Optional[Iterable[str]] = None,
) -> float:
    variants = _artist_variants(spotify_artist)
    if not variants:
        return 0.0
    fields = [_fold(youtube_title), _fold(channel)]
    fields.extend(_fold(item) for item in (yt_artists or []) if item)
    best = 0.0
    for variant in variants:
        vt = _tokens(variant)
        for field in fields:
            if not field:
                continue
            compact_variant = variant.replace(" ", "")
            compact_field = field.replace(" ", "")
            if variant in field or (
                len(compact_variant) >= 4 and compact_variant in compact_field
            ):
                best = max(best, 1.0)
                continue
            ft = _tokens(field)
            coverage = len(vt & ft) / max(1, len(vt))
            best = max(best, coverage)
    return best


def _candidate_title_without_artist(title: str, artist: str) -> str:
    # "Artist & Guest - Song" is common. Strip the left side only when it
    # contains evidence for the requested artist; otherwise the dash may be
    # part of the song title.
    for separator in (" - ", " – ", " — "):
        if separator in title:
            left, right = title.split(separator, 1)
            if _artist_evidence(artist, left, "") >= 0.72:
                return right
    return title


def _title_score(spotify_title: str, youtube_title: str, max_pts: float = 40.0) -> float:
    return round(
        _similarity(recording_intent(spotify_title).base_title,
                    recording_intent(youtube_title).base_title) * max_pts,
        2,
    )


def _duration_score(
    spotify_dur: Optional[int], youtube_dur: Optional[int], max_pts: float = 30.0,
) -> float:
    if spotify_dur is None or youtube_dur is None:
        return max_pts * 0.3
    diff = abs(int(spotify_dur) - int(youtube_dur))
    if diff <= 3:
        return max_pts
    tolerance = max(15.0, float(spotify_dur) * 0.08)
    if diff >= tolerance:
        return 0.0
    return round(max_pts * (1.0 - (diff - 3.0) / max(1.0, tolerance - 3.0)), 2)


def _artist_score(
    spotify_artist: str, youtube_title: str, channel: str, max_pts: float = 20.0,
) -> float:
    title_evidence = _artist_evidence(spotify_artist, youtube_title, "")
    channel_evidence = _artist_evidence(spotify_artist, "", channel)
    evidence = max(title_evidence, channel_evidence)
    if title_evidence >= 0.95 and channel_evidence >= 0.95:
        return max_pts
    if evidence >= 0.72:
        return max_pts * 0.8
    if evidence >= 0.45:
        return max_pts * 0.3
    return 0.0


def _channel_score(channel: str, spotify_artist: str, max_pts: float = 10.0) -> float:
    if not channel:
        return 0.0
    pts = 0.0
    if _BRANDED_CHANNEL_RE.search(channel):
        pts += max_pts * 0.5
    if _TOPIC_RE.search(channel):
        pts += max_pts * 0.5
    if _OFFICIAL_RE.search(channel):
        pts += max_pts * 0.3
    if _artist_evidence(spotify_artist, "", channel) >= 0.72:
        pts += max_pts * 0.3
    return min(pts, max_pts)


def assess_candidate(
    spotify_title: str,
    spotify_artist: str,
    spotify_dur: Optional[int],
    yt_title: str,
    yt_channel: str,
    yt_dur: Optional[int],
    *,
    yt_artists: Optional[Iterable[str]] = None,
) -> tuple[float, dict, bool]:
    structured_artists = tuple(item for item in (yt_artists or ()) if item)
    clean_candidate_title = _candidate_title_without_artist(yt_title, spotify_artist)
    source_intent = recording_intent(spotify_title)
    candidate_intent = recording_intent(clean_candidate_title)
    title_similarity = _similarity(source_intent.base_title, candidate_intent.base_title)
    artist_evidence = _artist_evidence(
        spotify_artist, yt_title, yt_channel, structured_artists,
    )
    structured_artist_evidence = _artist_evidence(
        spotify_artist, "", "", structured_artists,
    ) if structured_artists else None
    duration_delta = (
        abs(int(spotify_dur) - int(yt_dur))
        if spotify_dur is not None and yt_dur is not None else None
    )

    title_pts = round(title_similarity * 40.0, 2)
    duration_pts = _duration_score(spotify_dur, yt_dur)
    artist_pts = round(
        (structured_artist_evidence if structured_artist_evidence is not None else artist_evidence)
        * 20.0,
        2,
    )
    channel_pts = _channel_score(yt_channel, spotify_artist)

    reasons: list[str] = []
    if title_similarity < 0.68:
        reasons.append("title")
    # Structured YTMusic artist credits are stronger evidence than a title
    # mention. A cover performer can put the original artists in its title;
    # accepting that mention over contradictory structured credits would pick
    # the wrong recording.
    if spotify_artist and structured_artist_evidence is not None:
        if structured_artist_evidence < 0.72:
            reasons.append("artist")
    elif spotify_artist and artist_evidence < 0.72:
        reasons.append("artist")
    if source_intent.versions != candidate_intent.versions:
        reasons.append("version")
    if (
        source_intent.qualifiers
        and source_intent.qualifiers != candidate_intent.qualifiers
    ):
        reasons.append("version_qualifier")
    if duration_delta is not None:
        max_delta = max(15.0, float(spotify_dur or 0) * 0.08)
        if duration_delta > max_delta:
            reasons.append("duration")

    safe = not reasons
    # Unsafe candidates remain rankable for diagnostics/deep-validation
    # ordering, but receive no confidence boost from presentation/channel.
    total = title_pts + duration_pts + artist_pts + channel_pts
    if not safe:
        total = min(total, 54.0)
    breakdown = {
        "title": title_pts,
        "duration": duration_pts,
        "artist": artist_pts,
        "channel": channel_pts,
        "title_similarity": round(title_similarity, 4),
        "artist_evidence": round(artist_evidence, 4),
        "structured_artist_evidence": (
            round(structured_artist_evidence, 4)
            if structured_artist_evidence is not None else None
        ),
        "source_versions": sorted(source_intent.versions),
        "candidate_versions": sorted(candidate_intent.versions),
        "source_version_qualifiers": sorted(source_intent.qualifiers),
        "candidate_version_qualifiers": sorted(candidate_intent.qualifiers),
        "duration_delta": duration_delta,
        "reject_reasons": reasons,
    }
    return round(total, 2), breakdown, safe


def score_candidate(
    spotify_title: str,
    spotify_artist: str,
    spotify_dur: Optional[int],
    yt_title: str,
    yt_channel: str,
    yt_dur: Optional[int],
    *,
    yt_artists: Optional[Iterable[str]] = None,
) -> tuple[float, dict]:
    score, breakdown, _safe = assess_candidate(
        spotify_title, spotify_artist, spotify_dur,
        yt_title, yt_channel, yt_dur, yt_artists=yt_artists,
    )
    return score, breakdown


def match_from_metadata(
    *,
    url: str,
    title: str,
    artist: str,
    duration_sec: Optional[int],
    yt_title: str,
    yt_channel: str,
    yt_duration_sec: Optional[int],
    yt_artists: Optional[Iterable[str]] = None,
) -> MatchResult:
    score, breakdown, safe = assess_candidate(
        title, artist, duration_sec, yt_title, yt_channel, yt_duration_sec,
        yt_artists=yt_artists,
    )
    evidence = "complete" if (duration_sec is None or yt_duration_sec is not None) else "partial"
    return MatchResult(
        url=url, youtube_title=yt_title, channel=yt_channel,
        duration_sec=yt_duration_sec, score=score, confidence=score / 100.0,
        breakdown=breakdown, safe=safe, evidence_quality=evidence,
    )


def _entry_url(entry: dict) -> str:
    webpage = entry.get("webpage_url") or entry.get("original_url") or ""
    if isinstance(webpage, str) and webpage.startswith(("http://", "https://")):
        return webpage
    video_id = entry.get("id")
    raw_url = entry.get("url")
    if not video_id and isinstance(raw_url, str) and re.fullmatch(r"[\w-]{6,}", raw_url):
        video_id = raw_url
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return raw_url if isinstance(raw_url, str) and raw_url.startswith("http") else ""


def _entry_match(entry: dict, title: str, artist: str, duration_sec: Optional[int]) -> Optional[MatchResult]:
    url = _entry_url(entry)
    if not url:
        return None
    raw_duration = entry.get("duration")
    try:
        yt_duration = int(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        yt_duration = None
    return match_from_metadata(
        url=url, title=title, artist=artist, duration_sec=duration_sec,
        yt_title=entry.get("title") or "",
        yt_channel=entry.get("channel") or entry.get("uploader") or "",
        yt_duration_sec=yt_duration,
    )


def _rank(matches: Iterable[MatchResult]) -> list[MatchResult]:
    return sorted(
        matches,
        key=lambda item: (
            not item.safe,
            -item.score,
            item.breakdown.get("duration_delta")
            if item.breakdown.get("duration_delta") is not None else 10**9,
            _fold(item.youtube_title),
            _fold(item.channel),
            item.url,
        ),
    )


def _search(
    query: str,
    *,
    extract_flat: bool,
    cookies_file: Optional[str],
) -> list[dict]:
    import yt_dlp
    from utils.logger import SilentLogger
    from utils.yt_dlp_opts import build_base_ydl_opts, temp_cookies_copy

    with temp_cookies_copy(cookies_file) as cookie_copy:
        opts = build_base_ydl_opts(
            cookies_file=cookie_copy, logger=SilentLogger(), quiet=True,
            retries=1, socket_timeout=8, respect_po_token_circuit=False,
        )
        opts.update({
            "extract_flat": extract_flat,
            "skip_download": True,
            "no_warnings": True,
            "extractor_retries": 1,
            "ignoreerrors": True,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False) or {}
    return [entry for entry in (info.get("entries") or []) if entry]


def _deep_validate_urls(
    urls: Iterable[str],
    *,
    title: str,
    artist: str,
    duration_sec: Optional[int],
    cookies_file: Optional[str],
) -> list[MatchResult]:
    import yt_dlp
    from utils.logger import SilentLogger
    from utils.yt_dlp_opts import build_base_ydl_opts, temp_cookies_copy

    validated: list[MatchResult] = []
    with temp_cookies_copy(cookies_file) as cookie_copy:
        opts = build_base_ydl_opts(
            cookies_file=cookie_copy, logger=SilentLogger(), quiet=True,
            retries=1, socket_timeout=8, respect_po_token_circuit=False,
        )
        opts.update({"skip_download": True, "no_warnings": True, "extractor_retries": 1})
        with yt_dlp.YoutubeDL(opts) as ydl:
            for url in urls:
                try:
                    entry = ydl.extract_info(url, download=False) or {}
                except Exception as exc:  # one unavailable result must not sink the rest
                    logger.debug("[MatchScorer] deep validation failed for %s: %s", url, exc)
                    continue
                match = _entry_match(entry, title, artist, duration_sec)
                if match:
                    validated.append(match)
    return validated


def find_best_youtube_match(
    title: str,
    artist: str,
    duration_sec: Optional[int] = None,
    max_candidates: int = 8,
    cookies_file: Optional[str] = None,
    min_confidence: float = 0.55,
    exclude_urls: Optional[set[str]] = None,
) -> Optional[MatchResult]:
    """Return the best identity-safe candidate, never merely the first hit."""
    excluded = exclude_urls or set()
    query = f"ytsearch{max(1, min(max_candidates, 10))}:{artist} {title}".strip()
    try:
        entries = _search(query, extract_flat=True, cookies_file=cookies_file)
    except Exception as exc:
        logger.debug("[MatchScorer] flat search failed: %s", exc)
        entries = []

    flat = _rank(
        match for entry in entries
        if (match := _entry_match(entry, title, artist, duration_sec)) is not None
        and match.url not in excluded
    )
    safe_flat = [item for item in flat if item.safe and item.confidence >= min_confidence]

    # A complete, well-separated flat result is already proven. A close
    # runner-up or missing duration is ambiguous and receives bounded detail
    # extraction below.
    if safe_flat:
        best = safe_flat[0]
        runner_score = safe_flat[1].score if len(safe_flat) > 1 else -1.0
        if best.evidence_quality == "complete" and best.score - runner_score >= 8.0:
            return best

    # Validate at most the three strongest semantic candidates. This is the
    # quality guard omitted by the original flat-search experiment.
    validation_urls = [item.url for item in flat[:3]]
    try:
        deep = _rank(_deep_validate_urls(
            validation_urls, title=title, artist=artist,
            duration_sec=duration_sec, cookies_file=cookies_file,
        )) if validation_urls else []
    except Exception as exc:
        logger.debug("[MatchScorer] bounded validation failed: %s", exc)
        deep = []

    safe_deep = [
        item for item in deep
        if item.safe and item.confidence >= min_confidence and item.url not in excluded
    ]
    if safe_deep:
        return safe_deep[0]

    # If duration was unavailable at both layers, a high-evidence flat result
    # can still be safe when title, artist, and version all agree. It is marked
    # partial so callers/tests can distinguish it from a fully validated hit.
    if safe_flat and safe_flat[0].score >= max(72.0, min_confidence * 100.0):
        return safe_flat[0]
    return None
