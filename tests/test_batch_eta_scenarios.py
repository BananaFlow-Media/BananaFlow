from __future__ import annotations

from core.batch_progress import BatchProgressAggregator
from scripts.validate_batch_eta import Clock, run_validation


def test_controlled_eta_workloads_have_useful_bounded_behavior():
    results = run_validation()
    assert {item.name for item in results} == {
        "serial_cold_start",
        "parallel_three_workers",
        "prefetch_then_live_misses",
        "retry_failure_and_rate_limit_stall",
        "postprocess_heavy_final_drain",
    }
    for result in results:
        assert result.samples >= 2, result
        assert result.median_absolute_percentage_error is not None
        assert result.median_absolute_percentage_error <= 0.45, result
        assert result.interval_coverage is not None
        assert result.interval_coverage >= 0.60, result
        assert result.largest_relative_revision is not None
        assert result.largest_relative_revision <= 0.70, result


def test_mixed_prefetch_burst_does_not_set_rate_for_live_remainder():
    clock = Clock()
    agg = BatchProgressAggregator(time_fn=clock, speed_smoothing=1.0)
    keys = [f"k{index}" for index in range(10)]
    agg.reset(keys)
    for index, key in enumerate(keys):
        agg.mark_resolution_source(key, "prefetched" if index < 3 else "live")
    for index, at in enumerate((2.0, 3.0, 4.0)):
        clock.set(at)
        agg.complete(keys[index])
    assert agg.snapshot().eta_seconds is None

    for index, at in zip(range(3, 6), (20.0, 30.0, 40.0)):
        clock.set(at)
        agg.complete(keys[index])
    snapshot = agg.snapshot()
    assert snapshot.eta_source == "live"
    assert snapshot.eta_seconds is not None
    assert snapshot.eta_seconds > 20.0


def test_source_selection_has_hysteresis_at_near_tie():
    clock = Clock()
    agg = BatchProgressAggregator(time_fn=clock)
    agg.reset(["l1", "l2", "c1", "c2", "c3"])
    for key in ("l1", "l2"):
        agg.mark_resolution_source(key, "live")
    for key in ("c1", "c2", "c3"):
        agg.mark_resolution_source(key, "cache")
    assert agg._eta_source_locked(list(agg._jobs.values())) == "cache"
    agg._last_eta_source = "live"
    assert agg._eta_source_locked(list(agg._jobs.values())) == "live"
