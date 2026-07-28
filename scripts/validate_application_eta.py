"""Application-level ETA trace validation through DownloadOrchestrator.

Unlike ``validate_batch_eta.py`` this harness executes the real orchestrator,
worker pools, lazy resolvers, progress callbacks, failure transitions, file I/O
and batch heartbeat.  The transport is instrumented and deterministic so runs
are comparable.  It is therefore an application integration trace, not an
external-network claim.  A separate live-download gate is intentionally left
to release validation where network access and media policy are controlled.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
from core.download_orchestrator import DownloadOrchestrator


@dataclass(frozen=True)
class TraceScenario:
    name: str
    split: str
    durations: tuple[float, ...]
    sizes: tuple[int, ...]
    workers: int
    resolve_delays: tuple[float, ...] = ()
    sources: tuple[str, ...] = ()
    failures: tuple[int, ...] = ()
    postprocess_delay: float = 0.0
    reliability_mode: str = "fast"


# The held-out set is frozen separately from the development scenarios. New
# estimator tuning must not change these schedules or their thresholds.
SCENARIOS = (
    TraceScenario("train_small_current_speed", "development", (0.18, 0.24, 0.15), (12_000, 24_000, 9_000), 2),
    TraceScenario("train_fast_parallel", "development", (0.18,) * 9, (18_000,) * 9, 3),
    TraceScenario("train_variable_sizes", "development", (0.10, 0.16, 0.23, 0.12, 0.27, 0.18, 0.14, 0.25), (8_000, 15_000, 31_000, 10_000, 40_000, 22_000, 12_000, 35_000), 2),
    TraceScenario("heldout_serial_cold", "heldout", (0.22,) * 8, (22_000,) * 8, 1, resolve_delays=(0.12,) + (0.02,) * 7, sources=("live",) * 8),
    TraceScenario("heldout_prefetch_then_live", "heldout", (0.08, 0.08, 0.08, 0.24, 0.24, 0.24, 0.24, 0.24, 0.24, 0.24), (9_000,) * 10, 2, resolve_delays=(0.0, 0.0, 0.0) + (0.12,) * 7, sources=("prefetched",) * 3 + ("live",) * 7),
    TraceScenario("heldout_retry_failure", "heldout", (0.16, 0.16, 0.38, 0.16, 0.16, 0.16, 0.16, 0.16), (16_000,) * 8, 2, failures=(2,)),
    TraceScenario("heldout_postprocess_drain", "heldout", (0.13,) * 8, (13_000,) * 8, 2, postprocess_delay=0.16),
    TraceScenario("heldout_warm_cache", "heldout", (0.09,) * 10, (10_000,) * 10, 3, resolve_delays=(0.01,) * 10, sources=("cache",) * 10),
    TraceScenario("heldout_large_fast", "heldout", (0.11, 0.14, 0.17, 0.12) * 4, (10_000, 16_000, 22_000, 13_000) * 4, 3),
    TraceScenario("heldout_conservative_gate", "heldout", (0.10,) * 8, (12_000,) * 8, 3, reliability_mode="conservative"),
)


@dataclass(frozen=True)
class TraceMetrics:
    name: str
    split: str
    elapsed_seconds: float
    completed: int
    failed: int
    eta_samples: int
    time_to_first_eta_seconds: float | None
    median_absolute_percentage_error: float | None
    p90_absolute_percentage_error: float | None
    interval_coverage: float | None
    largest_revision_seconds: float | None
    longest_frozen_display_seconds: float | None
    final_drain_absolute_error_seconds: float | None


class _Resolver:
    def __init__(self, url: str, delay: float, source: str) -> None:
        self.url = url
        self.delay = delay
        self.resolve_source = source

    def __call__(self, cancel: threading.Event) -> str:
        if cancel.wait(self.delay):
            return ""
        return self.url


class _TraceEngine:
    def __init__(self, scenario: TraceScenario, directory: Path) -> None:
        self._cancel_event = threading.Event()
        self.scenario = scenario
        self.directory = directory

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, request: DownloadRequest) -> None:
        index = int(request.url.rsplit("/", 1)[-1])
        duration = self.scenario.durations[index]
        total = self.scenario.sizes[index]
        steps = 5
        started = time.monotonic()
        for step in range(1, steps + 1):
            time.sleep(duration / steps)
            done = total * step // steps
            if request.on_progress:
                request.on_progress(DownloadProgress(
                    status=DownloadStatus.DOWNLOADING,
                    url=request.url,
                    downloaded_bytes=done,
                    total_bytes=total,
                    speed_bps=done / max(0.001, time.monotonic() - started),
                    fraction=step / steps,
                ))
        if self.scenario.postprocess_delay:
            if request.on_progress:
                request.on_progress(DownloadProgress(status=DownloadStatus.PROCESSING, url=request.url, fraction=1.0))
            time.sleep(self.scenario.postprocess_delay)
        if index in self.scenario.failures:
            if request.on_error:
                request.on_error(DownloadProgress(status=DownloadStatus.ERROR, url=request.url, error_message="instrumented retry exhausted"))
            return
        output = self.directory / f"trace-{index}.bin"
        output.write_bytes(b"x" * total)
        if request.on_finished:
            request.on_finished(DownloadProgress(status=DownloadStatus.FINISHED, url=request.url, output_path=str(output), downloaded_bytes=total, total_bytes=total, fraction=1.0))


class _Callbacks:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.samples: list[tuple[float, object]] = []

    def on_batch_snapshot(self, snapshot) -> None:
        self.samples.append((time.monotonic() - self.started, snapshot))

    def __getattr__(self, name):
        if name.startswith("on_"):
            return lambda *_args, **_kwargs: None
        raise AttributeError(name)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def run_scenario(scenario: TraceScenario) -> TraceMetrics:
    with tempfile.TemporaryDirectory(prefix="bananaflow-eta-trace-") as raw:
        directory = Path(raw)
        callbacks = _Callbacks()
        engine = _TraceEngine(scenario, directory)
        jobs = []
        for index in range(len(scenario.durations)):
            real_url = (
                f"https://youtube.com/watch/{index}"
                if scenario.reliability_mode == "conservative"
                else f"https://trace.invalid/{index}"
            )
            request = DownloadRequest(url=real_url, output_dir=str(directory), media_type=MediaType.AUDIO, forced_title=f"trace-{index}", youtube_reliability_mode=scenario.reliability_mode)
            if scenario.resolve_delays:
                request.url = f"spotify:trace:{index}"
                request.url_resolver = _Resolver(real_url, scenario.resolve_delays[index], scenario.sources[index])
            jobs.append((f"trace-{index}", request))
        from core import download_orchestrator as orchestrator_module
        old_cooldown = orchestrator_module.CONSERVATIVE_DELAY_RANGE
        if scenario.reliability_mode == "conservative":
            # Exercise the real serialization/cooldown branch without turning
            # a release validation into a minute-long politeness sleep.
            orchestrator_module.CONSERVATIVE_DELAY_RANGE = (0.03, 0.04)
        try:
            result = DownloadOrchestrator(engine, callbacks, max_workers=scenario.workers).run_batch(jobs, delay_range=(0.0, 0.0), batch_id=scenario.name)
        finally:
            orchestrator_module.CONSERVATIVE_DELAY_RANGE = old_cooldown
        elapsed = time.monotonic() - callbacks.started

    usable = [(at, snap) for at, snap in callbacks.samples if snap.eta_seconds is not None and elapsed - at > 0.01]
    errors = [abs(snap.eta_seconds - (elapsed - at)) / (elapsed - at) for at, snap in usable]
    covered = [snap.eta_lower_seconds <= elapsed - at <= snap.eta_upper_seconds for at, snap in usable if snap.eta_lower_seconds is not None and snap.eta_upper_seconds is not None]
    revisions = [abs(usable[i][1].eta_seconds - usable[i - 1][1].eta_seconds) for i in range(1, len(usable))]
    frozen = [usable[i][0] - usable[i - 1][0] for i in range(1, len(usable)) if round(usable[i][1].eta_seconds, 1) == round(usable[i - 1][1].eta_seconds, 1)]
    final_error = abs(usable[-1][1].eta_seconds - (elapsed - usable[-1][0])) if usable else None
    return TraceMetrics(
        scenario.name, scenario.split, round(elapsed, 3), result.completed, result.failed, len(usable),
        round(usable[0][0], 3) if usable else None,
        median(errors) if errors else None,
        _percentile(errors, 0.90),
        sum(covered) / len(covered) if covered else None,
        max(revisions) if revisions else None,
        max(frozen) if frozen else 0.0 if usable else None,
        final_error,
    )


def run_validation() -> list[TraceMetrics]:
    return [run_scenario(scenario) for scenario in SCENARIOS]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps({"validation_kind": "real_orchestrator_controlled_transport", "external_downloads": False, "results": [asdict(item) for item in run_validation()]}, indent=2, sort_keys=True))
