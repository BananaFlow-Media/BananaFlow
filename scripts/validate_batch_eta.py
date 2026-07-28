"""Deterministic and real-time whole-batch ETA validation harness.

Run directly to print JSON metrics. The schedules cover cold startup, serial
and parallel throughput, mixed prefetch/live resolution, retry/failure stalls,
and a final post-processing drain. The default is a zero-sleep fake-clock run;
``--realtime-runs`` replays the same workloads against the real monotonic
clock for controlled, directly comparable end-to-end observations.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from statistics import median

from core.batch_progress import BatchProgressAggregator


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = float(value)


@dataclass(frozen=True)
class Scenario:
    name: str
    completion_times: tuple[float, ...]
    sources: tuple[str, ...]
    peak_parallel: int = 1
    failed_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class Metrics:
    name: str
    samples: int
    first_estimate_seconds: float | None
    median_absolute_percentage_error: float | None
    interval_coverage: float | None
    largest_relative_revision: float | None


SCENARIOS = (
    Scenario(
        "serial_cold_start",
        (28, 39, 50, 61, 72, 83, 94, 105, 116, 127, 138, 149),
        ("direct",) * 12,
    ),
    Scenario(
        "parallel_three_workers",
        (20, 20, 20, 31, 31, 31, 42, 42, 42, 53, 53, 53),
        ("direct",) * 12,
        peak_parallel=3,
    ),
    Scenario(
        "prefetch_then_live_misses",
        (5, 6, 7, 24, 35, 46, 57, 68, 79, 90, 101, 112),
        ("prefetched", "prefetched", "cache") + ("live",) * 9,
    ),
    Scenario(
        "retry_failure_and_rate_limit_stall",
        (12, 24, 36, 62, 75, 88, 101, 114, 127, 140),
        ("direct",) * 10,
        failed_indices=(3,),
    ),
    Scenario(
        "postprocess_heavy_final_drain",
        (14, 28, 42, 56, 70, 84, 108, 134),
        ("direct",) * 8,
    ),
)


def evaluate(scenario: Scenario) -> Metrics:
    clock = Clock()
    keys = [f"job-{index}" for index in range(len(scenario.completion_times))]
    agg = BatchProgressAggregator(time_fn=clock, speed_smoothing=1.0)
    agg.reset(keys)
    for index, source in enumerate(scenario.sources):
        agg.mark_resolution_source(keys[index], source)
    # Establish observed concurrency without inventing a worker-count term.
    for index in range(min(scenario.peak_parallel, len(keys))):
        agg.update(keys[index], downloaded_bytes=1, total_bytes=100, speed_bps=1)

    finish = scenario.completion_times[-1]
    errors: list[float] = []
    coverage: list[bool] = []
    revisions: list[float] = []
    previous: float | None = None
    first: float | None = None

    for index, event_time in enumerate(scenario.completion_times):
        clock.set(event_time)
        agg.mark_submitted(keys[index])
        if index in scenario.failed_indices:
            agg.fail(keys[index])
        else:
            agg.complete(keys[index], final_bytes=100)
        snapshot = agg.snapshot()
        actual = max(0.0, finish - event_time)
        if snapshot.eta_seconds is None or actual <= 0:
            continue
        if first is None:
            first = event_time
        errors.append(abs(snapshot.eta_seconds - actual) / actual)
        if snapshot.eta_lower_seconds is not None:
            upper = snapshot.eta_upper_seconds
            coverage.append(
                actual >= snapshot.eta_lower_seconds
                and (upper is None or actual <= upper)
            )
        if previous is not None:
            revisions.append(abs(snapshot.eta_seconds - previous) / max(1.0, previous))
        previous = snapshot.eta_seconds

    return Metrics(
        name=scenario.name,
        samples=len(errors),
        first_estimate_seconds=first,
        median_absolute_percentage_error=median(errors) if errors else None,
        interval_coverage=(sum(coverage) / len(coverage)) if coverage else None,
        largest_relative_revision=max(revisions) if revisions else None,
    )


def run_validation() -> list[Metrics]:
    return [evaluate(scenario) for scenario in SCENARIOS]


def evaluate_realtime(scenario: Scenario, *, scale: float) -> Metrics:
    """Replay one scenario using the real monotonic clock and real waits."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    keys = [f"job-{index}" for index in range(len(scenario.completion_times))]
    agg = BatchProgressAggregator(speed_smoothing=1.0)
    agg.reset(keys)
    for index, source in enumerate(scenario.sources):
        agg.mark_resolution_source(keys[index], source)
    for index in range(min(scenario.peak_parallel, len(keys))):
        agg.update(keys[index], downloaded_bytes=1, total_bytes=100, speed_bps=1)

    started = time.monotonic()
    finish_at = started + scenario.completion_times[-1] * scale
    errors: list[float] = []
    coverage: list[bool] = []
    revisions: list[float] = []
    previous: float | None = None
    first: float | None = None

    for index, event_time in enumerate(scenario.completion_times):
        deadline = started + event_time * scale
        remaining_wait = deadline - time.monotonic()
        if remaining_wait > 0:
            time.sleep(remaining_wait)
        agg.mark_submitted(keys[index])
        if index in scenario.failed_indices:
            agg.fail(keys[index])
        else:
            agg.complete(keys[index], final_bytes=100)
        observed_at = time.monotonic()
        snapshot = agg.snapshot()
        actual = max(0.0, finish_at - observed_at)
        if snapshot.eta_seconds is None or actual <= 0:
            continue
        if first is None:
            first = observed_at - started
        errors.append(abs(snapshot.eta_seconds - actual) / actual)
        if snapshot.eta_lower_seconds is not None:
            upper = snapshot.eta_upper_seconds
            coverage.append(
                actual >= snapshot.eta_lower_seconds
                and (upper is None or actual <= upper)
            )
        if previous is not None:
            revisions.append(abs(snapshot.eta_seconds - previous) / max(0.01, previous))
        previous = snapshot.eta_seconds

    return Metrics(
        name=scenario.name,
        samples=len(errors),
        first_estimate_seconds=first,
        median_absolute_percentage_error=median(errors) if errors else None,
        interval_coverage=(sum(coverage) / len(coverage)) if coverage else None,
        largest_relative_revision=max(revisions) if revisions else None,
    )


def run_realtime_validation(*, runs: int, scale: float) -> list[list[Metrics]]:
    return [
        [evaluate_realtime(scenario, scale=scale) for scenario in SCENARIOS]
        for _ in range(max(0, runs))
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--realtime-runs", type=int, default=0,
        help="also replay every scenario this many times against the real clock",
    )
    parser.add_argument(
        "--scale", type=float, default=0.03,
        help="real-time replay scale (0.03 makes 100 simulated seconds take 3 seconds)",
    )
    args = parser.parse_args()
    payload: dict[str, object] = {
        "deterministic": [asdict(item) for item in run_validation()],
    }
    if args.realtime_runs:
        payload["realtime"] = [
            [asdict(item) for item in run]
            for run in run_realtime_validation(
                runs=args.realtime_runs, scale=args.scale,
            )
        ]
        payload["realtime_scale"] = args.scale
    print(json.dumps(payload, indent=2, sort_keys=True))
