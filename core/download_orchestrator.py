"""
core/download_orchestrator.py  –  Framework-agnostic batch download manager
============================================================================
Owns the job queue, thread pool, per-job cancellation, progress aggregation,
and history persistence.  Communicates exclusively via a callback protocol —
zero Qt / GUI imports.

This is the single source of truth for "run N downloads in parallel".
The UI layer (DownloadWorker QThread, or a future CLI) only needs to:
  1. Create an Orchestrator with an OrchestratorCallbacks implementation.
  2. Call run_batch() — blocking, meant to be called from a background thread.
  3. Optionally call cancel() / cancel_track() from any thread.

Thread safety
-------------
* The ThreadPoolExecutor handles scheduling.
* _progress_lock guards the shared progress dict.
* cancel events are threading.Event — safe to set from any thread.
* All callback invocations are wrapped in try/except so a crashing
  callback never kills a pool thread.

Zero GUI imports.
"""

from __future__ import annotations

import time
import random
import logging
import threading
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

from core.history_db import DownloadRecord, HistoryDB
from core.batch_outcome import BatchOutcome
from core.batch_progress import BatchProgressAggregator, BatchSnapshot
from core.downloader import (
    DownloadEngine,
    DownloadProgress,
    DownloadRequest,
    DownloadStatus,
)
from core.playlist_parser import SourcePlatform
from core.retry_policy import DEFAULT_POLICY, retry_download
from core.youtube_reliability import (
    CONSERVATIVE_DELAY_RANGE,
    CONSERVATIVE_MAX_PARALLEL_YOUTUBE,
    is_youtube_url,
)
from error_handler import classify_error, ErrorInfo

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Callback protocol (the "port" that the UI adapter implements)
# ──────────────────────────────────────────────────────────────────────────────

class OrchestratorCallbacks(Protocol):
    """
    Interface that any consumer (Qt worker, CLI, tests) must implement.
    All methods are called from background threads — the implementer is
    responsible for marshalling to the correct thread (e.g. via Qt signals).
    """

    def on_track_progress(self, key: str, fraction: float) -> None: ...
    def on_track_speed(self, key: str, speed_bps: float, eta_seconds: float) -> None: ...
    def on_track_status(self, key: str, status: str) -> None: ...
    def on_track_finished(self, key: str, output_path: str) -> None: ...
    def on_track_error(self, key: str, error: ErrorInfo) -> None: ...
    def on_overall_progress(self, fraction: float) -> None: ...
    def on_metrics(self, speed: str, eta: str) -> None: ...
    def on_batch_snapshot(self, snapshot: "BatchSnapshot") -> None: ...
    def on_status_message(self, msg: str) -> None: ...
    def on_job_count_changed(self, completed: int, total: int) -> None: ...
    def on_batch_finished(self, outcome: "BatchOutcome") -> None: ...
    def on_track_thumbnail(self, key: str, thumbnail_url: str) -> None: ...


