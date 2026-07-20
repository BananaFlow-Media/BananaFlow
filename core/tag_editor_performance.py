"""Deterministic Phase 13 large-library fixtures and measurement helpers.

This module deliberately has no Qt or pytest dependency. Production models and
tests can supply callables/adapters while sharing one result vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median
import time
import tracemalloc
from typing import Callable, Iterable

from core.change_sets import FileIdentity
from core.metadata_models import AudioTrackItem, OriginalTags


@dataclass(frozen=True)
class PerformanceSample:
    name: str
    item_count: int
    samples_ms: tuple[float, ...]
    median_ms: float
    slowest_ms: float
    peak_bytes: int = 0


@dataclass(frozen=True)
class OperationCounterSnapshot:
    resets: int = 0
    inserted_rows: int = 0
    removed_rows: int = 0
    refreshed_rows: int = 0
    layout_changes: int = 0
    proxy_invalidations: int = 0


def synthetic_tracks(count: int, *, root: Path = Path("C:/phase13-fixture"),
                     folders: int = 100) -> list[AudioTrackItem]:
    """Build stable metadata-only 1k/5k/10k fixtures without filesystem IO."""
    count = max(0, int(count)); folders = max(1, int(folders))
    values: list[AudioTrackItem] = []
    for index in range(count):
        folder = Path(root) / f"album-{index % folders:04d}"
        path = folder / f"track-{index:05d}.mp3"
        values.append(AudioTrackItem(
            path=path, folder=folder, ext=".mp3",
            original=OriginalTags(
                title=f"Track {index:05d}", artist=f"Artist {index % 257:03d}",
                album=f"Album {index % folders:04d}",
                track_num=(index % 99) + 1, genre=f"Genre {index % 12:02d}"),
            format_id="mp3", metadata_editable=True,
            baseline_identity=FileIdentity(
                str(path), 4096 + index, 1_700_000_000_000_000_000 + index,
                1, index + 1000),
        ))
    return values


def measure_operation(name: str, item_count: int, operation: Callable[[], object],
                      *, samples: int = 5, warmups: int = 1,
                      measure_memory: bool = False) -> PerformanceSample:
    """Measure an operation with warm-up and generous environment neutrality."""
    for _ in range(max(0, int(warmups))):
        operation()
    timings: list[float] = []
    peak = 0
    if measure_memory:
        tracemalloc.start()
    try:
        for _ in range(max(1, int(samples))):
            started = time.perf_counter()
            operation()
            timings.append((time.perf_counter() - started) * 1000.0)
        if measure_memory:
            _current, peak = tracemalloc.get_traced_memory()
    finally:
        if measure_memory:
            tracemalloc.stop()
    return PerformanceSample(
        str(name), int(item_count), tuple(timings), median(timings),
        max(timings), int(peak))


def counter_snapshot(model=None, proxy=None) -> OperationCounterSnapshot:
    values = getattr(model, "operation_counters", None)
    return OperationCounterSnapshot(
        resets=int(getattr(values, "resets", 0)),
        inserted_rows=int(getattr(values, "inserted_rows", 0)),
        removed_rows=int(getattr(values, "removed_rows", 0)),
        refreshed_rows=int(getattr(values, "refreshed_rows", 0)),
        layout_changes=int(getattr(values, "layout_changes", 0)),
        proxy_invalidations=int(getattr(proxy, "invalidation_count", 0)),
    )


def format_samples(samples: Iterable[PerformanceSample]) -> str:
    lines = ["operation,item_count,median_ms,slowest_ms,peak_bytes"]
    for sample in samples:
        lines.append(
            f"{sample.name},{sample.item_count},{sample.median_ms:.3f},"
            f"{sample.slowest_ms:.3f},{sample.peak_bytes}")
    return "\n".join(lines) + "\n"
