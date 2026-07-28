"""Reproducible Spotify-to-YouTube matching validation.

The deterministic corpus is the release gate and lives in
``tests/fixtures/spotify_match_corpus.json``.  This script complements it with
current YouTube Music search results and an optional flat-vs-deep yt-dlp
latency comparison.  Correctness is based only on returned metadata
(title/artist/duration/recording version); no historical video ID is expected
or printed.

Examples::

    python scripts/validate_spotify_matching.py
    python scripts/validate_spotify_matching.py --live --limit 12
    python scripts/validate_spotify_matching.py --compare-fallback 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from core.spotify_match_scorer import _search, match_from_metadata


ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "tests" / "fixtures" / "spotify_match_corpus.json"

# Cases whose source metadata describes a real released recording.  Synthetic
# adversarial entries remain in the deterministic corpus but are not useful as
# live search queries.
LIVE_CASE_NAMES = (
    "hebrew withdrawn-pr cover regression",
    "neutral rejects cover",
    "neutral rejects live",
    "requested live preserved",
    "neutral rejects remix",
    "requested remix preserved",
    "acoustic intent",
    "remaster intent",
    "explicit remaster wording",
    "radio edit intent",
    "presentation lyrics is same recording",
    "multi artist credit",
    "wrong artist same title and duration",
    "hebrew live intent",
)


@dataclass(frozen=True)
class LiveResult:
    case: str
    elapsed_seconds: float
    candidates: int
    outcome: str
    selected_title: str = ""
    selected_artist: str = ""
    selected_duration: int | None = None
    score: float | None = None
    source_versions: tuple[str, ...] = ()
    selected_versions: tuple[str, ...] = ()
    source_version_qualifiers: tuple[str, ...] = ()
    selected_version_qualifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class FallbackTiming:
    case: str
    flat_seconds: float | None
    deep_seconds: float | None
    flat_candidates: int
    deep_candidates: int
    error: str = ""


def load_corpus() -> list[dict]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _duration_seconds(result: dict) -> int | None:
    raw = result.get("duration_seconds")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    text = result.get("duration") or ""
    try:
        parts = [int(part) for part in text.split(":")]
    except (TypeError, ValueError):
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def _artists(result: dict) -> list[str]:
    return [
        item.get("name") or ""
        for item in (result.get("artists") or [])
        if isinstance(item, dict) and item.get("name")
    ]


def validate_live(*, limit: int) -> list[LiveResult]:
    from ytmusicapi import YTMusic

    cases_by_name = {case["name"]: case for case in load_corpus()}
    cases = [cases_by_name[name] for name in LIVE_CASE_NAMES[: max(0, limit)]]
    ytm = YTMusic()
    output: list[LiveResult] = []

    for case in cases:
        title, artist, duration = case["source"]
        started = time.perf_counter()
        try:
            results = ytm.search(f"{artist} {title}", filter="songs", limit=5)
        except Exception as exc:
            output.append(LiveResult(
                case=case["name"],
                elapsed_seconds=round(time.perf_counter() - started, 3),
                candidates=0,
                outcome=f"search_error:{type(exc).__name__}",
            ))
            continue

        matches = []
        for index, result in enumerate(results or []):
            artists = _artists(result)
            match = match_from_metadata(
                # A stable synthetic locator is deliberate: ranking and the
                # validation verdict must not depend on a YouTube video ID.
                url=f"live-candidate-{index}",
                title=title,
                artist=artist,
                duration_sec=int(duration) if duration else None,
                yt_title=result.get("title") or "",
                yt_channel=artists[0] if artists else "",
                yt_duration_sec=_duration_seconds(result),
                yt_artists=artists,
            )
            if match.safe and match.confidence >= 0.65:
                matches.append((match, artists))

        elapsed = round(time.perf_counter() - started, 3)
        if not matches:
            output.append(LiveResult(
                case=case["name"], elapsed_seconds=elapsed,
                candidates=len(results or []), outcome="safe_miss",
            ))
            continue

        matches.sort(key=lambda pair: (
            -pair[0].score,
            pair[0].youtube_title.casefold(),
            pair[0].channel.casefold(),
        ))
        selected, artists = matches[0]
        output.append(LiveResult(
            case=case["name"],
            elapsed_seconds=elapsed,
            candidates=len(results or []),
            outcome="safe_match",
            selected_title=selected.youtube_title,
            selected_artist=", ".join(artists),
            selected_duration=selected.duration_sec,
            score=selected.score,
            source_versions=tuple(selected.breakdown["source_versions"]),
            selected_versions=tuple(selected.breakdown["candidate_versions"]),
            source_version_qualifiers=tuple(
                selected.breakdown["source_version_qualifiers"]
            ),
            selected_version_qualifiers=tuple(
                selected.breakdown["candidate_version_qualifiers"]
            ),
        ))
    return output


def compare_fallback(*, limit: int) -> list[FallbackTiming]:
    cases_by_name = {case["name"]: case for case in load_corpus()}
    cases = [cases_by_name[name] for name in LIVE_CASE_NAMES[: max(0, limit)]]
    output: list[FallbackTiming] = []
    for case in cases:
        title, artist, _duration = case["source"]
        query = f"ytsearch5:{artist} {title}"
        flat_elapsed = deep_elapsed = None
        flat_count = deep_count = 0
        errors: list[str] = []
        try:
            started = time.perf_counter()
            flat_count = len(_search(query, extract_flat=True, cookies_file=None))
            flat_elapsed = round(time.perf_counter() - started, 3)
        except Exception as exc:
            errors.append(f"flat:{type(exc).__name__}")
        try:
            started = time.perf_counter()
            deep_count = len(_search(query, extract_flat=False, cookies_file=None))
            deep_elapsed = round(time.perf_counter() - started, 3)
        except Exception as exc:
            errors.append(f"deep:{type(exc).__name__}")
        output.append(FallbackTiming(
            case=case["name"],
            flat_seconds=flat_elapsed,
            deep_seconds=deep_elapsed,
            flat_candidates=flat_count,
            deep_candidates=deep_count,
            error=",".join(errors),
        ))
    return output


def _median(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(statistics.median(present), 3) if present else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=len(LIVE_CASE_NAMES))
    parser.add_argument("--compare-fallback", type=int, default=0, metavar="N")
    args = parser.parse_args()

    payload: dict[str, object] = {
        "deterministic_corpus_cases": len(load_corpus()),
        "correctness_key": "title+artist+duration+recording_version metadata",
        "video_id_assertions": 0,
    }
    exit_code = 0
    if args.live:
        results = validate_live(limit=args.limit)
        safe = sum(result.outcome == "safe_match" for result in results)
        payload["live"] = {
            "results": [asdict(result) for result in results],
            "safe_matches": safe,
            "safe_misses": sum(result.outcome == "safe_miss" for result in results),
            "errors": sum(result.outcome.startswith("search_error") for result in results),
            "median_seconds": _median(result.elapsed_seconds for result in results),
        }
        # A returned selection is a failure if recording intent diverges. A
        # conservative miss is reported separately and never turned into a
        # wrong-track fallback.
        if any(
            result.outcome == "safe_match"
            and (
                result.source_versions != result.selected_versions
                or result.source_version_qualifiers
                != result.selected_version_qualifiers
            )
            for result in results
        ):
            exit_code = 1

    if args.compare_fallback:
        timings = compare_fallback(limit=args.compare_fallback)
        payload["fallback_comparison"] = {
            "results": [asdict(result) for result in timings],
            "flat_median_seconds": _median(result.flat_seconds for result in timings),
            "deep_median_seconds": _median(result.deep_seconds for result in timings),
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