# ──────────────────────────────────────────────────────────────────────────────
# Batch result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BatchResult:
    """Summary returned by run_batch()."""
    total:      int
    completed:  int
    failed:     int
    cancelled:  bool
    outcome:    BatchOutcome = BatchOutcome.COMPLETED


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class DownloadOrchestrator:
    """
    Pure-Python batch download manager.

    Parameters
    ----------
    engine      : Shared DownloadEngine instance.
    db          : Optional HistoryDB for post-download persistence.
    callbacks   : Implementation of OrchestratorCallbacks.
    max_workers : Concurrent download limit (1-6). Clamped internally to
                  match AppConfig.max_parallel_downloads.
    """

    def __init__(
        self,
        engine:      DownloadEngine,
        callbacks:   OrchestratorCallbacks,
        db:          Optional[HistoryDB] = None,
        max_workers: int = 3,
    ) -> None:
        self._engine      = engine
        self._cb          = callbacks
        self._db          = db
        self._max_workers = max(1, min(max_workers, 6))

        # Cancel infrastructure
        self._cancel_events: dict[str, threading.Event] = {}
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pool_lock = threading.Lock()

        # Progress accounting. The aggregator owns byte-weighted batch
        # progress, aggregate speed, and the whole-batch ETA (see
        # core.batch_progress). The simple counters below remain for the
        # BatchResult summary and job-count callback.
        self._progress_lock = threading.Lock()
        self._aggregator = BatchProgressAggregator()
        self._completed = 0
        self._failed    = 0
        self._total     = 0

        # YouTube-only conservative reliability mode: serializes YouTube
        # jobs (regardless of max_workers) and adds a cooldown between
        # them. Non-YouTube jobs never touch this gate. Only engaged when
        # a batch actually contains more than one conservative-mode
        # YouTube job — see run_batch().
        self._youtube_gate = threading.Semaphore(CONSERVATIVE_MAX_PARALLEL_YOUTUBE)
        self._youtube_serialize = False

    # ── Public API (call from any thread) ─────────────────────────────────────

    def cancel(self) -> None:
        """Cancel all in-flight and pending downloads."""
        logger.info("[Orchestrator] cancel() — stopping all jobs")
        for ev in self._cancel_events.values():
            ev.set()
        self._engine.cancel_all()
        with self._pool_lock:
            if self._pool is not None:
                try:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    self._pool.shutdown(wait=False)

    def cancel_track(self, key: str) -> None:
        """Cancel a single track by its key."""
        ev = self._cancel_events.get(key)
        if ev:
            ev.set()
            logger.debug("[Orchestrator] Cancelled track %s", key)

    # ── Main entry point (blocking — call from background thread) ─────────────

    def run_batch(
        self, 
        jobs: list[tuple[str, DownloadRequest]],
        delay_range: Optional[Tuple[float, float]] = None
    ) -> BatchResult:
        """
        Execute a batch of downloads with bounded parallelism and optional staggered start.
        """
        if not jobs:
            # Empty batch is NOT a completed download — never fake 100%.
            logger.debug("[Orchestrator] Empty batch — skipping")
            self._aggregator.reset()
            self._safe_cb("on_overall_progress", 0.0)
            self._safe_cb("on_batch_snapshot", self._aggregator.snapshot())
            self._safe_cb("on_batch_finished", BatchOutcome.COMPLETED)
            return BatchResult(
                total=0, completed=0, failed=0, cancelled=False,
                outcome=BatchOutcome.COMPLETED,
            )

        # Check for pre-cancellation
        if self._engine._cancel_event.is_set():
            logger.info("[Orchestrator] run_batch() — started in cancelled state")
            # Mark all as cancelled
            self._aggregator.reset([key for key, _ in jobs])
            for key, _ in jobs:
                self._aggregator.cancel(key)
                self._safe_cb("on_track_status", key, "cancelled")
            self._safe_cb("on_batch_finished", BatchOutcome.CANCELLED_BY_USER)
            return BatchResult(
                total=len(jobs), completed=0, failed=0, cancelled=True,
                outcome=BatchOutcome.CANCELLED_BY_USER,
            )

        self._total     = len(jobs)
        self._completed = 0
        self._failed    = 0
        stagger_delay_range = tuple(delay_range) if delay_range else None
        self._aggregator.reset(
            [key for key, _ in jobs],
            conservative_delay_range=CONSERVATIVE_DELAY_RANGE,
            stagger_delay_range=stagger_delay_range,
        )
        self._cancel_events.clear()

        # Only serialize YouTube jobs when there's more than one in this
        # batch — a lone YouTube job (e.g. single-track download, or a
        # paused-track resume) has no sibling to protect and gains nothing
        # from an artificial cooldown.
        youtube_job_count = sum(
            1 for _, req in jobs
            if getattr(req, "youtube_reliability_mode", "conservative") == "conservative"
            and is_youtube_url(req.url)
        )
        self._youtube_serialize = youtube_job_count > 1
        # We DON'T clear engine._cancel_event here anymore to respect pre-cancellation.
        # The UI/Worker should clear it when starting a FRESH download session.

        # Tell the aggregator which jobs are bound to the conservative-mode
        # serial gate so the whole-batch ETA can add the mandatory cooldown
        # overhead for the ones still queued (see BatchProgressAggregator's
        # module docstring / _conservative_overhead_locked). Matches the same
        # per-job predicate _download_one() uses to decide gate membership.
        if self._youtube_serialize:
            for key, req in jobs:
                if (
                    getattr(req, "youtube_reliability_mode", "conservative") == "conservative"
                    and is_youtube_url(req.url)
                ):
                    self._aggregator.mark_serialized(key)

        n_workers = min(self._max_workers, self._total)
        futures: dict = {}

        pool = ThreadPoolExecutor(
            max_workers=n_workers,
            thread_name_prefix="dl-pool",
        )
        with self._pool_lock:
            self._pool = pool

        try:
            for i, (key, req) in enumerate(jobs):
                if self._engine._cancel_event.is_set():
                    break
                    
                # Stagger the starts so the batch does not hit the server
                # as a burst — keeps the request rate under typical limits.
                if i > 0 and delay_range:
                    sleep_time = random.uniform(*delay_range)
                    logger.debug(f"[Orchestrator] Staggering start: sleeping {sleep_time:.2f}s")
                    sleep_start = time.time()
                    while time.time() - sleep_start < sleep_time:
                        if self._engine._cancel_event.is_set():
                            break
                        time.sleep(0.2)

                if self._engine._cancel_event.is_set():
                    break

                ev = threading.Event()
                req.cancel_event = ev
                self._cancel_events[key] = ev
                self._aggregator.register(key)
                self._aggregator.mark_submitted(key)

                future = pool.submit(self._download_one, key, req)
                futures[future] = key

            pending = set(futures)
            while pending:
                done = {future for future in pending if future.done()}
                if not done:
                    done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue

                for future in done:
                    pending.discard(future)
                    key = futures[future]
                    try:
                        future.result()
                    except CancelledError:
                        self._aggregator.cancel(key)
                        self._safe_cb("on_track_status", key, "cancelled")
                    except Exception as exc:  # noqa: BLE001
                        if self._engine._cancel_event.is_set():  # noqa: SLF001
                            self._aggregator.cancel(key)
                            self._safe_cb("on_track_status", key, "cancelled")
                            continue
                        err = classify_error(exc)
                        self._failed += 1
                        self._safe_cb("on_track_status", key, "error")
                        self._safe_cb("on_track_error", key, err)
                        logger.error(
                            "[Orchestrator] Unhandled exception for %s: %s",
                            key, exc, exc_info=True,
                        )
        finally:
            pool.shutdown(wait=False)
            with self._pool_lock:
                self._pool = None

        # ── Finalisation ──────────────────────────────────────────────────────
        was_cancelled = self._engine._cancel_event.is_set()  # noqa: SLF001
        if was_cancelled:
            for key in self._aggregator.cancel_outstanding():
                self._safe_cb("on_track_status", key, "cancelled")

        # Never force the bar to 100% on cancellation — the real, weighted
        # progress at the moment we stopped is the honest value. A normal
        # completion reaches 1.0 on its own because every job completed.
        snapshot = self._aggregator.snapshot()
        self._safe_cb("on_overall_progress", snapshot.progress)
        self._safe_cb("on_batch_snapshot", snapshot)
        self._safe_cb("on_metrics", "", "")

        # The orchestrator can only distinguish "cancelled" from "completed"
        # (it doesn't know a UI pause from a UI cancel — that intent lives in
        # the DownloadController, which overrides this outcome). It CAN tell a
        # clean completion from one with failures.
        if was_cancelled:
            outcome = BatchOutcome.CANCELLED_BY_USER
        elif self._failed > 0:
            outcome = BatchOutcome.COMPLETED_WITH_ERRORS
        else:
            outcome = BatchOutcome.COMPLETED

        # Plain-text status line (no emoji) — primarily for the CLI/log; the
        # GUI controller re-emits localized wording keyed off the outcome.
        if was_cancelled:
            self._safe_cb("on_status_message", "Stopped.")
        elif self._failed > 0:
            ok = self._total - self._failed
            self._safe_cb(
                "on_status_message",
                f"Finished: {ok} completed, {self._failed} failed.",
            )
        else:
            s = "s" if self._total != 1 else ""
            self._safe_cb(
                "on_status_message",
                f"Done — {self._total} track{s} downloaded.",
            )

        self._safe_cb("on_batch_finished", outcome)

        logger.info(
            "[Orchestrator] Batch finished: total=%d completed=%d failed=%d cancelled=%s outcome=%s",
            self._total, self._completed, self._failed, was_cancelled, outcome.value,
        )

        return BatchResult(
            total=self._total,
            completed=self._completed,
            failed=self._failed,
            cancelled=was_cancelled,
            outcome=outcome,
        )

    # ── Per-job runner (pool thread) ──────────────────────────────────────────

    def _download_one(self, key: str, req: DownloadRequest) -> None:
        cancel_ev = self._cancel_events[key]

        if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
            self._aggregator.cancel(key)
            self._safe_cb("on_track_status", key, "cancelled")
            return

        conservative_youtube = (
            self._youtube_serialize
            and getattr(req, "youtube_reliability_mode", "conservative") == "conservative"
            and is_youtube_url(req.url)
        )
        if conservative_youtube:
            # This log line only fires when the gate is actually engaged
            # (self._youtube_serialize — batch has >1 conservative YouTube
            # job). A lone YouTube job never reaches here, so it never
            # produces a misleading "serializing"/"delay" log for a delay
            # that isn't applied.
            logger.info(
                "[yt-dlp][youtube_conservative] parallel=%d cooldown=%.0f-%.0fs "
                "— serializing YouTube job: %s",
                CONSERVATIVE_MAX_PARALLEL_YOUTUBE,
                CONSERVATIVE_DELAY_RANGE[0], CONSERVATIVE_DELAY_RANGE[1], key,
            )
            self._youtube_gate.acquire()
            if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
                self._youtube_gate.release()
                self._aggregator.cancel(key)
                self._safe_cb("on_track_status", key, "cancelled")
                return

        try:
            self._download_one_locked(key, req, cancel_ev)
        finally:
            if conservative_youtube:
                self._youtube_cooldown(cancel_ev, key)
                self._youtube_gate.release()

    def _youtube_cooldown(self, cancel_ev: threading.Event, key: str) -> None:
        """Sleep the conservative-mode cooldown before the next YouTube job
        may start, staying responsive to cancellation."""
        delay = random.uniform(*CONSERVATIVE_DELAY_RANGE)
        logger.debug(
            "[yt-dlp][youtube_conservative] cooldown %.1fs before next YouTube job (after %s)",
            delay, key,
        )
        slept = 0.0
        while slept < delay:
            if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
                return
            chunk = min(0.2, delay - slept)
            time.sleep(chunk)
            slept += chunk

    def _download_one_locked(self, key: str, req: DownloadRequest, cancel_ev: threading.Event) -> None:
        """The actual per-job download logic, run either directly (non-YouTube
        or fast mode) or while holding ``self._youtube_gate`` (conservative
        YouTube jobs — see _download_one)."""
        # Lazy URL resolution (Spotify two-stage import): produce the real
        # target URL now, the instant before downloading, so a large catalog's
        # YouTube matching is pipelined with downloading instead of blocking it
        # up front. The track stays visually "queued" during the (usually
        # cached, else 1-2 network calls) match — no matching/YouTube wording
        # is ever surfaced. Cancellation is honoured before and after.
        if req.url_resolver is not None:
            resolver = req.url_resolver
            req.url_resolver = None  # resolve at most once
            if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
                self._aggregator.cancel(key)
                self._safe_cb("on_track_status", key, "cancelled")
                return
            try:
                resolved = resolver(cancel_ev)
            except Exception as exc:  # noqa: BLE001 - a bad match must not sink the job
                logger.debug("[Orchestrator] URL resolver failed for %s: %s", key, exc)
                resolved = ""
            if resolved:
                req.url = resolved
            if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
                self._aggregator.cancel(key)
                self._safe_cb("on_track_status", key, "cancelled")
                return

        self._safe_cb("on_track_status", key, "downloading")
        logger.debug("[Orchestrator] Starting %s", key)

        update_counter = [0]

        def on_progress(p: DownloadProgress) -> None:
            if p.thumbnail_url and not getattr(req, "_thumb_sent", False):
                # Prefer the original thumbnail (e.g. Spotify) if we had one
                thumb_to_send = req.thumbnail_url if req.thumbnail_url else p.thumbnail_url
                self._safe_cb("on_track_thumbnail", key, thumb_to_send)
                req._thumb_sent = True

            # Feed the aggregator every tick (cheap) so byte totals and the
            # smoothed speed stay current even between throttled UI emits.
            self._aggregator.update(
                key,
                downloaded_bytes=p.downloaded_bytes or None,
                total_bytes=p.total_bytes,
                total_bytes_estimate=p.total_bytes_estimate,
                fraction=p.fraction,
                speed_bps=p.speed_bps or 0.0,
                eta_seconds=p.eta_seconds,
            )
            self._safe_cb("on_track_progress", key, p.fraction)
            self._safe_cb("on_track_speed", key, p.speed_bps or 0.0, p.eta_seconds or 0.0)

            update_counter[0] += 1
            if update_counter[0] % 10 != 0:
                return
            snapshot = self._aggregator.snapshot()
            self._safe_cb("on_overall_progress", snapshot.progress)
            self._safe_cb("on_batch_snapshot", snapshot)

        def on_finished(p: DownloadProgress) -> None:
            with self._progress_lock:
                self._completed += 1
            self._aggregator.complete(
                key,
                final_bytes=(
                    p.total_bytes
                    or p.downloaded_bytes
                    or p.total_bytes_estimate
                    or None
                ),
            )
            snapshot = self._aggregator.snapshot()
            self._safe_cb("on_track_status", key, "done")
            self._safe_cb("on_track_progress", key, 1.0)
            self._safe_cb("on_track_finished", key, p.output_path or "")
            self._safe_cb("on_overall_progress", snapshot.progress)
            self._safe_cb("on_batch_snapshot", snapshot)
            self._safe_cb("on_job_count_changed", self._completed, self._total)
            if p.warning_message:
                # Non-fatal post-processing note; plain text, no emoji.
                self._safe_cb("on_status_message", p.warning_message)
            logger.info("[Orchestrator] Track done: %s -> %s", key, p.output_path)
            self._persist_record(req, p)

        def on_error(p: DownloadProgress) -> None:
            with self._progress_lock:
                self._failed += 1
            self._aggregator.fail(key)
            err = classify_error(
                Exception(p.error_message or "Unknown download error"),
                cookies_file=req.cookies_file or "",
                cookies_browser=req.cookies_browser or "",
            )
            self._safe_cb("on_track_status", key, "error")
            self._safe_cb("on_track_error", key, err)
            self._safe_cb("on_batch_snapshot", self._aggregator.snapshot())
            self._safe_cb("on_job_count_changed", self._completed + self._failed, self._total) # treat failed as 'done' for progress count
            logger.warning("[Orchestrator] Track error: %s — %s", key, p.error_message)

        req.on_progress = on_progress
        req.on_finished = on_finished

        # ── Retry wrapper ─────────────────────────────────────────────────────
        # engine.download() signals failure via req.on_error callback (not
        # by raising).  We intercept it to raise a catchable exception so
        # retry_download() can apply retriable-error detection and backoff.
        _err: list[str] = []

        def _capture_error(p: DownloadProgress) -> None:
            _err.append(p.error_message or "Unknown download error")

        def _attempt() -> None:
            _err.clear()
            req.on_error = _capture_error
            self._engine.download(req)
            if _err:
                raise RuntimeError(_err[0])

        final_error = retry_download(_attempt, cancel_event=cancel_ev, job_key=key)

        if final_error and final_error != "Cancelled":
            on_error(DownloadProgress(
                status=DownloadStatus.ERROR,
                url=req.url,
                error_message=final_error,
            ))

    # ── History persistence ───────────────────────────────────────────────────

    def _persist_record(self, req: DownloadRequest, prog: DownloadProgress) -> None:
        if self._db is None:
            return
        try:
            # Derive history platform from the request. HistoryDB recognises
            # "youtube" | "ytmusic" | "spotify" | "unknown" (see history_db.py).
            # SourcePlatform.value already produces the right string for each
            # known platform; GENERIC and missing platform fall through to
            # "unknown" so per-platform stats stay honest.
            if isinstance(req.platform, SourcePlatform) and req.platform in (
                SourcePlatform.YOUTUBE,
                SourcePlatform.YOUTUBE_MUSIC,
                SourcePlatform.SPOTIFY,
            ):
                platform_str = req.platform.value
            else:
                platform_str = "unknown"

            record = DownloadRecord(
                title=prog.title or req.forced_title or "",
                artist=req.forced_artist or "",
                url=req.url,
                output_path=prog.output_path or "",
                media_type=req.media_type.value,
                file_size_mb=None,
                duration_sec=req.forced_duration,
                thumbnail_url="",
                platform=platform_str,
            )
            self._db.insert(record)
        except Exception as exc:  # noqa: BLE001
            logger.error("[Orchestrator] History insert failed: %s", exc)

    # ── Safe callback dispatch ────────────────────────────────────────────────

    def _safe_cb(self, method: str, *args) -> None:
        """Call a callback method, swallowing any exception it raises."""
        fn = getattr(self._cb, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            logger.warning("[Orchestrator] Callback %s raised unexpectedly", method, exc_info=True)
