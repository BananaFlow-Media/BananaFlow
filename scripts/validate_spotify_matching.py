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
import re
import statistics
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from core.spotify_match_scorer import (
    _rank, _search, find_best_youtube_match, match_from_metadata,
)


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
    resolution_path: str = ""


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


def validate_labeled_corpus() -> dict[str, object]:
    """Score candidates while treating fixture labels as the sole oracle."""
    latencies: list[float] = []
    correct = wrong = misses = 0
    for case in load_corpus():
        title, artist, duration = case["source"]
        started = time.perf_counter()
        ranked = _rank([
            match_from_metadata(
                url=f"fixture-candidate-{index}",
                title=title,
                artist=artist,
                duration_sec=duration,
                yt_title=candidate[0],
                yt_channel=candidate[1],
                yt_duration_sec=candidate[2],
            )
            for index, candidate in enumerate(case["candidates"])
        ])
        latencies.append(time.perf_counter() - started)
        safe = [item for item in ranked if item.safe]
        expected = case["expected"]
        if not safe and expected is None:
            misses += 1
        elif not safe:
            misses += 1
        elif expected is not None and safe[0].youtube_title == expected:
            correct += 1
        else:
            wrong += 1
    return {
        "cases": correct + wrong + misses,
        "correct_selections": correct,
        "wrong_selections": wrong,
        "conservative_misses": misses,
        "p50_milliseconds": round((_median(latencies) or 0.0) * 1000, 3),
        "p95_milliseconds": round((_p95(latencies) or 0.0) * 1000, 3),
        "oracle": "fixture expected title chosen before scorer execution",
    }


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


_ORACLE_VERSIONS = {
    "cover": re.compile(r"\bcover\b|קאבר", re.I),
    "live": re.compile(r"\b(?:live|concert)\b", re.I),
    "remix": re.compile(r"\b(?:remix|mix)\b", re.I),
    "acoustic": re.compile(r"\b(?:acoustic|unplugged)\b", re.I),
    "instrumental": re.compile(r"\binstrumental\b", re.I),
    "remaster": re.compile(r"\bremaster(?:ed)?\b", re.I),
    "radio_edit": re.compile(r"\bradio\s+(?:edit|version)\b", re.I),
}


def _oracle_fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def _oracle_versions(value: str) -> frozenset[str]:
    return frozenset(name for name, pattern in _ORACLE_VERSIONS.items() if pattern.search(value or ""))


def _oracle_base_title(value: str) -> str:
    cleaned = value or ""
    # Recording/version identity is checked independently below. Parenthetical
    # artist, venue, broadcast and presentation descriptors are not part of
    # the base song title (for example the current YTM spelling of the Cedric
    # Gervais remix includes three such groups).
    cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"\b(?:official|audio|video|lyrics?|visualizer)\b", " ", cleaned, flags=re.I)
    for pattern in _ORACLE_VERSIONS.values():
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
    return _oracle_fold(cleaned)


