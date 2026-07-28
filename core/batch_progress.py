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

ETA — measured batch throughput
------------------------------
The remaining-time estimate measures the batch's **output rate**, not any
individual job's residence time, and multiplies it by the work left:

    cycle = (t_last_completion - anchor) / completions_since_anchor
    eta   = outstanding * cycle - min(now - t_last_completion, cycle)

``cycle`` is wall-seconds-per-completion.  Because it is a **time span
divided by a count**, it already encodes however much parallelism the
batch actually achieved — three workers finishing a job each per 10 s
yield a 3.33 s cycle, a serialized gate yields ~10 s — so one formula
covers both regimes with no worker-count term anywhere.

The anchor is an observed completion, never the batch's start.  Everything
a batch pays once — plugin warm-up, the first Spotify match, the first
extractor call, the opening stagger — lands inside the interval before the
first completion.  Anchoring at batch start divides that one-off cost
across however few completions have happened, which on a real 18-track
batch opened the estimate at roughly double the truth and then visibly
collapsed as the window rolled past it.  How many opening completions
belong to that startup depends on how many jobs the batch actually runs at
once (``_peak_active``): one under the conservative gate, ``w`` in
parallel.  Those are skipped and whole waves are measured from there.

Three estimators were tried and rejected before this one:

* *remaining_bytes / aggregate_speed + modelled overhead*.  Divided
  remaining bytes by the smoothed active speed, then added one average
  YouTube cooldown per queued job plus one average start-stagger per
  unsubmitted job.  On a real 59-track batch it opened at ~22 minutes
  against a ~9 minute actual: the stagger term double-counted sleeps that
  overlap the gate wait entirely, and the byte term inflated because
  yt-dlp's pre-extraction ``total_bytes_estimate`` is much larger than the
  post-conversion file :meth:`complete` later writes back.
* *outstanding × median(per-job wall time)*.  A job's own wall time
  includes the time it sat blocked behind *other* jobs at the conservative
  gate, so with several workers in flight it absorbs multiple service
  cycles and multiplying by the outstanding count inflates by up to
  ~``max_workers``.
* *median of consecutive completion intervals*.  Degenerate under parallel
  bursts: three workers completing at t=10,10,10,20,20,20 give intervals
  10,0,0,10,0,0 whose median is **0**, collapsing the ETA to zero in a
  perfectly healthy batch.  Averaging per-interval *gaps* is the defect;
  dividing a span by a count is not susceptible to it.

Because a job only becomes terminal after it has published, ``cycle``
absorbs the YouTube cooldown, Spotify match resolution, gate starvation,
ffmpeg conversion, tagging, retry backoff and the cross-volume copy
automatically — none of it needs to be modelled per-phase.

Completion histories are also bounded independently by resolution source.
The cohort that dominates the remaining work (live, direct, cache, prefetched,
or unknown) prices the ETA once it has enough evidence. A mixed global history
is used only as an explicitly labeled, wider-uncertainty warm-up fallback after
that cohort has produced at least one observation; an opening cache burst can
therefore never set the long-run rate for live misses.

Until the batch has produced three counted completions the ETA is ``None``
and the UI shows "calculating…" rather than a number it cannot justify.
Two completions give a single interval, and a rate from one interval is a
coin flip — see ``_MIN_COMPLETIONS_FOR_ETA``.
The value is always labelled an estimate — see ``eta_is_estimate``.

