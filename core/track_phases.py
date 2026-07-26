"""
core/track_phases.py  –  What a single track is actually doing, and how far in
==============================================================================
A track card used to show the byte transfer and nothing else. On a real batch
that is about one second out of every nine or ten: the card sat at 0% through
the Spotify match and the wait at the conservative YouTube gate, raced to 95%
in roughly a second, then sat at 95% through ffmpeg, tagging, MusicBrainz,
ReplayGain and publish. Users read the two frozen stretches as a hung download,
which is a reasonable thing to conclude when the only thing on screen is a bar
that is not moving.

Nothing was missing from the *measurements* — the orchestrator already timed
every one of those phases for its end-of-batch log. They simply never reached
the UI. This module turns them into a position and a remaining time.

How a phase's own progress is known
-----------------------------------
Only the download phase has an intrinsic progress signal (bytes). Every other
phase is opaque: there is no "40% matched". So for those, progress within the
phase is *elapsed time against how long that phase usually takes on this batch*,
which is exactly the thing the orchestrator has been measuring all along. That
is what stops the bar freezing — an opaque phase still visibly advances, and a
phase running long simply approaches its end without ever claiming to finish.

Weights are learned, not assumed
--------------------------------
Phase weights come from the tracks this batch has already completed, so a batch
of long files, a slow connection or a cold match cache all shape the bar
correctly. ``_DEFAULT_WEIGHTS`` exists only to have something to draw before the
first track finishes, and is replaced by measurement the moment it can be.

The same caution as the batch ETA applies, for the same reason: a fixed constant
that assumes what work "should" cost is exactly what produced the old
22-minutes-for-a-9-minute-batch estimate. The defaults here are weaker than that
in two ways — they only shape a bar's pacing rather than multiply out into a
quoted duration, and :meth:`TrackPhaseModel.remaining` returns ``None`` until at
least one real track has been measured, so no per-track time is ever quoted from
a guess.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Dict, List, Optional


class TrackPhase(Enum):
    """Ordered stages every track passes through.

    ``QUEUED`` and the terminal states are not weighted segments — a queued
    track has not started and a finished one has no remaining work.
    """

    QUEUED      = "queued"
    MATCHING    = "matching"      # Spotify two-stage resolve -> a YouTube URL
    WAITING     = "waiting"       # blocked on the conservative YouTube gate
    STARTING    = "starting"      # extract_info, up to the first byte
    DOWNLOADING = "downloading"   # bytes actually moving
    PROCESSING  = "processing"    # ffmpeg, tagging, artwork, publish


# The segments that carry weight on the bar, in the order a track meets them.
PHASE_ORDER: List[TrackPhase] = [
    TrackPhase.MATCHING,
    TrackPhase.WAITING,
    TrackPhase.STARTING,
    TrackPhase.DOWNLOADING,
    TrackPhase.PROCESSING,
]

# Seconds. Used only to have something to draw before the first track of a batch
# finishes; every one of these is replaced by a measured average as soon as one
# exists. Rough shape of a cached-match YouTube track under the conservative
# gate. WAITING defaults to zero because a batch without the gate never waits,
# and a batch with it learns the real figure from its first track.
_DEFAULT_WEIGHTS: Dict[TrackPhase, float] = {
    TrackPhase.MATCHING:    0.5,
    TrackPhase.WAITING:     0.0,
    TrackPhase.STARTING:    1.5,
    TrackPhase.DOWNLOADING: 2.0,
    TrackPhase.PROCESSING:  3.0,
}

# A phase that runs past its learned average must still leave room to finish, or
# the bar would sit at exactly 100% while work continued — the same lie as
# sitting at 95%. Progress within a phase asymptotes toward this instead.
_WITHIN_PHASE_CEILING = 0.97

# How many completed tracks before per-track times are quoted at all. One real
# track is enough to stop guessing; see the module docstring.
_MIN_TRACKS_FOR_TIME = 1


class TrackPhaseModel:
    """Per-batch phase weights, learned from the tracks that have finished.

    Thread-safe: the orchestrator's pool threads record observations while the
    UI thread reads positions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: Dict[TrackPhase, float] = {p: 0.0 for p in PHASE_ORDER}
        self._counts: Dict[TrackPhase, int] = {p: 0 for p in PHASE_ORDER}
        self._tracks_measured = 0

    # ── Learning ──────────────────────────────────────────────────────────────

    def observe(self, phase: TrackPhase, seconds: float) -> None:
        """Record how long one track spent in one phase."""
        if phase not in self._totals or seconds < 0:
            return
        with self._lock:
            self._totals[phase] += float(seconds)
            self._counts[phase] += 1

    def note_track_measured(self) -> None:
        """Record that a track has been observed end to end."""
        with self._lock:
            self._tracks_measured += 1

    def reset(self) -> None:
        with self._lock:
            self._totals = {p: 0.0 for p in PHASE_ORDER}
            self._counts = {p: 0 for p in PHASE_ORDER}
            self._tracks_measured = 0

    # ── Weights ───────────────────────────────────────────────────────────────

    def _weight_locked(self, phase: TrackPhase) -> float:
        count = self._counts.get(phase, 0)
        if count:
            return self._totals[phase] / count
        return _DEFAULT_WEIGHTS.get(phase, 0.0)

    def weights(self) -> Dict[TrackPhase, float]:
        with self._lock:
            return {p: self._weight_locked(p) for p in PHASE_ORDER}

    # ── Position and remaining time ───────────────────────────────────────────

    @staticmethod
    def _within(phase: TrackPhase, elapsed: float, weight: float,
                byte_fraction: Optional[float]) -> float:
        """How far through its own phase a track is, in 0..1.

        The download phase knows this exactly. The others are opaque, so their
        progress is elapsed time against the learned average — capped short of
        1.0 so a phase running long still has somewhere to go.
        """
        if phase == TrackPhase.DOWNLOADING and byte_fraction is not None:
            return max(0.0, min(1.0, byte_fraction))
        if weight <= 0:
            return 1.0
        return min(_WITHIN_PHASE_CEILING, max(0.0, elapsed / weight))

    def position(
        self,
        phase: TrackPhase,
        elapsed_in_phase: float,
        byte_fraction: Optional[float] = None,
    ) -> float:
        """Overall progress of one track, 0..1, across every phase."""
        if phase == TrackPhase.QUEUED:
            return 0.0
        with self._lock:
            weights = {p: self._weight_locked(p) for p in PHASE_ORDER}
        total = sum(weights.values())
        if total <= 0:
            return 0.0
        if phase not in PHASE_ORDER:
            return 1.0

        done = 0.0
        for p in PHASE_ORDER:
            if p == phase:
                done += weights[p] * self._within(
                    p, elapsed_in_phase, weights[p], byte_fraction
                )
                break
            done += weights[p]
        return max(0.0, min(1.0, done / total))

    def remaining(
        self,
        phase: TrackPhase,
        elapsed_in_phase: float,
        byte_fraction: Optional[float] = None,
    ) -> Optional[float]:
        """Seconds left for this track across every phase, or None while the
        batch has nothing measured to base that on."""
        with self._lock:
            if self._tracks_measured < _MIN_TRACKS_FOR_TIME:
                return None
            weights = {p: self._weight_locked(p) for p in PHASE_ORDER}
        if phase == TrackPhase.QUEUED:
            return sum(weights.values())
        if phase not in PHASE_ORDER:
            return 0.0

        seen = False
        left = 0.0
        for p in PHASE_ORDER:
            if p == phase:
                seen = True
                within = self._within(
                    p, elapsed_in_phase, weights[p], byte_fraction
                )
                left += weights[p] * (1.0 - within)
                continue
            if seen:
                left += weights[p]
        return max(0.0, left)
