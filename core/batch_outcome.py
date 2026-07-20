"""
core/batch_outcome.py  –  Explicit batch termination reasons
=============================================================
The Downloads page has to tell five different endings apart, and the old
code collapsed all of them into a single ``_was_cancelled = True`` boolean.
That made a user *pause* look identical to a *fatal error*, and a *fatal
error* look like a deliberate *user cancellation*.

``BatchOutcome`` is the single vocabulary every layer (orchestrator →
worker → controller → status bar) uses to describe how a batch ended.
Zero GUI imports — the UI maps each value to localized wording.
"""

from __future__ import annotations

from enum import Enum


class BatchOutcome(Enum):
    """How a download batch finished."""

    COMPLETED             = "completed"              # every job succeeded
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # finished, some jobs failed
    PAUSED_BY_USER        = "paused_by_user"         # user paused; resumable
    CANCELLED_BY_USER     = "cancelled_by_user"      # user cancelled; stop
    STOPPED_BY_FATAL_ERROR = "stopped_by_fatal_error"  # a fatal condition halted it

    @property
    def is_success(self) -> bool:
        return self in (BatchOutcome.COMPLETED, BatchOutcome.COMPLETED_WITH_ERRORS)

    @property
    def user_initiated(self) -> bool:
        return self in (BatchOutcome.PAUSED_BY_USER, BatchOutcome.CANCELLED_BY_USER)
