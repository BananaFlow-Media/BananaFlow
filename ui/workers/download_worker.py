"""
ui/workers/download_worker.py  –  Qt adapter for DownloadOrchestrator
======================================================================
This is now a thin QThread shell.  All download logic, concurrency,
progress aggregation, and history persistence live in
core.download_orchestrator.DownloadOrchestrator (pure Python, zero Qt).

DownloadWorker's only job is:
  1. Implement OrchestratorCallbacks by forwarding each call to a Qt Signal.
  2. Call orchestrator.run_batch() inside QThread.run().
  3. Expose cancel() / cancel_track() / shutdown() for the UI.

Signal summary  (unchanged from v3)
------------------------------------
track_progress(str, float)    Per-track progress fraction.
track_status(str, str)        Per-track status string.
track_finished(str, str)      (key, output_path) on success.
overall_progress(float)       Batch-level 0.0–1.0.
metrics(str, str)             (speed_str, eta_str).
status_msg(str)               Human-readable status line.
job_error(str, object)        (key, ErrorInfo) on failure.
all_finished()                Entire batch complete.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QThread, Signal

from config import AppConfig
from core.download_orchestrator import (
    BatchResult,
    DownloadOrchestrator,
    OrchestratorCallbacks,
)
from core.history_db import HistoryDB
from core.downloader import DownloadEngine, DownloadRequest
from error_handler import ErrorInfo

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Signal-based callback adapter
# ──────────────────────────────────────────────────────────────────────────────

class _SignalAdapter:
    """
    Bridges OrchestratorCallbacks → Qt Signals.

    Each method simply emits the corresponding signal.  Qt's cross-thread
    signal mechanism automatically queues them to the main thread.
    """

    def __init__(self, worker: "DownloadWorker") -> None:
        self._w = worker

    def on_track_progress(self, key: str, fraction: float) -> None:
        self._w.track_progress.emit(key, fraction)

    def on_track_first_byte(self, key: str) -> None:
        self._w.track_first_byte.emit(key)

    def on_track_speed(self, key: str, speed_bps: float, eta_seconds: float) -> None:
        self._w.track_speed.emit(key, speed_bps, eta_seconds)

    def on_track_phase(self, key: str, phase: str, remaining_seconds) -> None:
        self._w.track_phase.emit(key, phase, remaining_seconds)

    def on_track_status(self, key: str, status: str) -> None:
        self._w.track_status.emit(key, status)

    def on_track_finished(self, key: str, output_path: str) -> None:
        self._w.track_finished.emit(key, output_path)

    def on_track_preexisting(self, key: str, output_path: str) -> None:
        self._w.track_preexisting.emit(key, output_path)

    def on_track_error(self, key: str, error: ErrorInfo) -> None:
        self._w.job_error.emit(key, error)

    def on_overall_progress(self, fraction: float) -> None:
        self._w.overall_progress.emit(fraction)

    def on_metrics(self, speed: str, eta: str) -> None:
        self._w.metrics.emit(speed, eta)

    def on_batch_snapshot(self, snapshot) -> None:
        self._w.batch_snapshot.emit(snapshot)

    def on_status_message(self, msg: str) -> None:
        self._w.status_msg.emit(msg)

    def on_job_count_changed(self, completed: int, total: int) -> None:
        self._w.job_count_changed.emit(completed, total)

    def on_batch_finished(self, outcome=None) -> None:
        self._w.all_finished.emit(outcome)

    def on_track_thumbnail(self, key: str, thumbnail_url: str) -> None:
        self._w.track_thumbnail.emit(key, thumbnail_url)


# ──────────────────────────────────────────────────────────────────────────────
# DownloadWorker (QThread shell)
# ──────────────────────────────────────────────────────────────────────────────

class DownloadWorker(QThread):
    """
    Thin Qt wrapper around DownloadOrchestrator.

    Parameters
    ----------
    jobs        : List of (key, DownloadRequest) tuples.
    engine      : Shared DownloadEngine.
    db          : Optional HistoryDB.
    max_workers : Concurrent download limit (1-6). Matches the
                  AppConfig.max_parallel_downloads clamp.
    parent      : Optional Qt parent.
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    track_progress   = Signal(str, float)
    track_first_byte = Signal(str)
    track_speed      = Signal(str, float, float)
    track_status     = Signal(str, str)
    track_phase      = Signal(str, str, object)   # key, phase, secs|None
    track_finished   = Signal(str, str)
    track_preexisting = Signal(str, str)   # (key, existing_path) — duplicate-skip, no download ran
    overall_progress = Signal(float)
    metrics          = Signal(str, str)
    batch_snapshot   = Signal(object)          # core.batch_progress.BatchSnapshot
    status_msg       = Signal(str)
    job_error        = Signal(str, object)
    job_count_changed = Signal(int, int)
    all_finished     = Signal(object)          # core.batch_outcome.BatchOutcome | None
    track_thumbnail  = Signal(str, str)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def __init__(
        self,
        jobs:        list[tuple[str, DownloadRequest]],
        engine:      DownloadEngine,
        config:      AppConfig,
        db:          Optional[HistoryDB] = None,
        max_workers: int = 3,
        preexisting: Optional[list[tuple[str, str]]] = None,
        batch_id:    Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._jobs   = jobs
        self._preexisting = preexisting or []
        self._cfg    = config
        # Identity the owner will use to recognise this batch's snapshots. The
        # owner mints it before starting the thread, so it can never be left
        # inferring which batch it is showing from whichever signal arrives
        # first. None for workers nobody is filtering (single-track resume).
        self._batch_id = batch_id
        self._orch   = DownloadOrchestrator(
            engine=engine,
            callbacks=_SignalAdapter(self),
            db=db,
            max_workers=max_workers,
        )
        # Set the instant run() actually begins — see wait_until_running.
        self._run_entered = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def wait_until_running(self, timeout_ms: int = 5000) -> bool:
        """Block until this worker's run() has actually begun, or the timeout
        expires. Returns whether it started.

        QThread.start() returns as soon as the thread has been *scheduled*;
        run() may not have executed a single line yet. That gap matters to
        the caller that owns this worker's persisted state: clearing "these
        jobs are paused" on the strength of start() alone leaves a window
        where a crash (or a kill) in between loses the jobs entirely — the
        worker never took ownership, and the next startup no longer has a
        record protecting their workspaces from the stale sweep. Waiting for
        this event means the on-disk record is only dropped once something
        is genuinely running that can re-create it.
        """
        return self._run_entered.wait(timeout=timeout_ms / 1000.0)

    def cancel(self) -> None:
        """Cancel all in-flight downloads."""
        self._orch.cancel()

    def cancel_track(self, card_key: str) -> None:
        """Cancel a single track by key."""
        self._orch.cancel_track(card_key)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """
        Graceful shutdown for application quit.
        Cancels everything, then waits for the QThread to finish.
        """
        logger.info("[DownloadWorker] shutdown(timeout=%dms)", timeout_ms)
        self.cancel()
        if self.isRunning():
            finished = self.wait(timeout_ms)
            if not finished:
                logger.warning(
                    "[DownloadWorker] Thread did not finish within %dms", timeout_ms,
                )

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Blocking call on the QThread — delegates entirely to orchestrator.

        The guard is not defensive padding.  ``all_finished`` is what
        returns the UI from its "downloading" state; if ``run_batch``
        raised, that signal was never emitted, the download button stayed
        disabled, and the traceback went to ``sys.stderr`` — which does
        not exist in a windowed build.  The visible result was a click
        that did nothing, permanently and with no error message.
        """
        # Signalled first thing, before any work: this worker has now
        # genuinely taken ownership of its jobs, which is what the caller
        # waits for before dropping their persisted paused record (see
        # wait_until_running). Set even if run_batch dies immediately —
        # ownership has still transferred, and the failure path below
        # releases the UI.
        self._run_entered.set()
        try:
            delay_range = self._cfg.download_delay_range
            self._orch.run_batch(
                self._jobs, delay_range=delay_range, preexisting=self._preexisting,
                batch_id=self._batch_id,
            )
        except Exception as exc:            # noqa: BLE001 - must not escape a QThread
            logger.exception("[DownloadWorker] Batch failed with an unhandled error")
            from utils.security import redact_text
            self.status_msg.emit(redact_text(exc))
            # Release the UI. A None outcome is the established "batch did
            # not produce a result" value for this signal.
            self.all_finished.emit(None)
