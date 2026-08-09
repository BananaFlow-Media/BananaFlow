from __future__ import annotations

import tempfile
import threading

import pytest

from core.batch_progress import JobState
from core.download_orchestrator import DownloadOrchestrator
from core.downloader import DownloadRequest, MediaType
from core.track_phases import TrackPhase


class _Engine:
    def __init__(self) -> None:
        self._cancel_event = threading.Event()

    def cancel_all(self) -> None:
        self._cancel_event.set()


class _Callbacks:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def __getattr__(self, name):
        return lambda *args: self.events.append((name, *args))


class _ClosedPool:
    def submit(self, *_args, **_kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")


def _request() -> DownloadRequest:
    return DownloadRequest(
        url="https://example.com/item",
        output_dir=tempfile.gettempdir(),
        media_type=MediaType.AUDIO,
    )


def test_submit_after_cancel_is_a_normal_cancel_not_a_batch_crash():
    engine = _Engine()
    callbacks = _Callbacks()
    orch = DownloadOrchestrator(engine=engine, callbacks=callbacks)
    key = "k"
    orch._aggregator.reset([key])
    orch._job_locks[key] = threading.Lock()
    orch._cancel_events[key] = threading.Event()
    orch._cancel_events[key].set()

    assert orch._submit_pool_task(_ClosedPool(), key, lambda: None) is None
    assert orch.job_state(key) == JobState.CANCELLED


def test_unexpected_submit_runtime_error_is_not_hidden():
    orch = DownloadOrchestrator(engine=_Engine(), callbacks=_Callbacks())
    key = "k"
    orch._cancel_events[key] = threading.Event()
    with pytest.raises(RuntimeError, match="cannot schedule"):
        orch._submit_pool_task(_ClosedPool(), key, lambda: None)


def test_pause_claim_suppresses_later_phase_and_heartbeat_updates():
    callbacks = _Callbacks()
    orch = DownloadOrchestrator(engine=_Engine(), callbacks=callbacks)
    key = "k"
    req = _request()
    orch._aggregator.reset([key])
    orch._job_locks[key] = threading.Lock()
    orch._active_requests[key] = req
    orch._enter_phase(key, TrackPhase.MATCHING)
    callbacks.events.clear()

    _state, snapshot = orch.live_request_snapshot(key)
    assert snapshot is not None
    orch._enter_phase(key, TrackPhase.STARTING)
    orch._emit_track_position(key)

    # The pause itself publishes ONE whole-batch snapshot, so the footer sees
    # the paused job immediately rather than at the end of the batch. What
    # must stay silent is this track's own repaints: every later phase and
    # heartbeat update for a paused key is suppressed.
    batch_snapshots = [e for e in callbacks.events if e[0] == "on_batch_snapshot"]
    assert len(batch_snapshots) == 1
    assert batch_snapshots[0][1].paused == 1
    assert [e for e in callbacks.events if e[0] != "on_batch_snapshot"] == []
    assert key not in orch._job_phase
    assert key in orch._phase_suppressed_keys


def test_diagnostic_scopes_do_not_mix_concurrent_batches():
    from utils.yt_dlp_opts import (
        diagnostic_scope, diagnostic_scope_metrics, note_cookie_diagnostic,
        note_po_token_provider_diagnostic, reset_po_token_provider_circuit,
    )

    reset_po_token_provider_circuit()
    barrier = threading.Barrier(2)

    def emit(scope_id: str, cookie_count: int, po_count: int) -> None:
        with diagnostic_scope(scope_id):
            barrier.wait(timeout=2)
            for _ in range(cookie_count):
                assert note_cookie_diagnostic("cookies are expired")
            for _ in range(po_count):
                note_po_token_provider_diagnostic("PO Token missing")

    a = threading.Thread(target=emit, args=("a", 2, 1))
    b = threading.Thread(target=emit, args=("b", 1, 3))
    a.start(); b.start(); a.join(timeout=3); b.join(timeout=3)
    assert not a.is_alive() and not b.is_alive()

    assert diagnostic_scope_metrics("a", clear=True) == {
        "attempts": 0, "po_diagnostics": 1, "cookie_diagnostics": 2,
    }
    assert diagnostic_scope_metrics("b", clear=True) == {
        "attempts": 0, "po_diagnostics": 3, "cookie_diagnostics": 1,
    }
