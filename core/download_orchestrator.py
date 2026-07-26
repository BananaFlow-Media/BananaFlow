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
import dataclasses
import hashlib
import logging
import re
import shutil
import threading
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

from core.history_db import DownloadRecord, HistoryDB
from core.batch_outcome import BatchOutcome
from core.batch_progress import BatchProgressAggregator, BatchSnapshot, JobState, is_terminal_state
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
from utils.paths import make_batch_workspace

logger = logging.getLogger(__name__)


_SUBDIR_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Directory names that belong to the BananaFlow batch-workspace structure.
# Empty-parent cleanup peels back ONLY through these, so an rmdir sweep can
# never escape into (and delete) the user's own output directory, which
# always has an arbitrary, non-matching name. See _is_workspace_owned.
_WORKSPACE_CONTAINER_NAMES = frozenset({".bananaflow_tmp", "download_workspaces"})


def _safe_subdir_name(key: str) -> str:
    """Filesystem-safe, collision-safe per-job workspace subdirectory name
    derived from a job key.

    Job keys are normally ``str(id(card))`` (digits), but this sanitises any
    caller/test that uses a freer-form key. Two requirements a naive
    "replace unsafe chars" pass does NOT meet on its own:
      * it must never produce ``.`` or ``..`` (which would make the "job
        subdir" alias the batch container or its parent, defeating per-job
        isolation and making cleanup delete the wrong thing), and
      * two different keys must never sanitise to the SAME name (e.g. "a*b"
        and "a/b" both naively become "a_b").
    A short hash of the ORIGINAL key is always appended, so the result is
    both structurally distinct from "." / ".." and collision-safe on the
    full key content, not just its mangled label.
    """
    label = _SUBDIR_UNSAFE.sub("_", key) or "job"
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{label}-{digest}"