def independent_recording_oracle(
    source_title: str, source_artist: str, source_duration: int | None,
    selected_title: str, selected_artists: list[str], selected_duration: int | None,
) -> bool:
    """Human-specified metadata oracle, independent of production scoring."""
    source_parts = [part for part in re.split(r"\s*(?:,|&|;|\bfeat\.?\b|\bft\.?\b|\s[x×]\s)\s*", source_artist, flags=re.I) if part]
    selected_folded = {_oracle_fold(part).removeprefix("the ") for part in selected_artists}
    for part in source_parts:
        wanted = _oracle_fold(part).removeprefix("the ")
        if wanted and wanted not in selected_folded:
            return False
    if _oracle_base_title(source_title) != _oracle_base_title(selected_title):
        return False
    if _oracle_versions(source_title) != _oracle_versions(selected_title):
        return False
    if source_duration and selected_duration and abs(source_duration - selected_duration) > max(8, source_duration * 0.08):
        return False
    return True


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
            observed_paths: list[str] = []
            fallback = find_best_youtube_match(
                title, artist, int(duration) if duration else None,
                path_observer=observed_paths.append,
            )
            elapsed = round(time.perf_counter() - started, 3)
            if fallback is None:
                output.append(LiveResult(
                    case=case["name"], elapsed_seconds=elapsed,
                    candidates=len(results or []), outcome="conservative_miss",
                    resolution_path=(
                        "deep_validation" if "deep_validation" in observed_paths
                        else "general_miss"
                    ),
                ))
                continue
            fallback_artists = [fallback.channel] if fallback.channel else []
            oracle_ok = independent_recording_oracle(
                title, artist, int(duration) if duration else None,
                fallback.youtube_title, fallback_artists, fallback.duration_sec,
            )
            output.append(LiveResult(
                case=case["name"], elapsed_seconds=elapsed,
                candidates=len(results or []),
                outcome="correct_selection" if oracle_ok else "wrong_selection",
                selected_title=fallback.youtube_title,
                selected_artist=fallback.channel,
                selected_duration=fallback.duration_sec,
                score=fallback.score,
                source_versions=tuple(fallback.breakdown["source_versions"]),
                selected_versions=tuple(fallback.breakdown["candidate_versions"]),
                source_version_qualifiers=tuple(fallback.breakdown["source_version_qualifiers"]),
                selected_version_qualifiers=tuple(fallback.breakdown["candidate_version_qualifiers"]),
                resolution_path=str(fallback.breakdown.get("resolution_path", "general")),
            ))
            continue

        matches.sort(key=lambda pair: (
            -pair[0].score,
            pair[0].youtube_title.casefold(),
            pair[0].channel.casefold(),
        ))
        selected, artists = matches[0]
        oracle_ok = independent_recording_oracle(
            title, artist, int(duration) if duration else None,
            selected.youtube_title, artists, selected.duration_sec,
        )
        output.append(LiveResult(
            case=case["name"],
            elapsed_seconds=elapsed,
            candidates=len(results or []),
            outcome="correct_selection" if oracle_ok else "wrong_selection",
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
            resolution_path="structured_ytm",
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


def _p95(values: Iterable[float | None]) -> float | None:
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    return round(present[min(len(present) - 1, int((len(present) - 1) * 0.95))], 3)


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
        "labeled_corpus": validate_labeled_corpus(),
    }
    exit_code = 0
    if args.live:
        results = validate_live(limit=args.limit)
        correct = sum(result.outcome == "correct_selection" for result in results)
        payload["live"] = {
            "results": [asdict(result) for result in results],
            "correct_selections": correct,
            "wrong_selections": sum(result.outcome == "wrong_selection" for result in results),
            "conservative_misses": sum(result.outcome == "conservative_miss" for result in results),
            "errors": sum(result.outcome.startswith("search_error") for result in results),
            "median_seconds": _median(result.elapsed_seconds for result in results),
            "p95_seconds": _p95(result.elapsed_seconds for result in results),
            "structured_path_percentage": round(
                100 * sum(result.resolution_path == "structured_ytm" for result in results)
                / max(1, len(results)), 1,
            ),
            "flat_path_percentage": round(
                100 * sum(result.resolution_path in {"flat", "flat_partial"} for result in results)
                / max(1, len(results)), 1,
            ),
            "deep_validation_percentage": round(
                100 * sum(result.resolution_path in {"deep", "deep_validation"} for result in results)
                / max(1, len(results)), 1,
            ),
        }
        # A returned selection is a failure if recording intent diverges. A
        # conservative miss is reported separately and never turned into a
        # wrong-track fallback.
        if any(
            result.outcome == "wrong_selection"
            for result in results
        ):
            exit_code = 1

    if args.compare_fallback:
        timings = compare_fallback(limit=args.compare_fallback)
        payload["fallback_comparison"] = {
            "results": [asdict(result) for result in timings],
            "flat_median_seconds": _median(result.flat_seconds for result in timings),
            "deep_median_seconds": _median(result.deep_seconds for result in timings),
            "flat_p95_seconds": _p95(result.flat_seconds for result in timings),
            "deep_p95_seconds": _p95(result.deep_seconds for result in timings),
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
