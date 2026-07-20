"""
core/batch_progress.py  –  Framework-agnostic batch download aggregator
=========================================================================
The bottom progress bar, the aggregate speed, and the "time remaining"
estimate on the Downloads page all describe the **whole selected batch**,
not whichever track happened to emit the most recent callback.

This module owns that math in one pure-Python place (zero Qt / GUI
imports) so it can be unit-tested exhaustively and reused by a future
CLI.  ``DownloadOrchestrator`` feeds per-job updates in; the UI layer
reads a single coherent :class:`BatchSnapshot` out.

Why not ``sum(fractions) / n``
------------------------------
The previous design averaged per-track fractions with equal weight, so a
5 MB file and a 1 GB file counted the same — a batch could read "50%"
with 99% of the *bytes* still to go.  When byte totals are known we weight
by bytes instead:

    progress = sum(downloaded_bytes) / sum(total_bytes)

Unknown sizes
-------------
Queued (and freshly-started) jobs often don't expose a total size yet.
For those we estimate a size from the average of the sizes we *do* know
(completed jobs count at their real final size), and fall back to a
plain normalized per-job fraction only when nothing is known yet.  This
keeps progress moving off zero, keeps it from lurching backwards when a
real total finally arrives (a monotonic floor guards that), and never
lets a single tiny/huge file dominate.

ETA
---
The total-remaining-time estimate divides the remaining bytes across all
active + queued jobs (real bytes where known, estimated where not) by the
smoothed aggregate speed of the *active* jobs.  When there isn't enough
information yet it returns ``None`` and the UI shows "calculating…".  The
value is deliberately labelled an estimate in the UI — see
``eta_is_estimate``.

Conservative-mode YouTube gate: when the orchestrator serializes YouTube
jobs (core.youtube_reliability — one at a time, with a cooldown between
them), that forced wait cannot overlap with anything else and would
otherwise be invisible to a pure bytes/speed estimate. Pass
``conservative_delay_range`` to the constructor and call
:meth:`mark_serialized` per job so the ETA adds one average cooldown for
every job still queued behind the gate — see :meth:`_eta_locked` and
:meth:`_conservative_overhead_locked`.

Limitations are documented on :meth:`snapshot`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

_UNSET = object()


# ──────────────────────────────────────────────────────────────────────────────
# Job state
# ──────────────────────────────────────────────────────────────────────────────

class JobState(Enum):
    QUEUED    = "queued"
    ACTIVE    = "active"
    COMPLETED = "completed"
    FAILED    = "failed"
    PAUSED    = "paused"
    CANCELLED = "cancelled"


# Terminal states no longer contribute a live speed and are "done" for the
# purpose of the outstanding-work denominator.
_TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}


@dataclass
class JobProgress:
    """Mutable per-job accounting held by the aggregator."""

    key:              str
    state:            JobState        = JobState.QUEUED
    downloaded_bytes: int             = 0
    total_bytes:      Optional[int]   = None      # known real/estimated total
    total_bytes_is_estimate: bool     = False
    fraction:         float           = 0.0       # 0..1 (from yt-dlp)
    speed_bps:        float           = 0.0
    eta_seconds:      Optional[float] = None       # per-track ETA (not used for batch ETA)
    started_at:       Optional[float] = None
    ended_at:         Optional[float] = None
    submitted:        bool            = False
    # True for a YouTube job the orchestrator forces onto its conservative-mode
    # serial gate (core.youtube_reliability) — it cannot start until every
    # other serialized job ahead of it has finished AND the cooldown between
    # them has elapsed, regardless of max_workers/parallelism. See
    # BatchProgressAggregator._eta_locked for how this adds a mandatory,
    # non-overlappable wait on top of the network-transfer estimate.
    serialized:       bool            = False

    @property
    def known_size(self) -> bool:
        return self.total_bytes is not None and self.total_bytes > 0

    @property
    def real_size(self) -> bool:
        return self.known_size and not self.total_bytes_is_estimate

    @property
    def duration(self) -> Optional[float]:
        if self.started_at is None or self.ended_at is None:
            return None
        d = self.ended_at - self.started_at
        return d if d > 0 else None


# ──────────────────────────────────────────────────────────────────────────────
# Immutable snapshot handed to the UI
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BatchSnapshot:
    """A single coherent read of the whole batch's progress state."""

    total:      int
    queued:     int
    active:     int
    completed:  int
    failed:     int
    paused:     int
    cancelled:  int

    progress:        float            # 0..1, weighted, monotonic within a batch
    byte_weighted:   bool             # True when ≥1 job had a known byte total
    speed_bps:       float            # smoothed sum of active-job speeds
    raw_speed_bps:   float            # instantaneous sum of active-job speeds
    eta_seconds:     Optional[float]  # whole-batch remaining time, or None
    eta_is_estimate: bool             # True unless every remaining job's size is known

    @property
    def finished(self) -> int:
        return self.completed + self.failed + self.cancelled

    @property
    def is_empty(self) -> bool:
        return self.total == 0