Limitations are documented on :meth:`snapshot`.
"""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

_UNSET = object()

# How many recent counted completions the throughput window retains. Bounded so
# an early, unrepresentative rate (the opening burst, a slow first match) ages
# out instead of anchoring the estimate for the whole batch.
_THROUGHPUT_WINDOW = 10

# Minimum counted completions before the ETA is reported at all.
#
# Two is the smallest number that can express a rate, but a rate from a single
# interval is a coin flip: measured over randomised batches, the opening number
# missed by more than a quarter in a third of runs, which is what makes a footer
# quote seven minutes and then walk it down to four. A third completion gives
# two intervals and takes that to roughly one run in seven, for about fourteen
# seconds more of "calculating..." - most of the available improvement for the
# smallest wait. A fourth buys only another three points.
#
# Nothing cleverer than waiting helps here, which was checked rather than
# assumed. With two intervals a median and a trimmed mean are arithmetically
# identical to the mean, so robustness has nothing to work with. Shrinking the
# estimate toward a prior does tighten the spread - but only while the prior
# happens to match: against a batch cycling at 30s instead of 10s it dropped the
# share of runs landing within a quarter of the truth from 76% to 30%. That is
# the same mistake as the modelled cooldown this estimator replaced, so it is
# not used. Clamping to the cooldown as a physical floor is safe but gains
# about one point, which does not pay for the extra concept.
#
# In parallel mode this is not the binding constraint: the wave logic in
# _throughput_cycle_locked already requires two full waves.
_MIN_COMPLETIONS_FOR_ETA = 3

# (No stall threshold. An earlier revision only degraded the rate once the
# pipeline had been silent for 3x the measured cycle, which left the estimate
# provably constant between 1x and 3x — `eta` had already bottomed out at
# `(outstanding - 1) * cycle` and could not move again until the threshold
# tripped. With the heartbeat re-publishing that identical value twice a second
# the footer sat visibly frozen for up to two cycles. Degradation now begins the
# moment a cycle runs over; see _throughput_cycle_locked.)

# Half-life of the displayed aggregate speed, in seconds. Time-based rather than
# per-tick so responsiveness does not depend on yt-dlp's tick rate or on how
# many jobs happen to be running.
_SPEED_HALF_LIFE_S = 3.0

# Half-life, in seconds, over which the measured cycle settles toward a freshly
# derived value. Applied to the *rate*, not to the finished ETA, so discrete
# facts (a job cancelled, paused or completed) take effect immediately while the
# noisy measurement is damped. The rate is NOT forced downward — new evidence
# may honestly raise it — it simply may not teleport. 1.5 s lets a genuine
# correction land within a couple of seconds while absorbing jitter.
_ETA_SMOOTH_HALF_LIFE_S = 1.5

_RESOLUTION_SOURCES = frozenset({
    "live", "direct", "cache", "prefetched", "unknown",
})


@dataclass
class _CompletionHistory:
    """One bounded completion timeline with its own evicted anchor."""

    times: List[float] = field(default_factory=list)
    window_anchor: float = 0.0
    window_full: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Job state
# ──────────────────────────────────────────────────────────────────────────────

class JobState(Enum):
    QUEUED       = "queued"
    ACTIVE       = "active"
    COMPLETED    = "completed"
    PREEXISTING  = "preexisting"  # duplicate-skip: file already existed, no download ran
    FAILED       = "failed"
    PAUSED       = "paused"
    CANCELLED    = "cancelled"


# Terminal states no longer contribute a live speed and are "done" for the
# purpose of the outstanding-work denominator.
_TERMINAL = {JobState.COMPLETED, JobState.PREEXISTING, JobState.FAILED, JobState.CANCELLED}


def is_terminal_state(state: Optional[JobState]) -> bool:
    """Whether a job in ``state`` has already reached a state it never
    leaves — the public form of ``_TERMINAL`` for callers outside this
    module (e.g. deciding whether a job is still safe to pause)."""
    return state in _TERMINAL

# States that count as a full-size, fully-done job for progress-weighting
# purposes (see _progress_locked). PREEXISTING is a terminal success exactly
# like COMPLETED — the file was found already correct on disk, so no bytes
# ever needed to move — but it is tracked separately so the batch summary can
# report "N downloaded, M already existed" instead of collapsing the two.
_DONE_FOR_PROGRESS = {JobState.COMPLETED, JobState.PREEXISTING}


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
    # How the final URL became available. This is pipeline provenance, not a
    # quality judgement: direct/cache/prefetched/live all use the same matcher.
    resolve_source:    str             = "direct"

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
    completed:  int            # successes: real downloads + preexisting
    preexisting: int           # subset of `completed` that were duplicate-skips
    failed:     int
    paused:     int
    cancelled:  int

    progress:        float            # 0..1, weighted, monotonic within a batch
    byte_weighted:   bool             # True when ≥1 job had a known byte total
    speed_bps:       float            # smoothed sum of active-job speeds
    raw_speed_bps:   float            # instantaneous sum of active-job speeds
    eta_seconds:     Optional[float]  # whole-batch remaining time, or None
    # Always True whenever eta_seconds is not None: the estimate extrapolates
    # the rate the batch has achieved so far and can never be more than an
    # informed projection. (False only in the degenerate "nothing left to do"
    # case, where eta_seconds is None anyway.)
    eta_is_estimate: bool

    # Identifies the batch this snapshot describes. Regenerated by every
    # reset(), so a consumer holding "the batch I am currently showing" can
    # reject a snapshot belonging to a different one — e.g. a single-track
    # resume runs its own orchestrator with its own 1-job aggregator, and its
    # snapshot must never repaint the whole-batch footer as "0 of 1". Defaulted
    # so existing constructions keep working.
    batch_id:        str = ""
    # Honest display interval. A missing interval means either warm-up or a
    # current-speed projection; it is never presented as a guaranteed bound.
    eta_lower_seconds: Optional[float] = None
    eta_upper_seconds: Optional[float] = None
    eta_confidence: str = "warming"  # warming | current_speed | low | steady
    eta_source: str = "none"
    # Which completion history priced the ETA. Normally identical to
    # ``eta_source``; ``global`` is an explicit warm-up fallback only.
    eta_rate_source: str = "none"

    @property
    def finished(self) -> int:
        return self.completed + self.failed + self.cancelled

    @property
    def downloaded(self) -> int:
        """Subset of `completed` that were actual downloads (not duplicate-skips)."""
        return self.completed - self.preexisting

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
    speed_smoothing : Retained for API compatibility. ``1.0`` disables the
                      displayed speed's time-based decay entirely (tests that
                      assert exact speeds pass this); any lower value selects
                      the standard ``_SPEED_HALF_LIFE_S`` decay. The aggregate
                      speed is a *display* metric only — it no longer feeds the
                      ETA, so this knob cannot distort the estimate.
    time_fn         : Injectable clock (defaults to ``time.monotonic``) so the
                      throughput window is deterministic in tests.
    conservative_delay_range : (min, max) seconds the orchestrator's
                      YouTube-conservative-mode gate sleeps between two
                      serialized YouTube jobs (core.youtube_reliability.
                      CONSERVATIVE_DELAY_RANGE). Accepted so callers need not
                      change, and still recorded for diagnostics, but the ETA
                      no longer adds a modelled cooldown: the measured cycle
                      already contains every real cooldown the batch paid. See
                      the module docstring for why the modelled version was
                      removed.
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
        # Both delay ranges are recorded for diagnostics and API compatibility
        # only — nothing reads them any more. The ETA used to add one average
        # cooldown per queued serialized job and one average start-stagger per
        # unsubmitted job on top of a bytes/speed estimate; the stagger term
        # double-counted sleeps that overlap the gate wait, and neither shrank
        # as the delays were actually paid. Both delays now reach the estimate
        # by being inside the measured wall time between completions.
        self._conservative_delay_range = conservative_delay_range
        self._stagger_delay_range: Optional[Tuple[float, float]] = None
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobProgress] = {}
        self._smoothed_speed: float = 0.0
        self._speed_updated_at: Optional[float] = None
        self._progress_floor: float = 0.0
        self._floor_uses_estimates: bool = False
        self._batch_id: str = uuid.uuid4().hex
        # Throughput window. ``_batch_start`` anchors the very first span, so
        # two completions are enough to express a rate. ``_window_anchor`` moves
        # forward to whichever completion falls out of the window, keeping the
        # span a real elapsed measurement without ever touching per-interval
        # gaps (see _throughput_cycle_locked for why gaps are the wrong unit).
        self._batch_start: float = self._time()
        self._completion_times: List[float] = []
        self._completion_sources: List[str] = []
        self._window_anchor: float = self._batch_start
        self._window_full: bool = False
        self._completion_histories: Dict[str, _CompletionHistory] = {
            source: _CompletionHistory(window_anchor=self._batch_start)
            for source in _RESOLUTION_SOURCES
        }
        # Highest number of jobs seen transferring at once. This is the batch's
        # real concurrency, not its configured limit: the conservative YouTube
        # gate holds every job but one at the gate rather than in ACTIVE, so a
        # serialized batch measures 1 even with max_workers=3. It decides how
        # many opening completions count as the pipeline filling up rather than
        # as steady-state throughput - see _throughput_cycle_locked.
        self._peak_active: int = 0
        self._last_cycle: Optional[float] = None
        self._last_cycle_at: Optional[float] = None
        self._last_eta_source: Optional[str] = None
        self._last_rate_source: Optional[str] = None

    # ── Registration ──────────────────────────────────────────────────────────

    def reset(
        self,
        keys: Optional[list[str]] = None,
        *,
        conservative_delay_range=_UNSET,
        stagger_delay_range=_UNSET,
        batch_id: Optional[str] = None,
    ) -> None:
        """Start a fresh batch. Optionally pre-register queued job keys.

        ``batch_id`` labels every snapshot this batch emits. Pass the id the
        *caller* already holds — the UI mints one before it starts the worker,
        so it knows which batch it is showing before the first snapshot can
        arrive and never has to infer that from whatever signal turns up first.
        Omitted, a fresh id is generated (CLI, tests, direct use).

        Also re-anchors the throughput window at "now": a resumed batch
        measures its own rate rather than inheriting a stale one from before
        the pause.
        """
        with self._lock:
            if conservative_delay_range is not _UNSET:
                self._conservative_delay_range = conservative_delay_range
            if stagger_delay_range is not _UNSET:
                self._stagger_delay_range = stagger_delay_range
            self._jobs = {}
            self._smoothed_speed = 0.0
            self._speed_updated_at = None
            self._progress_floor = 0.0
            self._floor_uses_estimates = False
            self._batch_id = batch_id or uuid.uuid4().hex
            self._batch_start = self._time()
            self._completion_times = []
            self._completion_sources = []
            self._window_anchor = self._batch_start
            self._window_full = False
            self._completion_histories = {
                source: _CompletionHistory(window_anchor=self._batch_start)
                for source in _RESOLUTION_SOURCES
            }
            self._peak_active = 0
            self._last_cycle = None
            self._last_cycle_at = None
            self._last_eta_source = None
            self._last_rate_source = None
            if keys:
                for k in keys:
                    self._jobs[k] = JobProgress(key=k, state=JobState.QUEUED)

    @property
    def batch_id(self) -> str:
        """Identity of the batch currently being aggregated (see BatchSnapshot)."""
        return self._batch_id

    def register(self, key: str) -> None:
        """Ensure a job exists in the QUEUED state."""
        with self._lock:
            self._jobs.setdefault(key, JobProgress(key=key, state=JobState.QUEUED))

    def job_state(self, key: str) -> Optional[JobState]:
        """Thread-safe point-in-time read of a single job's state.

        This reflects the orchestrator's own bookkeeping, updated
        synchronously on the worker thread the instant a job transitions —
        unlike a UI card's status label, which is only updated once a
        Qt-queued signal is dispatched and can lag the real state by one
        event-loop tick. Callers that need to know "has this job already
        reached a terminal state" *before* acting on it (e.g. deciding
        whether it is still safe to pause/resume-snapshot) must use this,
        not the card.
        """
        with self._lock:
            job = self._jobs.get(key)
            return job.state if job is not None else None

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
        it.

        Diagnostic only as of the throughput ETA. The estimate used to add one
        modelled average cooldown per job flagged here; it now measures the
        cooldowns the batch actually paid, because they land inside the wall
        time between completions. The flag is kept so callers need not change
        and so the serialized/parallel distinction stays visible for logging.
        """
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key, state=JobState.QUEUED))
            job.serialized = True

    def mark_resolution_source(self, key: str, source: str) -> None:
        """Attach bounded source-cohort provenance to one job."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key, state=JobState.QUEUED))
            job.resolve_source = source if source in _RESOLUTION_SOURCES else "unknown"

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

    def _record_completion_locked(self, job: JobProgress) -> None:
        """Add one tick to the throughput window (see the module docstring).

        Only transitions that consumed a real pipeline cycle belong here.
        PREEXISTING is excluded because duplicate-skips are resolved in a tight
        loop before any download starts — twenty of them would land on the same
        instant and drive the measured rate to nonsense. CANCELLED is excluded
        because a mass-cancel produces the same degenerate burst.
        """
        completed_at = self._time()
        source = job.resolve_source or "unknown"
        if source not in _RESOLUTION_SOURCES:
            source = "unknown"
        self._completion_times.append(completed_at)
        self._completion_sources.append(source)
        while len(self._completion_times) > _THROUGHPUT_WINDOW:
            self._window_full = True
            # The evicted completion becomes the window's new anchor, so the
            # span stays a real elapsed measurement rather than silently losing
            # the time that produced the completions still in the window.
            self._window_anchor = self._completion_times.pop(0)
            self._completion_sources.pop(0)

        history = self._completion_histories[source]
        history.times.append(completed_at)
        while len(history.times) > _THROUGHPUT_WINDOW:
            history.window_full = True
            history.window_anchor = history.times.pop(0)

    def complete(self, key: str, final_bytes: Optional[int] = None) -> None:
        """Mark a job finished successfully; it counts at its full size."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key))
            if job.state in (JobState.FAILED, JobState.CANCELLED):
                return
            already_terminal = job.state in _TERMINAL
            job.submitted = True
            job.state = JobState.COMPLETED
            job.fraction = 1.0
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.ended_at = self._time()
            if not already_terminal:
                self._record_completion_locked(job)
            if final_bytes is not None and final_bytes > 0:
                job.downloaded_bytes = int(final_bytes)
                job.total_bytes = int(final_bytes)
                job.total_bytes_is_estimate = False
            elif job.known_size:
                job.downloaded_bytes = int(job.total_bytes)
            self._recompute_speed_locked()

    def mark_preexisting(self, key: str) -> None:
        """Mark a job as a terminal success without a download: a duplicate-skip
        found the target file already correct on disk. Counts as `completed`
        (see BatchSnapshot.completed) but is tracked separately in
        `BatchSnapshot.preexisting` so the batch summary can distinguish
        "downloaded" from "already existed"."""
        with self._lock:
            job = self._jobs.setdefault(key, JobProgress(key=key))
            if job.state in (JobState.FAILED, JobState.CANCELLED):
                return
            job.submitted = True
            job.state = JobState.PREEXISTING
            job.fraction = 1.0
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.ended_at = self._time()
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
            # A failure that ran the pipeline — resolve, gate wait, download
            # attempt, retry backoff — is real evidence of the batch's
            # throughput and belongs in the window. A cancellation is not, and
            # arrives in bursts besides.
            #
            # `submitted` is the discriminator, and it matters: a job whose
            # private workspace directory could not be created is failed by the
            # orchestrator up front, before registration, before the pool ever
            # sees it. Those failures land together on essentially the same
            # instant, so two of them would satisfy the warm-up rule with a
            # near-zero span and collapse the ETA for every healthy job behind
            # them. They consumed no pipeline time and must not be measured as
            # though they had.
            if state == JobState.FAILED and job.submitted:
                self._record_completion_locked(job)
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
        * ``eta_seconds`` extrapolates the rate the batch has achieved *so
          far*. It therefore assumes the rest of the batch behaves like the
          recent past — a run of unusually large files, a slow stretch of
          Spotify matches, or a throttling change will show up only once it
          starts affecting real completions. It is always labelled an
          estimate (``eta_is_estimate``).
        * While fewer than two jobs have completed there is nothing measured
          to extrapolate from, so ``eta_seconds`` is ``None`` and the UI
          shows "calculating…" rather than a number derived from one sample.
        * In the final drain — when fewer jobs remain than the batch was
          running in parallel — the remaining jobs finish concurrently rather
          than at the measured serial rate, so the estimate runs mildly
          pessimistic over the last few seconds. The floor at the slowest
          in-flight job's own remaining transfer time keeps a single large
          file from being under-called; the residual over-estimate is
          accepted in preference to maintaining a second model for the tail.
        * ``progress`` imputes a size for jobs whose total isn't known yet,
          preferring real totals over yt-dlp's pre-extraction estimates, so
          it sharpens as the batch reveals real sizes.
        """
        with self._lock:
            return self._snapshot_locked()

    # ── Internal calculation ──────────────────────────────────────────────────

    def _recompute_speed_locked(self) -> None:
        """Decay the displayed aggregate speed toward the instantaneous sum.

        Time-based, not per-tick: the previous version applied one EMA step per
        yt-dlp callback, so how quickly the footer's speed responded depended on
        the tick rate and on how many jobs happened to be running. It also
        snapped straight to zero the moment the last active job finished, which
        made the number flicker to 0 on every completion and throughout every
        conservative-mode cooldown. Decaying through those gaps reads as a
        settling number rather than a broken one.

        This is a display metric only — the ETA is derived from measured
        throughput and does not consult it.
        """
        active = sum(1 for j in self._jobs.values() if j.state == JobState.ACTIVE)
        if active > self._peak_active:
            self._peak_active = active
        raw = sum(j.speed_bps for j in self._jobs.values() if j.state == JobState.ACTIVE)
        now = self._time()
        # speed_smoothing=1.0 is the documented "no smoothing" setting used by
        # tests that assert exact speeds.
        if self._alpha >= 1.0:
            self._smoothed_speed = raw
            self._speed_updated_at = now
            return
        last = self._speed_updated_at
        self._speed_updated_at = now
        if last is None:
            self._smoothed_speed = raw
            return
        dt = max(0.0, now - last)
        weight = 0.5 ** (dt / _SPEED_HALF_LIFE_S) if dt > 0 else 1.0
        self._smoothed_speed = weight * self._smoothed_speed + (1.0 - weight) * raw

    def _raw_active_speed_locked(self) -> float:
        return sum(j.speed_bps for j in self._jobs.values() if j.state == JobState.ACTIVE)

    def _mean_known_size_locked(self) -> Optional[float]:
        """Average size used to impute jobs whose total isn't known yet.

        Real totals are preferred over yt-dlp's ``total_bytes_estimate``.  The
        estimate describes the *pre-extraction* stream and is routinely several
        times the audio file that finally lands on disk, which :meth:`complete`
        writes back as the job's real total.  Averaging the two together meant
        the imputed size for every not-yet-started job started inflated and
        shrank as the batch progressed, so the denominator shrank too and the
        bar accelerated toward the end instead of moving evenly.  Estimates are
        still used while no real total exists at all — some size beats none.
        """
        real = [
            float(j.total_bytes)
            for j in self._jobs.values()
            if j.known_size and not j.total_bytes_is_estimate
        ]
        if real:
            return sum(real) / len(real)
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
                1.0 if j.state in _DONE_FOR_PROGRESS else j.fraction
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
                    if j.state in _DONE_FOR_PROGRESS:
                        done = size
                else:
                    size = mean_known
                    uses_estimates = True
                    if j.state in _DONE_FOR_PROGRESS:
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

    def _eta_source_locked(self, outstanding: List[JobProgress]) -> str:
        """Choose a representative remaining-work cohort with hysteresis."""
        counts: Dict[str, int] = {}
        for job in outstanding:
            source = job.resolve_source or "unknown"
            if source not in _RESOLUTION_SOURCES:
                source = "unknown"
            counts[source] = counts.get(source, 0) + 1
        if not counts:
            return "none"
        priority = {
            "live": 4, "direct": 3, "unknown": 2, "cache": 1, "prefetched": 0,
        }
        leader = max(counts, key=lambda item: (counts[item], priority.get(item, 0)))
        previous = self._last_eta_source
        # Do not oscillate on every completion when two cohorts are nearly tied.
        if previous in counts and counts[previous] >= counts[leader] - 1:
            return previous
        return leader

    def _legacy_mixed_throughput_cycle_locked(
        self, source: Optional[str] = None,
    ) -> Optional[float]:
        """
        Measured wall-seconds per completion, or ``None`` while warming up.

        A **time span divided by a completion count** — never an average of the
        gaps between consecutive completions. That distinction is the whole
        point: parallel workers finish in bursts, so consecutive-gap statistics
        degenerate (three workers completing at t=10,10,10,20,20,20 produce
        gaps of 10,0,0,10,0,0, whose median is zero). Dividing the 20-second
        span by the 6 completions in it gives 3.33 s — the batch's real
        wall-time cost per track, parallelism already baked in.
        """
        times = self._completion_times
        if len(times) < _MIN_COMPLETIONS_FOR_ETA:
            return None

        now = self._time()
        t_last = times[-1]

        all_sources = set(self._completion_sources)
        all_sources.update(
            job.resolve_source or "direct"
            for job in self._jobs.values()
            if job.state not in _TERMINAL and job.state != JobState.PAUSED
        )

        if source and len(all_sources) > 1 and not self._window_full:
            # Ignore an opening cache/prefetch burst when live misses remain,
            # but count every completion after the selected cohort reaches the
            # pipeline. Those other completions still reduce real outstanding
            # work and therefore belong in aggregate throughput.
            source_indices = [
                index for index, value in enumerate(self._completion_sources)
                if value == source
            ]
            if len(source_indices) < _MIN_COMPLETIONS_FOR_ETA:
                return None
            anchor_index = source_indices[0]
            n = len(times) - anchor_index - 1
            if n < 2:
                return None
            anchor, span_end = times[anchor_index], t_last
        elif self._window_full:
            # Past the first window, the anchor is itself a completion that
            # aged out, so the batch's opening costs are already behind it.
            anchor, n, span_end = self._window_anchor, len(times), t_last
        else:
            # Still measuring the batch's first completions, which is where a
            # naive anchor at batch start goes wrong: everything paid once at
            # the beginning — plugin warm-up, the first Spotify match, the
            # first extractor call, the opening stagger — lands inside the very
            # first interval and then gets divided across only two or three
            # completions. On a real 18-track batch that opened the estimate at
            # roughly double the truth, and it only healed later as the window
            # rolled past the startup.
            #
            # So anchor on an observed completion instead of on batch start.
            # Which one depends on how many jobs the batch actually runs at
            # once: with the conservative gate serializing YouTube there is one
            # in flight, so the first completion ends the startup; with w
            # running in parallel the first w completions are the startup wave
            # and the pipeline is only full after them. Counting whole waves
            # from there keeps the measurement honest either way — a partial
            # wave would charge one worker's whole service time to a single
            # completion and treble the estimate.
            w = max(1, self._peak_active)
            after = len(times) - w
            waves = after // w
            if waves < 1:
                return None
            n = waves * w
            anchor = times[w - 1]
            span_end = times[w + n - 1]

        span = span_end - anchor
        if span <= 0.0:
            # Every retained completion landed on the same instant — a burst
            # finer than the clock's resolution, or an injected test clock that
            # never advanced. The span up to now is still a real measurement.
            span = now - anchor
        if span <= 0.0:
            return None

        cycle = span / n
        if cycle <= 0.0:
            return None

        stall = now - t_last
        if stall > cycle:
            # This cycle has already run longer than the measured average, so
            # the batch is behind. Fold the elapsed dead time into the rate:
            # extend the measured span all the way to now while the completion
            # count stays put, which is the same span-over-count estimator
            # applied to what is actually known right now.
            #
            # Beginning at `stall > cycle` rather than at some multiple of it is
            # what keeps the estimate alive. Past that point the
            # `- min(stall, cycle)` term in _eta_locked has already saturated,
            # so without this the value would be pinned at exactly
            # `(outstanding - 1) * cycle` — constant, and republished unchanged
            # by every heartbeat, which reads as a broken footer rather than an
            # honest one. Below the threshold the raw span is used untouched, so
            # a healthy batch never sees its rate inflated mid-cycle.
            cycle = max(cycle, (now - anchor) / n)
        return cycle

    def _history_cycle_locked(
        self,
        history: _CompletionHistory,
    ) -> tuple[Optional[float], Optional[float]]:
        """Return ``(cycle, last_completion)`` for exactly one timeline."""
        times = history.times
        if len(times) < _MIN_COMPLETIONS_FOR_ETA:
            return None, None

        now = self._time()
        t_last = times[-1]
        if history.window_full:
            anchor, n, span_end = history.window_anchor, len(times), t_last
        else:
            # The first observed wave fills the pipeline. Measure only whole
            # steady-state waves after it, independently for every cohort.
            w = max(1, self._peak_active)
            after = len(times) - w
            waves = after // w
            if waves < 1:
                return None, None
            n = waves * w
            anchor = times[w - 1]
            span_end = times[w + n - 1]

        span = span_end - anchor
        if span <= 0.0:
            span = now - anchor
        if span <= 0.0:
            return None, None
        cycle = span / n
        if cycle <= 0.0:
            return None, None

        stall = now - t_last
        if stall > cycle:
            cycle = max(cycle, (now - anchor) / n)
        return cycle, t_last

    def _throughput_measurement_locked(
        self,
        source: Optional[str] = None,
    ) -> tuple[Optional[float], Optional[float], str, int, List[float]]:
        """Measure one cohort, using global history only as explicit fallback."""
        if source and source in _RESOLUTION_SOURCES:
            history = self._completion_histories[source]
            cycle, last = self._history_cycle_locked(history)
            if cycle is not None:
                return cycle, last, source, len(history.times), history.times
            # A faster, already-finished cohort is not evidence about work that
            # has not produced even one observation.  Once the selected cohort
            # emits its first completion, the mixed global rate may keep the
            # display moving while the cohort-specific window warms, clearly
            # labeled as low-confidence global fallback.
            if not history.times:
                return None, None, "none", 0, []

        global_history = _CompletionHistory(
            times=self._completion_times,
            window_anchor=self._window_anchor,
            window_full=self._window_full,
        )
        cycle, last = self._history_cycle_locked(global_history)
        return cycle, last, "global", len(self._completion_times), self._completion_times

    def _throughput_cycle_locked(self, source: Optional[str] = None) -> Optional[float]:
        """Compatibility view returning only the selected measured cycle."""
        cycle, _last, _basis, _count, _times = self._throughput_measurement_locked(source)
        return cycle

    def _slowest_active_byte_time_locked(self) -> Optional[float]:
        """
        Longest time any single in-flight job still needs at its own speed.

        The throughput model assumes the pipeline keeps emitting completions at
        the measured rate, which stops holding in the final drain: when fewer
        jobs remain than the batch was running in parallel, ``outstanding ×
        cycle`` can under-state one large file that is still transferring. The
        batch cannot finish before its slowest in-flight job does, so this is a
        sound lower bound at any outstanding count, and it is applied as a floor
        (``max``) — it can only raise the estimate, never pull it down to a
        single track's remaining time.

        Note this is derived from bytes and speed. yt-dlp's own per-track
        ``JobProgress.eta_seconds`` is never read here or anywhere else in the
        batch estimate; it exists solely for the track card.
        """
        best: Optional[float] = None
        for j in self._jobs.values():
            if j.state != JobState.ACTIVE or j.speed_bps <= 0 or not j.known_size:
                continue
            remaining = max(0.0, float(j.total_bytes) - j.downloaded_bytes)
            secs = remaining / j.speed_bps
            if best is None or secs > best:
                best = secs
        return best

    def _smooth_cycle_locked(self, measured: float, now: float) -> float:
        """
        Damp the *measured rate* so the reported ETA settles instead of jumping.

        Smoothing is applied here rather than to the finished ETA on purpose.
        The cycle is the noisy term — it is re-derived from a sliding window and
        wobbles as fast and slow tracks enter and leave it. Everything else in
        the formula is exact: how many jobs are left, and how long since the
        last completion. Smoothing the finished number would delay those facts
        too, so cancelling a job or pausing one would leave the footer quoting
        work the batch is no longer going to do.

        Deliberately **not** monotonic. Fresh evidence — a run of larger files,
        a slower stretch of matches, a stall — may honestly raise the estimate,
        and forcing it downward would be a lie. What this suppresses is jitter,
        not honest revision.
        """
        prev, prev_at = self._last_cycle, self._last_cycle_at
        self._last_cycle_at = now
        if prev is None or prev_at is None:
            self._last_cycle = measured
            return measured

        dt = max(0.0, now - prev_at)
        if dt <= 0.0:
            # No time has passed, so there is no new evidence to weigh; the
            # previously settled rate still stands.
            return prev
        weight = 0.5 ** (dt / _ETA_SMOOTH_HALF_LIFE_S)
        value = max(0.0, weight * prev + (1.0 - weight) * measured)
        self._last_cycle = value
        return value

    def _eta_details_locked(
        self,
    ) -> tuple[
        Optional[float], bool, Optional[float], Optional[float], str, str, str
    ]:
        """
        Whole-batch remaining time. Returns (seconds, is_estimate).

        ``outstanding × measured_cycle − elapsed_into_the_current_cycle``, with
        a floor at the slowest in-flight job's own remaining transfer time. See
        the module docstring for the derivation and for the three estimators
        this replaced.

        The subtraction keeps the countdown continuous across a completion:
        just before one lands, ``stall ≈ cycle`` so the value already reads
        ``(outstanding − 1) × cycle``; the instant it lands, ``outstanding``
        drops by one and ``stall`` resets to zero, giving the same number.
        Without it, anchoring the window at batch start would bias every
        estimate high by half a cycle.
        """
        jobs = list(self._jobs.values())
        # PAUSED jobs are not coming back on their own — a resume starts a new
        # batch — so they are not work this batch still has to get through.
        outstanding = [
            j for j in jobs
            if j.state not in _TERMINAL and j.state != JobState.PAUSED
        ]
        if not outstanding:
            self._last_cycle = None
            self._last_cycle_at = None
            self._last_eta_source = None
            self._last_rate_source = None
            return None, False, None, None, "warming", "none", "none"

        source = self._eta_source_locked(outstanding)
        measured, measured_last, rate_source, sample_count, measurement_times = (
            self._throughput_measurement_locked(source)
        )
        if measured is None:
            # Small batches can finish before a throughput window exists. Byte
            # time at the current speed is useful, but neither the byte total
            # nor the future speed is guaranteed. Present it only as a
            # current-speed projection, never as a lower bound.
            floor = self._slowest_active_byte_time_locked()
            if len(jobs) <= 3 and floor is not None:
                return floor, True, None, None, "current_speed", source, "bytes"
            if len(jobs) <= 3 and self._completion_times:
                observed_cycle = max(
                    0.1,
                    (self._completion_times[-1] - self._batch_start)
                    / len(self._completion_times),
                )
                center = len(outstanding) * observed_cycle
                return center, True, center * 0.5, center * 2.0, "low", source, "global"
            return None, True, None, None, "warming", source, "none"

        now = self._time()
        if (
            self._last_eta_source not in (None, source)
            or self._last_rate_source not in (None, rate_source)
        ):
            # Hysteresis prevents casual switching; when a real cohort change
            # does happen, do not blend incompatible warm/live rates.
            self._last_cycle = None
            self._last_cycle_at = None
        self._last_eta_source = source
        self._last_rate_source = rate_source
        cycle = self._smooth_cycle_locked(measured, now)
        stall = now - (measured_last if measured_last is not None else now)
        eta = max(0.0, len(outstanding) * cycle - min(stall, cycle))

        floor = self._slowest_active_byte_time_locked()
        if floor is not None:
            eta = max(eta, floor)

        relative = max(0.15, min(0.75, 1.0 / math.sqrt(max(2, sample_count - 1))))
        if sample_count < 7:
            # With only a handful of completions there is not enough evidence
            # to rule out a late retry/post-processing cohort. A deliberately
            # wide interval is more honest than a narrow but fragile one.
            relative = max(relative, 1.0)
        if len(set(self._completion_sources)) > 1:
            relative = min(1.10, relative + 0.10)
        if rate_source == "global" and source != "global":
            # The selected cohort is still warming. Global evidence keeps the
            # display moving but must carry visibly wider uncertainty.
            relative = max(relative, 0.75)
        lower = max(floor or 0.0, eta * (1.0 - relative))
        upper = max(lower, eta * (1.0 + relative))
        if len(measurement_times) >= 2:
            recent = measurement_times[-4:]
            largest_recent_gap = max(
                (right - left for left, right in zip(recent, recent[1:])),
                default=0.0,
            )
            upper = max(upper, len(outstanding) * largest_recent_gap * 1.25)
        confidence = "steady" if sample_count >= 7 and relative <= 0.35 else "low"
        return eta, True, lower, upper, confidence, source, rate_source

    def _eta_locked(self) -> tuple[Optional[float], bool]:
        """Compatibility view for callers that need only center + estimate flag."""
        eta, estimate, _lower, _upper, _confidence, _source, _rate = (
            self._eta_details_locked()
        )
        return eta, estimate

    def _snapshot_locked(self) -> BatchSnapshot:
        # Advance the speed decay on read, not only on mutation. The decay is a
        # function of elapsed time, but the mutators are the only things that
        # used to call it — and during a conservative-mode cooldown there are no
        # mutators for five to ten seconds. The footer would hold whatever value
        # the last completion left behind and report a healthy transfer rate
        # while nothing at all was transferring. Reading advances it, so an idle
        # pipeline visibly settles to zero.
        self._recompute_speed_locked()

        counts = {s: 0 for s in JobState}
        for j in self._jobs.values():
            counts[j.state] += 1

        progress, byte_weighted = self._progress_locked()
        raw_speed = self._raw_active_speed_locked()
        (
            eta, eta_estimate, eta_lower, eta_upper, eta_confidence, eta_source,
            eta_rate_source,
        ) = self._eta_details_locked()

        return BatchSnapshot(
            total=len(self._jobs),
            queued=counts[JobState.QUEUED],
            active=counts[JobState.ACTIVE],
            completed=counts[JobState.COMPLETED] + counts[JobState.PREEXISTING],
            preexisting=counts[JobState.PREEXISTING],
            failed=counts[JobState.FAILED],
            paused=counts[JobState.PAUSED],
            cancelled=counts[JobState.CANCELLED],
            progress=progress,
            byte_weighted=byte_weighted,
            speed_bps=self._smoothed_speed,
            raw_speed_bps=raw_speed,
            eta_seconds=eta,
            eta_is_estimate=eta_estimate,
            batch_id=self._batch_id,
            eta_lower_seconds=eta_lower,
            eta_upper_seconds=eta_upper,
            eta_confidence=eta_confidence,
            eta_source=eta_source,
            eta_rate_source=eta_rate_source,
        )
