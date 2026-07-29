"""
core/retry_policy.py  –  Retry logic for transient download failures
=====================================================================
Provides a simple retry-with-backoff wrapper and classifies which errors
are worth retrying.

Integration
-----------
Used by DownloadOrchestrator._download_one() to wrap engine.download()
calls.  When a retriable error occurs, the job is retried up to
``max_retries`` times with exponential backoff (1s, 2s, 4s, …).

Zero GUI imports.
"""

from __future__ import annotations

import logging
import re
import time
import threading
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Retriable error detection
# ──────────────────────────────────────────────────────────────────────────────

# Patterns that indicate transient / retriable failures
_RETRIABLE_PATTERNS = [
    re.compile(r"429|too many requests|rate.?limit", re.I),
    re.compile(r"503|service unavailable", re.I),
    re.compile(r"502|bad gateway", re.I),
    re.compile(r"500|internal server error", re.I),
    re.compile(r"connection reset|connection aborted|connection closed", re.I),
    re.compile(r"timed?\s*out|socket.?timeout|read timeout", re.I),
    re.compile(r"temporary failure|temporary error", re.I),
    re.compile(r"network is unreachable|unable to connect", re.I),
    re.compile(r"name.*resolution.*fail|dns.*fail|getaddrinfo", re.I),
    re.compile(r"incomplete read|chunked.?encoding|empty.*response", re.I),
    re.compile(r"ssl.*error|certificate.*verify.*failed", re.I),
    # Windows file-lock errors (winerror 32 SHARING_VIOLATION and similar) —
    # another process (AV scanner, Explorer preview) has the .part file
    # open momentarily; worth a short retry, unlike a real permission error.
    re.compile(r"used by another process|sharing violation", re.I),
]

# Patterns that indicate permanent failures — never retry
_PERMANENT_PATTERNS = [
    re.compile(r"private video|video unavailable|has been removed", re.I),
    re.compile(r"this video is not available", re.I),
    re.compile(r"sign in to confirm.*not a bot|confirm you.re not a bot", re.I),
    re.compile(r"age.?gated|login required", re.I),
    re.compile(r"not available in your country|geo.?block", re.I),
    re.compile(r"copyright|dmca|taken down", re.I),
    re.compile(r"permission denied|access denied", re.I),
    re.compile(r"members?.only|membership required", re.I),
    # PO Token / cookies / 403 need a config or auth fix, not a blind
    # retry. 403 is intentionally distinct from 429 above (429 is a
    # transient rate-limit worth backing off; 403 means the request was
    # refused outright and retrying changes nothing).
    re.compile(r"po[ _]?token", re.I),
    re.compile(r"cookies?.*(no longer valid|expired|invalid)", re.I),
    re.compile(r"\b403\b|forbidden", re.I),
]

_TEMPORARY_MEDIA_403_RE = re.compile(
    r"unable to download (?:video )?data[^\n]*\b403\b|"
    r"\b403\b[^\n]*unable to download (?:video )?data",
    re.I,
)


def is_temporary_media_403(error_message: str) -> bool:
    """True only for a transfer-stage 403 fixed by fresh extraction data."""
    return bool(_TEMPORARY_MEDIA_403_RE.search(error_message or ""))


def is_retriable(error_message: str) -> bool:
    """
    Determine if an error message indicates a transient failure
    that is worth retrying.
    """
    # Check permanent patterns first — they take priority
    if is_temporary_media_403(error_message):
        return True
    for pat in _PERMANENT_PATTERNS:
        if pat.search(error_message):
            return False
    # Check retriable patterns
    for pat in _RETRIABLE_PATTERNS:
        if pat.search(error_message):
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Retry policy
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RetryPolicy:
    """
    Configurable retry strategy.

    Parameters
    ----------
    max_retries     : Maximum number of retry attempts (0 = no retries).
    base_delay_s    : Initial delay in seconds before the first retry.
    max_delay_s     : Cap on the backoff delay.
    backoff_factor  : Multiplier applied to the delay after each attempt.
    """
    max_retries:    int   = 3
    base_delay_s:   float = 1.0
    max_delay_s:    float = 30.0
    backoff_factor: float = 2.0

    def delay_for_attempt(self, attempt: int) -> float:
        """Return the delay in seconds for the given attempt (0-based)."""
        delay = self.base_delay_s * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay_s)


# Default policy used by the orchestrator
DEFAULT_POLICY = RetryPolicy(max_retries=3, base_delay_s=1.0, backoff_factor=2.0)


def retry_download(
    fn:             Callable[[], None],
    policy:         RetryPolicy = DEFAULT_POLICY,
    cancel_event:   Optional[threading.Event] = None,
    job_key:        str = "",
) -> Optional[str]:
    """
    Call ``fn()`` with retry-on-retriable-error logic.

    Parameters
    ----------
    fn           : The download callable (e.g. ``lambda: engine.download(req)``).
                   Should raise on failure; success = no exception.
    policy       : Retry policy to use.
    cancel_event : If set, abort retries immediately.
    job_key      : Identifier for logging.

    Returns
    -------
    None on success, or the final error message string on exhausted retries.
    """
    last_error = ""

    for attempt in range(1 + policy.max_retries):
        if cancel_event and cancel_event.is_set():
            return "Cancelled"

        try:
            fn()
            return None  # success
        except Exception as exc:
            last_error = str(exc)

            # Refresh a transfer-stage 403 once; a second refusal is final for
            # this track. Generic retries retain their configured budget.
            retry_limit = (
                min(policy.max_retries, 1)
                if is_temporary_media_403(last_error)
                else policy.max_retries
            )

            if attempt >= retry_limit:
                logger.warning(
                    "[Retry] %s — all %d attempts exhausted: %s",
                    job_key, retry_limit + 1, last_error[:100],
                )
                return last_error

            if not is_retriable(last_error):
                logger.debug(
                    "[Retry] %s — non-retriable error, giving up: %s",
                    job_key, last_error[:100],
                )
                return last_error

            delay = policy.delay_for_attempt(attempt)
            logger.info(
                "[Retry] %s — attempt %d/%d failed (retriable), "
                "waiting %.1fs: %s",
                job_key, attempt + 1, retry_limit + 1,
                delay, last_error[:80],
            )

            # Sleep in small increments so cancellation is responsive
            slept = 0.0
            while slept < delay:
                if cancel_event and cancel_event.is_set():
                    return "Cancelled"
                chunk = min(0.5, delay - slept)
                time.sleep(chunk)
                slept += chunk

    return last_error