# ──────────────────────────────────────────────────────────────────────────────
# Aggregator
# ──────────────────────────────────────────────────────────────────────────────

class BatchProgressAggregator:
    """
    Thread-safe accumulator of per-job download progress for one batch.

    All mutators take the same lock, so the orchestrator's pool threads can
    call them freely while the UI thread reads :meth:`snapshot`.

    Parameters
    ----------
    speed_smoothing : EMA factor for the displayed aggregate speed. Higher =
                      more responsive, lower = smoother. 0.3 is a calm default.
    time_fn         : Injectable clock (defaults to ``time.monotonic``) so
                      duration-based ETA fallbacks are deterministic in tests.
    conservative_delay_range : (min, max) seconds the orchestrator's
                      YouTube-conservative-mode gate sleeps between two
                      serialized YouTube jobs (core.youtube_reliability.
                      CONSERVATIVE_DELAY_RANGE). When set, the ETA adds one
                      average cooldown per still-queued serialized job — see
                      :meth:`_eta_locked`. ``None`` (the default) means
                      conservative mode is not in effect for this batch and
                      no overhead is added.
    """

    def __init__(
        self,
        speed_smoothing: float = 0.3,
        time_fn: Optional[Callable[[], float]] = None,
        conservative_delay_range: Optional[Tuple[float, float]] = None,
    ) -> None:
        import time as _time
        self._time = time_fn or _time.monotonic
        self._alpha = max(0.01, min(1.0, speed_smoothing))
        self._conservative_delay_range = conservative_delay_range
        self._stagger_delay_range: Optional[Tuple[float, float]] = None
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobProgress] = {}
        self._smoothed_speed: float = 0.0
        self._progress_floor: float = 0.0
        self._floor_uses_estimates: bool = False

    # ── Registration ──────────────────────────────────────────────────────────

    def reset(
        self,
        keys: Optional[list[str]] = None,
        *,
        conservative_delay_range=_UNSET,
        stagger_delay_range=_UNSET,
    ) -> None:
        """Start a fresh batch. Optionally pre-register queued job keys."""
        with self._lock:
            if conservative_delay_range is not _UNSET:
                self._conservative_delay_range = conservative_delay_range
            if stagger_delay_range is not _UNSET:
                self._stagger_delay_range = stagger_delay_range
            self._jobs = {}
            self._smoothed_speed = 0.0
            self._progress_floor = 0.0
            self._floor_uses_estimates = False
            if keys:
                for k in keys:
                    self._jobs[k] = JobProgress(key=k, state=JobState.QUEUED)

    def register(self, key: str) -> None:
        """Ensure a job exists in the QUEUED state."""
        with self._lock:
            self._jobs.setdefault(key, JobProgress(key=key, state=JobState.QUEUED))

    def mark_submitted(self, key: str) -> None:
        """Record that the orchestrator has submitted a job to the pool.

        A submitted job may still be waiting for a worker slot, but it has
        already paid the application-controlled start-stagger delay. Jobs that
        remain unsubmitted are the only ones that add that delay to ETA.
        """
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key, state=JobState.QUEUED))
            job.submitted = True

    def mark_serialized(self, key: str) -> None:
        """Flag a job as subject to the conservative-mode YouTube serial gate
        (only one such job runs at a time; each waits a cooldown after the
        previous one). Call once per job, right after registration, when the
        orchestrator has decided the batch's conservative-mode gate applies to
        it. Has no effect if ``conservative_delay_range`` was not configured."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key, state=JobState.QUEUED))
            job.serialized = True

    # ── Mutators (called from pool threads) ───────────────────────────────────

    def update(
        self,
        key: str,
        *,
        downloaded_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
        total_bytes_estimate: Optional[int] = None,
        fraction: Optional[float] = None,
        speed_bps: Optional[float] = None,
        eta_seconds: Optional[float] = None,
    ) -> None:
        """Record a live progress tick for an active job."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key))
            if job.state in _TERMINAL:
                return
            job.submitted = True
            if job.state != JobState.ACTIVE:
                job.state = JobState.ACTIVE
                if job.started_at is None:
                    job.started_at = self._time()
            if downloaded_bytes is not None:
                job.downloaded_bytes = max(job.downloaded_bytes, int(downloaded_bytes))
            if total_bytes is not None and total_bytes > 0:
                job.total_bytes = int(total_bytes)
                job.total_bytes_is_estimate = False
            elif total_bytes_estimate is not None and total_bytes_estimate > 0 and not job.real_size:
                job.total_bytes = int(total_bytes_estimate)
                job.total_bytes_is_estimate = True
            if fraction is not None:
                job.fraction = max(0.0, min(1.0, fraction))
            if speed_bps is not None:
                job.speed_bps = max(0.0, float(speed_bps))
            job.eta_seconds = eta_seconds
            self._recompute_speed_locked()

    def complete(self, key: str, final_bytes: Optional[int] = None) -> None:
        """Mark a job finished successfully; it counts at its full size."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key))
            if job.state in (JobState.FAILED, JobState.CANCELLED):
                return
            job.submitted = True
            job.state = JobState.COMPLETED
            job.fraction = 1.0
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.ended_at = self._time()
            if final_bytes is not None and final_bytes > 0:
                job.downloaded_bytes = int(final_bytes)
                job.total_bytes = int(final_bytes)
                job.total_bytes_is_estimate = False
            elif job.known_size:
                job.downloaded_bytes = int(job.total_bytes)
            self._recompute_speed_locked()

    def fail(self, key: str) -> None:
        self._terminate(key, JobState.FAILED)

    def cancel(self, key: str) -> None:
        self._terminate(key, JobState.CANCELLED)

    def pause(self, key: str) -> None:
        """Pause preserves the job's accumulated bytes but stops its speed."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key))
            job.state = JobState.PAUSED
            job.speed_bps = 0.0
            job.eta_seconds = None
            self._recompute_speed_locked()

    def _terminate(self, key: str, state: JobState) -> None:
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key))
            if job.state in _TERMINAL:
                return
            job.state = state
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.ended_at = self._time()
            self._recompute_speed_locked()

    def cancel_outstanding(self) -> list[str]:
        """Cancel every non-terminal job and return the keys that changed."""
        changed: list[str] = []
        with self._lock:
            for key, job in self._jobs.items():
                if job.state in _TERMINAL:
                    continue
                job.state = JobState.CANCELLED
                job.speed_bps = 0.0
                job.eta_seconds = None
                job.ended_at = self._time()
                changed.append(key)
            if changed:
                self._recompute_speed_locked()
        return changed

    # ── Read ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> BatchSnapshot:
        """
        Return a coherent immutable view of the whole batch.

        Limitations
        -----------
        * ``eta_seconds`` divides *remaining bytes* by the smoothed active
          speed. Sizes for not-yet-started jobs are estimated from the mean
          of the sizes already known, so the estimate sharpens as the batch
          reveals real totals. It is always labelled an estimate
          (``eta_is_estimate``) unless every remaining job's size is already
          known AND no conservative-mode overhead applies.
        * Conservative-mode YouTube serialization (see module docstring) adds
          one *average* cooldown per still-queued serialized job. This is a
          fixed-average approximation: it does not know how much of an
          in-progress cooldown has already elapsed, and does not model
          per-job start staggering (``download_delay_range``) beyond that —
          so it can still run slightly optimistic or pessimistic versus the
          real schedule, especially for large queues of not-yet-started
          YouTube jobs.
        * When neither byte totals, a completed-job duration history, nor
          conservative-mode overhead exist, ``eta_seconds`` is ``None`` and
          the UI shows "calculating…".
        """
        with self._lock:
            return self._snapshot_locked()

    # ── Internal calculation ──────────────────────────────────────────────────

    def _recompute_speed_locked(self) -> None:
        raw = sum(j.speed_bps for j in self._jobs.values() if j.state == JobState.ACTIVE)
        # EMA toward the instantaneous sum. Snapping straight to zero when the
        # last active job finishes avoids a lingering ghost speed.
        if raw <= 0.0:
            self._smoothed_speed = 0.0
        else:
            self._smoothed_speed = (
                self._alpha * raw + (1.0 - self._alpha) * self._smoothed_speed
            )

    def _raw_active_speed_locked(self) -> float:
        return sum(j.speed_bps for j in self._jobs.values() if j.state == JobState.ACTIVE)

    def _mean_known_size_locked(self) -> Optional[float]:
        sizes = [float(j.total_bytes) for j in self._jobs.values() if j.known_size]
        if not sizes:
            return None
        return sum(sizes) / len(sizes)

    def _progress_locked(self) -> tuple[float, bool]:
        """
        Compute weighted batch progress in 0..1 and whether it was byte-weighted.

        Strategy:
          * Jobs with a known total contribute real ``downloaded / total``.
          * Jobs without a known total are estimated at the mean known size
            (so they carry proportional weight), or — when nothing is known —
            fall back to a plain per-job fraction average.
        A monotonic floor prevents the value from stepping backwards when an
        estimate is replaced by a smaller real total.
        """
        jobs = list(self._jobs.values())
        total_jobs = len(jobs)
        if total_jobs == 0:
            return 0.0, False

        mean_known = self._mean_known_size_locked()

        uses_estimates = False
        if mean_known is None:
            # No byte information anywhere yet — normalized fraction average.
            frac_sum = sum(
                1.0 if j.state == JobState.COMPLETED else j.fraction
                for j in jobs
            )
            raw = frac_sum / total_jobs
            byte_weighted = False
            uses_estimates = True
        else:
            numerator = 0.0
            denominator = 0.0
            for j in jobs:
                if j.known_size:
                    size = float(j.total_bytes)
                    if j.total_bytes_is_estimate:
                        uses_estimates = True
                    done = float(min(j.downloaded_bytes, j.total_bytes))
                    if j.state == JobState.COMPLETED:
                        done = size
                else:
                    size = mean_known
                    uses_estimates = True
                    if j.state == JobState.COMPLETED:
                        done = size
                    else:
                        done = j.fraction * size
                numerator += done
                denominator += size
            raw = (numerator / denominator) if denominator > 0 else 0.0
            byte_weighted = True

        raw = max(0.0, min(1.0, raw))
        # Enforce monotonicity within the batch, except for the specific case
        # that caused the old footer to lie: an estimate-derived floor can be
        # corrected downward when real byte evidence later proves it wildly
        # optimistic. A fully-finished batch is allowed to reach exactly its
        # computed value (which will be 1.0 when everything completed, or <1.0
        # when some jobs were cancelled/failed).
        if raw < self._progress_floor:
            estimate_correction = (
                self._floor_uses_estimates
                and (self._progress_floor - raw) > 0.05
            )
            if estimate_correction:
                self._progress_floor = raw
                self._floor_uses_estimates = uses_estimates
            else:
                raw = self._progress_floor
        else:
            self._progress_floor = raw
            self._floor_uses_estimates = uses_estimates
        return raw, byte_weighted

    def _average_delay(self, delay_range: Optional[Tuple[float, float]]) -> float:
        if not delay_range:
            return 0.0
        lo, hi = delay_range
        return max(0.0, (float(lo) + float(hi)) / 2.0)

    def _conservative_overhead_locked(self, outstanding: list["JobProgress"]) -> float:
        """
        Extra wall-clock time the conservative-mode YouTube gate forces on top
        of raw network transfer: one average cooldown for every job still
        QUEUED (not yet started) that the orchestrator flagged as
        ``serialized``. Only one serialized job runs at a time regardless of
        ``max_workers``, so this time cannot overlap with anything else and
        must be added, not folded into the parallel-speed estimate.

        A job already ACTIVE, PAUSED, or terminal doesn't add to this — its
        own transfer cost is already counted by the byte/duration branches of
        :meth:`_eta_locked` (or, for PAUSED, deliberately excluded, matching
        "pause preserves progress but pauses the clock").
        """
        avg_delay = self._average_delay(self._conservative_delay_range)
        if avg_delay <= 0:
            return 0.0
        queued_serialized = sum(
            1 for j in outstanding
            if j.serialized and j.state == JobState.QUEUED
        )
        return queued_serialized * avg_delay

    def _stagger_overhead_locked(self, outstanding: list["JobProgress"]) -> float:
        """Average configured start-stagger delay for jobs not submitted yet."""
        avg_delay = self._average_delay(self._stagger_delay_range)
        if avg_delay <= 0:
            return 0.0
        not_submitted = sum(
            1 for j in outstanding
            if j.state == JobState.QUEUED and not j.submitted
        )
        return not_submitted * avg_delay

    def _eta_locked(self, speed: float) -> tuple[Optional[float], bool]:
        """
        Whole-batch remaining time. Returns (seconds, is_estimate).

        Primary: remaining bytes (real + estimated) / smoothed active speed.
        Fallback: average completed-job duration × outstanding jobs when no
        live speed/bytes are available but history exists.
        Conservative-mode overhead (see ``_conservative_overhead_locked``) is
        added on top of either branch — and can produce a valid ETA on its own
        when there is no byte/speed/duration data yet (e.g. the very first
        cooldown of a batch), instead of leaving the UI stuck on "calculating".

        Limitation: this models the cooldown as a fixed *average* delay per
        still-queued serialized job. It does not know how much of an
        in-progress cooldown has already elapsed, nor does it model any
        interaction between the serialized gate and non-serialized parallel
        jobs finishing at different times — it is a defensible additive
        approximation, not an exact schedule simulation.
        """
        jobs = list(self._jobs.values())
        outstanding = [j for j in jobs if j.state not in _TERMINAL]
        if not outstanding:
            return None, False

        overhead = (
            self._conservative_overhead_locked(outstanding)
            + self._stagger_overhead_locked(outstanding)
        )
        mean_known = self._mean_known_size_locked()

        if speed > 0 and mean_known is not None:
            remaining_bytes = 0.0
            all_known = True
            for j in outstanding:
                if j.known_size:
                    if j.total_bytes_is_estimate:
                        all_known = False
                    remaining_bytes += max(0.0, float(j.total_bytes) - j.downloaded_bytes)
                else:
                    all_known = False
                    # Estimate: a fraction-aware remainder of the mean size.
                    remaining_bytes += max(0.0, mean_known * (1.0 - j.fraction))
            eta = remaining_bytes / speed + overhead
            return max(0.0, eta), ((not all_known) or overhead > 0)

        # Duration-history fallback (used when speed is momentarily 0 but the
        # batch is genuinely progressing, e.g. between conservative-mode jobs).
        durations = [d for d in (j.duration for j in jobs) if d is not None]
        active = sum(1 for j in jobs if j.state == JobState.ACTIVE) or 1
        if durations:
            avg = sum(durations) / len(durations)
            # Parallelism divides the outstanding wall-clock cost.
            eta = avg * len(outstanding) / active + overhead
            return max(0.0, eta), True

        # No byte/speed/duration data at all yet — the mandatory cooldown
        # overhead alone is still a valid (if rough) answer; otherwise we
        # truly don't know and the UI should show "calculating…".
        if overhead > 0:
            return overhead, True

        return None, True

    def _snapshot_locked(self) -> BatchSnapshot:
        counts = {s: 0 for s in JobState}
        for j in self._jobs.values():
            counts[j.state] += 1

        progress, byte_weighted = self._progress_locked()
        raw_speed = self._raw_active_speed_locked()
        eta, eta_estimate = self._eta_locked(self._smoothed_speed)

        return BatchSnapshot(
            total=len(self._jobs),
            queued=counts[JobState.QUEUED],
            active=counts[JobState.ACTIVE],
            completed=counts[JobState.COMPLETED],
            failed=counts[JobState.FAILED],
            paused=counts[JobState.PAUSED],
            cancelled=counts[JobState.CANCELLED],
            progress=progress,
            byte_weighted=byte_weighted,
            speed_bps=self._smoothed_speed,
            raw_speed_bps=raw_speed,
            eta_seconds=eta,
            eta_is_estimate=eta_estimate,
        )