def _is_workspace_owned(path: Path) -> bool:
    """Whether ``path`` is a BananaFlow-owned workspace directory that empty-
    parent cleanup is allowed to rmdir. True only for a ``batch-*`` dir or a
    known container root — never for the user's output directory."""
    name = path.name
    return name.startswith("batch-") or name in _WORKSPACE_CONTAINER_NAMES


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
    def on_track_preexisting(self, key: str, output_path: str) -> None: ...
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

        # Per-job lock + live-request registry backing live_request_snapshot()
        # / job_state(): lets an outside caller (Global Pause, on the UI
        # thread) read a job's terminal state and take a resume-ready copy
        # of its live DownloadRequest atomically with respect to this
        # orchestrator's own critical sections — the lazy-URL-resolver
        # commit and the terminal-state transition — instead of racing them
        # via the UI card's (delayed) status label. See both methods below.
        self._job_locks: dict[str, threading.Lock] = {}
        self._active_requests: dict[str, DownloadRequest] = {}
        # Keys whose Spotify two-stage url_resolver has been consumed but
        # whose resolved URL has not yet been committed to req.url. A
        # snapshot of the LIVE request while a key is in here would have
        # neither a resolver nor a real URL, so the pre-resolve copy taken
        # at the same instant is handed out instead — see _resolve_lazy_url.
        self._resolving_keys: set[str] = set()
        self._pending_resolver_requests: dict[str, DownloadRequest] = {}
        # First-writer-wins record of how each job ended up being disposed
        # of: "paused" (an outside Global Pause captured it for a later
        # resume) or "terminal" (it completed, failed or was cancelled).
        # Every transition into either goes through _claim_job_outcome under
        # the job's own lock, so the two can never both happen for one job.
        # See _claim_job_outcome and live_request_snapshot.
        self._job_outcomes: dict[str, str] = {}

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

        # Per-phase timing accounting (diagnostics only — never affects flow).
        # Split so we can see where a batch spends its wall-clock time and
        # decide later whether the conservative gate/cooldown policy itself is
        # the bottleneck. name -> [total_seconds, count]. Guarded by its lock;
        # reset per batch in run_batch.
        self._phase_lock = threading.Lock()
        self._phase_times: dict[str, list[float]] = {}
        # Gate-starvation accounting: monotonic time the conservative gate was
        # last released, so the next acquire can measure how long the download
        # pipeline sat idle waiting for a match to become ready. Reset per batch.
        self._gate_lock = threading.Lock()
        self._gate_last_release: Optional[float] = None

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

    def job_state(self, key: str) -> Optional[JobState]:
        """Authoritative terminal/non-terminal state for a job — see
        BatchProgressAggregator.job_state for why this must be used instead
        of a UI card's status label when the answer needs to be race-free."""
        return self._aggregator.job_state(key)

    # ── Per-job outcome claim (the pause / completion boundary) ───────────────

    def _claim_job_outcome(self, key: str, outcome: str) -> bool:
        """First-writer-wins claim of how job ``key`` is disposed of.

        ``outcome`` is ``"paused"`` (an outside Global Pause captured it) or
        ``"terminal"`` (it completed, failed, or was cancelled). Serialized
        on the job's OWN lock — the same one live_request_snapshot holds —
        so a pause and a completing download can never both succeed for the
        same job: whichever reaches the lock first wins and the other backs
        off. Returns True when the caller owns the outcome (including when
        it already owned it — the claim is idempotent per outcome), False
        when the other side got there first.

        Without this, "pause" and "complete" were two independent, unlocked
        code paths: the aggregator's terminal transition happened on a pool
        thread with no lock at all, so Global Pause could read a job as
        still-live, snapshot it for resume, and have that same job publish
        and report success a microsecond later — the batch then both
        finished the track and offered to resume it.

        A key this orchestrator never registered (a stale key, or a
        test/CLI harness that never went through run_batch) has nothing to
        serialize against and is always granted.
        """
        lock = self._job_locks.get(key)
        if lock is None:
            return True
        with lock:
            return self._claim_job_outcome_locked(key, outcome)

    def _claim_job_outcome_locked(self, key: str, outcome: str) -> bool:
        """_claim_job_outcome's body, for callers already holding the lock."""
        current = self._job_outcomes.get(key)
        if current is None:
            self._job_outcomes[key] = outcome
            return True
        return current == outcome

    def _mark_cancelled(self, key: str) -> None:
        """Record a job as cancelled and tell the UI — unless a Global Pause
        already claimed it, in which case the job is *paused*, not cancelled,
        and must keep both its resumable state and its "paused" card label
        (cancellation and pause set the same events; only the claim tells
        them apart)."""
        if not self._claim_job_outcome(key, "terminal"):
            return
        self._aggregator.cancel(key)
        self._safe_cb("on_track_status", key, "cancelled")

    def live_request_snapshot(
        self, key: str,
    ) -> Tuple[Optional[JobState], Optional[DownloadRequest]]:
        """Atomically read a job's state and a defensive copy of its live
        DownloadRequest, serialized against this orchestrator's own
        resolver-commit and cancellation critical sections via the job's
        per-job lock (see __init__ and _resolve_lazy_url).

        This is the only race-free way for an outside caller (Global Pause,
        on the UI thread) to decide whether a job is still safe to
        pause-and-snapshot for a later resume. Reading the aggregator state
        and reconstructing the request as two separate, unlocked steps (the
        old approach, keyed off the UI card's status label) leaves two
        windows open:
          * the job reaches a terminal state between the two reads, so a
            job that already succeeded gets pause-snapshotted anyway and
            Resume All re-downloads a file that is already correct on disk;
          * the job's url_resolver has been consumed but the resolved URL
            has not yet been committed, so the snapshot has neither a
            resolver nor a usable URL.

        Handing back a request also CLAIMS the job for the pause (see
        _claim_job_outcome), which is what actually closes the window: from
        that instant the job's own pool thread can no longer take it
        terminal — not by completing, not by failing, and not by
        publishing, since the engine asks the same claim for permission
        immediately before it commits the file to the user's output
        directory. Callers must therefore treat a returned request as "this
        job is now mine to pause", not as a passive read.

        A job caught mid-resolve is NOT skipped: the pre-resolve copy taken
        at the moment its resolver was consumed is handed back instead, so
        the job resumes by re-running the match rather than being silently
        dropped from the paused set (which lost the track outright — the
        engine still cancelled it, but nothing recorded it as resumable).

        Returns ``(None, None)`` for a key this orchestrator never
        registered a lock for (a stale key from a previous batch). Returns
        ``(state, None)`` when the job cannot be paused — it already reached
        a terminal state, or its own thread claimed it first — even though
        the key is known; callers must treat a ``None`` request as "do not
        pause this job".
        """
        lock = self._job_locks.get(key)
        if lock is None:
            return None, None
        with lock:
            state = self._aggregator.job_state(key)
            if is_terminal_state(state):
                return state, None
            if key in self._resolving_keys:
                req = self._pending_resolver_requests.get(key)
            else:
                req = self._active_requests.get(key)
            if req is None:
                return state, None
            if not self._claim_job_outcome_locked(key, "paused"):
                return state, None
            return state, dataclasses.replace(req)

    # ── Main entry point (blocking — call from background thread) ─────────────

    def run_batch(
        self,
        jobs: list[tuple[str, DownloadRequest]],
        delay_range: Optional[Tuple[float, float]] = None,
        preexisting: Optional[list[tuple[str, str]]] = None,
    ) -> BatchResult:
        """
        Execute a batch of downloads with bounded parallelism and optional staggered start.

        ``preexisting`` is a list of (key, existing_path) pairs for jobs the
        caller has already resolved to a file that exists on disk (the
        duplicate-skip policy) — no engine work runs for them. They are
        registered in the aggregator as terminal successes (JobState.
        PREEXISTING) up front, before the pool starts, so the batch total /
        completed counters and the final summary are correct even when a
        batch is entirely duplicate-skips (see core.batch_progress).
        """
        preexisting = preexisting or []
        total_jobs = len(jobs) + len(preexisting)

        if total_jobs == 0:
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

        all_keys = [key for key, _ in jobs] + [key for key, _ in preexisting]

        # Check for pre-cancellation
        if self._engine._cancel_event.is_set():
            logger.info("[Orchestrator] run_batch() — started in cancelled state")
            # Mark all as cancelled — including preexisting entries: a batch
            # that starts already-cancelled produces no successes at all.
            self._aggregator.reset(all_keys)
            for key in all_keys:
                self._aggregator.cancel(key)
                self._safe_cb("on_track_status", key, "cancelled")
            self._safe_cb("on_batch_finished", BatchOutcome.CANCELLED_BY_USER)
            return BatchResult(
                total=total_jobs, completed=0, failed=0, cancelled=True,
                outcome=BatchOutcome.CANCELLED_BY_USER,
            )

        self._total     = total_jobs
        self._completed = 0
        self._failed    = 0
        stagger_delay_range = tuple(delay_range) if delay_range else None
        self._aggregator.reset(
            all_keys,
            conservative_delay_range=CONSERVATIVE_DELAY_RANGE,
            stagger_delay_range=stagger_delay_range,
        )
        self._cancel_events.clear()
        self._job_locks.clear()
        self._active_requests.clear()
        self._resolving_keys.clear()
        self._pending_resolver_requests.clear()
        self._job_outcomes.clear()
        with self._phase_lock:
            self._phase_times.clear()
        with self._gate_lock:
            self._gate_last_release = None
        run_start = time.monotonic()

        # Batch workspace: downloads, conversion and post-processing happen
        # here instead of visibly inside the user's output directory — see
        # core.downloader's atomic-publish step and
        # utils.paths.make_batch_workspace. Skipped for any job that already
        # carries a workspace_dir (a paused-track resume re-submitted
        # through DownloadController.pause_track/resume_track) so a resume
        # keeps using the SAME workspace its .part file already lives in,
        # instead of orphaning it behind a brand-new empty one.
        #
        # Each job gets its OWN subdirectory inside the batch container so
        # two parallel jobs that resolve to the same final filename (e.g. a
        # playlist with two same-titled tracks) never collide in temporary
        # or intermediate storage — their .part / intermediate / final files
        # live under separate keys and only meet (if ever) at the published
        # destination.
        batch_container: Optional[Path] = None
        jobs_needing_workspace = [(key, req) for key, req in jobs if not req.workspace_dir]
        workspace_failed_keys: list[str] = []
        if jobs_needing_workspace:
            try:
                batch_container = make_batch_workspace(jobs_needing_workspace[0][1].output_dir)
            except OSError as exc:
                # Isolation failed at BOTH the same-volume and app-data
                # locations. Do NOT fall back to writing visible partials
                # into the user's output directory — fail these jobs with a
                # clear error instead (the invariant "downloads never appear
                # visibly in the output folder" wins over "download anyway").
                logger.error(
                    "[Orchestrator] Could not create an isolated batch workspace "
                    "(%s) — failing %d job(s) rather than writing visible partials",
                    exc, len(jobs_needing_workspace),
                )
                batch_container = None

            if batch_container is not None:
                for key, req in jobs_needing_workspace:
                    subdir = batch_container / _safe_subdir_name(key)
                    try:
                        subdir.mkdir(parents=True, exist_ok=True)
                        req.workspace_dir = str(subdir)
                    except OSError as exc:
                        # A per-job subdir that can't be created must fail
                        # THAT job — never fall back to sharing the container
                        # (a shared workspace_dir would let this job's
                        # success-cleanup rmtree a sibling's files).
                        logger.error(
                            "[Orchestrator] Could not create job workspace %s (%s) "
                            "— failing this job", subdir, exc,
                        )
                        workspace_failed_keys.append(key)
            else:
                workspace_failed_keys = [key for key, _ in jobs_needing_workspace]

            if workspace_failed_keys:
                err = classify_error(
                    OSError("Could not create a private download workspace")
                )
                failed = set(workspace_failed_keys)
                for key in workspace_failed_keys:
                    self._aggregator.fail(key)
                    with self._progress_lock:
                        self._failed += 1
                    self._safe_cb("on_track_status", key, "error")
                    self._safe_cb("on_track_error", key, err)
                # Drop the failed jobs so they never reach the pool; keep
                # everything that has a real workspace (fresh subdirs that
                # succeeded, plus preset-workspace resumes).
                jobs = [(key, req) for key, req in jobs if key not in failed]

        # Resolve duplicate-skips immediately — no engine work, no gate, no
        # stagger. Reported up front so the UI shows these cards as done
        # right away instead of waiting behind the real download jobs.
        for key, existing_path in preexisting:
            self._aggregator.mark_preexisting(key)
            with self._progress_lock:
                self._completed += 1
            self._safe_cb("on_track_preexisting", key, existing_path)
            self._safe_cb("on_job_count_changed", self._completed, self._total)
        if preexisting:
            snapshot = self._aggregator.snapshot()
            self._safe_cb("on_overall_progress", snapshot.progress)
            self._safe_cb("on_batch_snapshot", snapshot)

        # Only serialize YouTube jobs when there's more than one in this
        # batch — a lone YouTube job (e.g. single-track download, or a
        # paused-track resume) has no sibling to protect and gains nothing
        # from an artificial cooldown.
        youtube_job_count = sum(
            1 for _, req in jobs if self._is_conservative_youtube_job(req)
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
                if self._is_conservative_youtube_job(req):
                    self._aggregator.mark_serialized(key)

        n_workers = min(self._max_workers, self._total)
        futures: dict = {}

        # Register EVERY job's cancel event, per-job lock and live request
        # up front — before the (staggered, therefore slow) submit loop
        # below, not one-at-a-time inside it.
        #
        # A job that had not yet reached its turn in that loop had no
        # registration at all, and that had two consequences, both of which
        # silently lost work:
        #   * Global Pause's live_request_snapshot returned "unknown key",
        #     so the job was skipped, never captured as paused, and then
        #     cancelled outright by the whole-batch cancel that follows —
        #     with a delay_range configured, a 50-track batch paused a few
        #     seconds in lost nearly all of it;
        #   * cancel_track() was a no-op for it, so pausing a single
        #     not-yet-submitted track marked the card paused while the job
        #     went on to download anyway.
        # Registration is cheap and side-effect-free; only mark_submitted()
        # and the pool.submit() itself belong in the staggered loop.
        for key, req in jobs:
            ev = threading.Event()
            req.cancel_event = ev
            self._cancel_events[key] = ev
            self._job_locks[key] = threading.Lock()
            self._active_requests[key] = req
            self._aggregator.register(key)

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
                    
                # Stagger the starts so the batch does not hit the server as a
                # burst. Lazy (Spotify two-stage) jobs skip the stagger for the
                # opening pool-fill so their matches resolve in parallel and
                # pipeline behind the conservative gate; regular downloads keep
                # the original per-job stagger (see _should_stagger).
                is_lazy = getattr(req, "url_resolver", None) is not None
                if delay_range and self._should_stagger(i, n_workers, is_lazy):
                    sleep_time = random.uniform(*delay_range)
                    logger.debug(f"[Orchestrator] Staggering start: sleeping {sleep_time:.2f}s")
                    sleep_start = time.time()
                    while time.time() - sleep_start < sleep_time:
                        if self._engine._cancel_event.is_set():
                            break
                        time.sleep(0.2)

                if self._engine._cancel_event.is_set():
                    break

                # Cancel event / lock / live-request registration already
                # happened up front (see above) — only the submission
                # itself is staggered.
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
                        self._mark_cancelled(key)
                    except Exception as exc:  # noqa: BLE001
                        if self._engine._cancel_event.is_set():  # noqa: SLF001
                            self._mark_cancelled(key)
                            continue
                        if not self._claim_job_outcome(key, "terminal"):
                            continue  # paused out from under this thread
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
                # A job a Global Pause claimed is paused, not cancelled: it
                # keeps its snapshot and its "paused" card. Telling the UI
                # "cancelled" here would overwrite that label a moment after
                # the pause set it (a whole-batch pause cancels the engine,
                # so every paused job lands in this loop).
                if self._job_outcomes.get(key) == "paused":
                    continue
                self._safe_cb("on_track_status", key, "cancelled")

        # Workspace cleanup: only when this orchestrator created one AND the
        # batch ran to a natural end with nothing paused/cancelled in it.
        # A whole-batch cancel might really be a pause — the orchestrator
        # can't tell the difference (that intent lives one layer up, in
        # DownloadController) — so its workspace, and any .part /
        # intermediate files in it, must survive for a possible resume.
        # A per-track pause (DownloadController.pause_track ->
        # cancel_track()) doesn't set the engine-level cancel event at all
        # — only that one job's own cancel_events entry — so was_cancelled
        # alone would miss it and delete a still-needed .part file out from
        # under a single paused track while its siblings kept downloading.
        # Cancellation-specific full cleanup is a separate, later concern.
        any_track_paused_or_cancelled = any(
            ev.is_set() for ev in self._cancel_events.values()
        )
        if batch_container is not None and not was_cancelled and not any_track_paused_or_cancelled:
            self._cleanup_workspace(batch_container)

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
            if snapshot.preexisting > 0:
                self._safe_cb(
                    "on_status_message",
                    f"Done — {self._total} track{s} "
                    f"({snapshot.downloaded} downloaded, {snapshot.preexisting} already existed).",
                )
            else:
                self._safe_cb(
                    "on_status_message",
                    f"Done — {self._total} track{s} downloaded.",
                )

        self._safe_cb("on_batch_finished", outcome)

        logger.info(
            "[Orchestrator] Batch finished: total=%d completed=%d failed=%d cancelled=%s outcome=%s",
            self._total, self._completed, self._failed, was_cancelled, outcome.value,
        )
        self._log_phase_summary(time.monotonic() - run_start)

        return BatchResult(
            total=self._total,
            completed=self._completed,
            failed=self._failed,
            cancelled=was_cancelled,
            outcome=outcome,
        )

    # ── Stagger / timing helpers ──────────────────────────────────────────────

    @staticmethod
    def _should_stagger(i: int, n_workers: int, is_lazy: bool) -> bool:
        """Whether job ``i`` gets the inter-start stagger sleep.

        Lazy (Spotify two-stage) jobs carry a ``url_resolver``: their opening
        wave (the first ``n_workers`` jobs) is submitted WITHOUT stagger so
        their matches resolve in parallel — the actual downloads still serialize
        behind the conservative gate, so there is no request burst. Only lazy
        jobs beyond the opening wave are staggered.

        Regular jobs (direct URLs, no resolver) keep the original burst-
        politeness behaviour — every job after the first is staggered — because
        for them the opening wave IS a burst of real downloads (e.g. fast mode
        or non-YouTube, where no gate serializes them).
        """
        if is_lazy:
            return i >= n_workers
        return i > 0

    # ── Batch workspace ────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_workspace(batch_container: Path) -> None:
        """Best-effort removal of a whole batch container this orchestrator
        created, plus the ``.bananaflow_tmp`` root if that leaves it empty.
        Never raises — a cleanup failure must not turn an otherwise-
        successful batch into a reported error. Only ever removes the
        BananaFlow-owned container and its dedicated root, never the user's
        output directory (``.bananaflow_tmp``'s parent), which rmdir would
        refuse anyway unless empty."""
        try:
            shutil.rmtree(batch_container, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Orchestrator] Failed to clean up batch workspace %s: %s",
                batch_container, exc,
            )
        # Container root (``.bananaflow_tmp`` / ``download_workspaces``) —
        # remove only if now empty (a concurrent batch keeps it alive; rmdir
        # refuses a non-empty dir) and only if it's a recognised workspace
        # root, so this can never rmdir the user's output directory.
        root = batch_container.parent
        if _is_workspace_owned(root):
            try:
                root.rmdir()
            except OSError:
                pass

    def _cleanup_job_workspace(self, req: DownloadRequest) -> None:
        """Remove a single completed job's private workspace subdirectory and
        its intermediates, without touching any sibling job's subdir.

        Called when a job publishes successfully. Sibling-safe by
        construction: each job owns a separate subdir, so removing one never
        races another's files. Also removes the batch container and the
        ``.bananaflow_tmp`` root if this was the last job in them (only when
        empty — an rmdir, never a recursive delete) so a resumed single
        track, whose container this orchestrator did NOT create and would
        therefore never batch-clean, does not leak an empty workspace."""
        if not req.workspace_dir:
            return
        subdir = Path(req.workspace_dir)
        try:
            shutil.rmtree(subdir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Orchestrator] job workspace cleanup failed for %s: %s", subdir, exc)
            return
        # Peel back empty BananaFlow-owned parents (batch-<id>, then the
        # container root). The _is_workspace_owned guard stops the sweep
        # before it could ever reach — let alone rmdir — the user's output
        # directory; rmdir also refuses any level a sibling still occupies.
        node = subdir.parent
        while _is_workspace_owned(node):
            try:
                node.rmdir()
            except OSError:
                break
            node = node.parent

    def _record_phase(self, name: str, seconds: float) -> None:
        """Accumulate a per-phase duration for the end-of-batch timing summary."""
        with self._phase_lock:
            slot = self._phase_times.setdefault(name, [0.0, 0.0])
            slot[0] += seconds
            slot[1] += 1

    def _log_phase_summary(self, wall_seconds: float) -> None:
        """Emit the aggregate timing summary for the batch.

        Two distinct kinds of number, kept separate on purpose:

        * **cumulative worker time** — per-phase sums across *parallel* workers
          (resolver_wait, gate_wait, download_time, first_byte_wait, cooldown).
          These overlap in wall-clock and can exceed ``wall_seconds``; they are
          NOT a partition of it. Presented as totals + averages only.
        * **critical path** — the serialized download pipeline that actually
          bounds a conservative batch: ``gate_idle`` (starvation — the pipe sat
          idle waiting for a match) plus the serial download+cooldown chain.
          This is what to compare against wall time.
        """
        with self._phase_lock:
            phases = {k: tuple(v) for k, v in self._phase_times.items()}

        cumulative = []
        for name in (
            "resolver_wait", "gate_wait", "download_time", "first_byte_wait", "cooldown",
        ):
            total, count = phases.get(name, (0.0, 0.0))
            if count:
                cumulative.append(
                    f"{name}={total:.1f}s(n={int(count)}, avg={total / count:.2f}s)"
                )
        logger.info(
            "[timing][batch] wall=%.1fs | cumulative worker time (parallel, "
            "not a partition of wall): %s",
            wall_seconds, " ".join(cumulative) if cumulative else "(no phase data)",
        )

        # Critical path is only meaningful when the conservative gate serialized
        # this batch; without it there is no single serial pipeline to bound.
        if self._youtube_serialize:
            gate_idle_total = phases.get("gate_idle", (0.0, 0.0))[0]
            download_total = phases.get("download_time", (0.0, 0.0))[0]
            cooldown_total = phases.get("cooldown", (0.0, 0.0))[0]
            serial_work = download_total + cooldown_total
            idle_pct = (gate_idle_total / wall_seconds * 100.0) if wall_seconds > 0 else 0.0
            logger.info(
                "[timing][batch] critical path: gate_idle(starvation)=%.1fs (%.0f%% of "
                "wall) serial_download+cooldown=%.1fs — near-zero gate_idle means the "
                "cooldown/download chain is the bottleneck, not match availability.",
                gate_idle_total, idle_pct, serial_work,
            )

    # ── Per-job runner (pool thread) ──────────────────────────────────────────

    @staticmethod
    def _is_conservative_youtube_job(req: DownloadRequest) -> bool:
        """Whether a job must pass through the conservative YouTube serial gate.

        A Spotify two-stage job carries a ``url_resolver`` and always resolves
        to a YouTube/YTM URL, so it counts as a conservative YouTube job even
        though ``req.url`` is still a placeholder at gate-decision time. This
        keeps the serialize decision, the ETA overhead, and the actual gating
        consistent for pending Spotify downloads.
        """
        if getattr(req, "youtube_reliability_mode", "conservative") != "conservative":
            return False
        return is_youtube_url(req.url) or getattr(req, "url_resolver", None) is not None

    def _resolve_lazy_url(
        self, key: str, req: DownloadRequest, cancel_ev: threading.Event
    ) -> bool:
        """Run a Spotify two-stage ``url_resolver`` (if any) to fill ``req.url``.

        Runs *before* the conservative YouTube gate is acquired, so matching
        happens in parallel across workers while only the downloads themselves
        serialize. Returns True if the job was cancelled (caller should abort).
        The track stays visually "queued" during the match — no matching/YouTube
        wording is surfaced. A bad/failed match never sinks the job.
        """
        if req.url_resolver is None:
            return False
        resolver = req.url_resolver
        lock = self._job_locks.get(key)
        # Mark the resolver "consumed" and the key "mid-resolve" as one
        # atomic step (under lock) so a concurrent live_request_snapshot()
        # can never observe url_resolver already cleared but req.url still
        # holding the stale placeholder. The resolver call itself — a
        # network round-trip — deliberately runs OUTSIDE the lock so a
        # snapshot request for some OTHER job is never blocked on it.
        #
        # A pre-resolve COPY is stashed in the same atomic step. A Global
        # Pause that lands in this window used to be told "no safe snapshot
        # right now" and skipped the job entirely — but the pause still
        # cancelled it, so the track was simply lost: absent from the
        # paused set, so Resume All never brought it back. The stashed copy
        # still carries the resolver and the placeholder URL, which is
        # exactly what a resume needs: it re-runs the match and then
        # downloads, the same as if it had never started.
        if lock is not None:
            with lock:
                self._pending_resolver_requests[key] = dataclasses.replace(req)
                req.url_resolver = None
                self._resolving_keys.add(key)
        else:
            req.url_resolver = None
        if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
            if lock is not None:
                with lock:
                    self._resolving_keys.discard(key)
                    self._pending_resolver_requests.pop(key, None)
            self._mark_cancelled(key)
            return True
        resolve_start = time.monotonic()
        try:
            resolved = resolver(cancel_ev)
        except Exception as exc:  # noqa: BLE001 - a bad match must not sink the job
            logger.debug("[Orchestrator] URL resolver failed for %s: %s", key, exc)
            resolved = ""
        resolver_wait = time.monotonic() - resolve_start
        self._record_phase("resolver_wait", resolver_wait)
        logger.debug("[timing][track] %s resolver_wait=%.2fs", key, resolver_wait)
        if lock is not None:
            with lock:
                if resolved:
                    req.url = resolved
                self._resolving_keys.discard(key)
                self._pending_resolver_requests.pop(key, None)
        elif resolved:
            req.url = resolved
        if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
            self._mark_cancelled(key)
            return True
        return False

    def _download_one(self, key: str, req: DownloadRequest) -> None:
        cancel_ev = self._cancel_events[key]

        if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
            self._mark_cancelled(key)
            return

        # Decide gate membership BEFORE resolving — a Spotify two-stage job's
        # url_resolver is still set here, and _is_conservative_youtube_job uses
        # that to recognise it as a YouTube job despite the placeholder URL.
        conservative_youtube = (
            self._youtube_serialize and self._is_conservative_youtube_job(req)
        )

        # Resolve the lazy URL now, BEFORE acquiring the gate, so matching runs
        # in parallel across workers and only the downloads serialize.
        if self._resolve_lazy_url(key, req, cancel_ev):
            return

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
            gate_start = time.monotonic()
            self._youtube_gate.acquire()
            acquired_at = time.monotonic()
            gate_wait = acquired_at - gate_start
            self._record_phase("gate_wait", gate_wait)
            logger.debug("[timing][track] %s gate_wait=%.2fs", key, gate_wait)
            # Gate starvation: time between the previous holder releasing and
            # this acquire. ~0 means a matched job was always ready to go (the
            # pipeline never starved); large means the download pipe sat idle
            # waiting on a match — the signal that match availability, not the
            # cooldown, is the bottleneck.
            with self._gate_lock:
                last_release = self._gate_last_release
            if last_release is not None:
                self._record_phase("gate_idle", max(0.0, acquired_at - last_release))
            if cancel_ev.is_set() or self._engine._cancel_event.is_set():  # noqa: SLF001
                self._youtube_gate.release()
                self._mark_cancelled(key)
                return

        download_start = time.monotonic()
        try:
            self._download_one_locked(key, req, cancel_ev)
        finally:
            download_time = time.monotonic() - download_start
            self._record_phase("download_time", download_time)
            logger.debug("[timing][track] %s download_time=%.2fs", key, download_time)
            if conservative_youtube:
                self._youtube_cooldown(cancel_ev, key)
                with self._gate_lock:
                    self._gate_last_release = time.monotonic()
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
                break
            chunk = min(0.2, delay - slept)
            time.sleep(chunk)
            slept += chunk
        self._record_phase("cooldown", slept)

    def _download_one_locked(self, key: str, req: DownloadRequest, cancel_ev: threading.Event) -> None:
        """The actual per-job download logic, run either directly (non-YouTube
        or fast mode) or while holding ``self._youtube_gate`` (conservative
        YouTube jobs — see _download_one).

        Any Spotify two-stage ``url_resolver`` has already run in _download_one
        (before the gate), so ``req.url`` is final here.
        """
        self._safe_cb("on_track_status", key, "downloading")
        logger.debug("[Orchestrator] Starting %s", key)

        # "downloading" is emitted here, before any byte arrives. Time the gap
        # to the first non-zero progress separately so the honest "download
        # actually started" moment (first byte) is distinguished from the
        # engine-start status.
        engine_start_ts = time.monotonic()
        first_byte_seen = [False]
        update_counter = [0]

        def on_progress(p: DownloadProgress) -> None:
            if not first_byte_seen[0] and ((p.downloaded_bytes or 0) > 0 or (p.fraction or 0) > 0):
                first_byte_seen[0] = True
                first_byte_wait = time.monotonic() - engine_start_ts
                self._record_phase("first_byte_wait", first_byte_wait)
                logger.debug(
                    "[timing][track] %s first_byte_wait=%.2fs (engine start -> first byte)",
                    key, first_byte_wait,
                )

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
            # Completion is a terminal transition and goes through the same
            # per-job claim a Global Pause uses, so the two are mutually
            # exclusive rather than two unlocked code paths that could both
            # "win". Losing the claim is not an error: the engine's publish
            # gate (below) already refused to commit the file, so nothing
            # was written to the user's output directory — the job is paused
            # and will be resumed.
            if not self._claim_job_outcome(key, "terminal"):
                logger.info(
                    "[Orchestrator] %s was paused before it completed — "
                    "not reporting it as done", key,
                )
                return
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
            # The final file has already been published out of this job's
            # private workspace subdir; remove the subdir (and its leftover
            # intermediates) now. Sibling-safe — each job owns its own
            # subdir — and it also prevents a resumed single track from
            # leaking a workspace the batch-level cleanup would never reach.
            self._cleanup_job_workspace(req)

        def on_error(p: DownloadProgress) -> None:
            # Failure is terminal too — a job a pause already claimed keeps
            # its resumable snapshot and its "paused" card instead of being
            # flipped to "error" (the resume retries it).
            if not self._claim_job_outcome(key, "terminal"):
                return
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
        # The engine asks this immediately before it makes the finished file
        # visible in the user's output directory. Returning False means a
        # Global Pause claimed the job first, so the publish is abandoned and
        # the file stays in the workspace for the resume — the completion
        # side of the same boundary live_request_snapshot guards, moved to
        # the last possible instant so "paused" and "published" can never
        # both be true.
        req.publish_gate = lambda: self._claim_job_outcome(key, "terminal")

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
