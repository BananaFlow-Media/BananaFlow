"""Shared download admission, rate-limit recovery, and user decisions.

The downloader has several independent producers of YouTube requests: direct
downloads, Spotify-to-YouTube resolution, match prefetch, and single-track
resume workers.  A per-track retry sleep cannot protect that process-wide
request stream.  This module therefore owns one pure-Python coordinator that
all of those paths can share without importing Qt.

The gates are intentionally distinct:

* an explicit YouTube rate limit closes network admission until the advertised
  delay expires, then admits exactly one canary (the request that observed the
  limit) before releasing peers; and
* a systemic failure closes admission only after its policy threshold (three
  consecutive authentication/network failures, or immediately for a missing
  required runtime); and
* legacy synchronous user-action requests remain available for compatibility,
  but ordinary per-track failures are collected by the UI without closing
  process-wide admission.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60 * 60
_MAX_RATE_LIMIT_WAIT_SECONDS = 24 * 60 * 60
RATE_LIMIT_SAFETY_RATIO = 0.05
RATE_LIMIT_SAFETY_MIN_SECONDS = 5.0
RATE_LIMIT_SAFETY_MAX_SECONDS = 60.0

_EXPLICIT_RATE_LIMIT_RE = re.compile(
    r"(?:\b(?:http(?:\s+error)?|status(?:\s+code)?)\s*[:=]?\s*429\b)"
    r"|(?:\b429\b(?=.{0,32}\b(?:too many requests|rate[\s_-]*limit|throttl)))"
    r"|too many requests|rate[\s_-]*limit(?:ed|ing)?|throttl(?:e|ed|ing)",
    re.I | re.S,
)
_RETRY_AFTER_RE = re.compile(r"retry[- ]after\s*[:=]\s*(\d+(?:\.\d+)?)", re.I)
_HUMAN_DELAY_RE = re.compile(
    r"(?:wait(?:ing)?|try(?:ing)?(?: again)?|retry(?:ing)?)"
    r".{0,80}?(?:for\s+)?(?:up\s+to\s+)?"
    r"(an?|one|\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.I | re.S,
)
_RATE_CONTEXT_DELAY_RE = re.compile(
    r"(?:rate[\s_-]*limit(?:ed|ing)?|throttl(?:e|ed|ing))"
    r".{0,100}?(?:for\s+)?(?:up\s+to\s+)?"
    r"(an?|one|\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.I | re.S,
)


def is_explicit_rate_limit(message: str) -> bool:
    """Return whether text is definite throttling, not a generic 403/bot check."""
    return bool(_EXPLICIT_RATE_LIMIT_RE.search(message or ""))


def rate_limit_retry_after_seconds(
    message: str,
    default: float = DEFAULT_RATE_LIMIT_WAIT_SECONDS,
) -> float:
    """Parse an advertised cooldown, defaulting to one hour.

    yt-dlp commonly says "try waiting for up to an hour" rather than exposing
    a numeric Retry-After header.  Both forms are accepted.  The defensive
    24-hour ceiling prevents malformed upstream prose from parking the process
    indefinitely while still respecting every duration YouTube currently uses.
    """
    text = message or ""
    header = _RETRY_AFTER_RE.search(text)
    if header:
        seconds = float(header.group(1))
    else:
        human = _HUMAN_DELAY_RE.search(text) or _RATE_CONTEXT_DELAY_RE.search(text)
        if not human:
            seconds = float(default)
        else:
            amount_text = human.group(1).casefold()
            amount = 1.0 if amount_text in {"a", "an", "one"} else float(amount_text)
            unit = human.group(2).casefold()
            if unit.startswith(("hour", "hr")):
                seconds = amount * 3600.0
            elif unit.startswith(("minute", "min")):
                seconds = amount * 60.0
            else:
                seconds = amount
    return max(0.0, min(float(seconds), float(_MAX_RATE_LIMIT_WAIT_SECONDS)))


def rate_limit_safety_margin_seconds(advertised_seconds: float) -> float:
    """Return a modest post-deadline buffer without slowing tiny test waits."""
    advertised = max(0.0, float(advertised_seconds))
    if advertised < 1.0:
        return 0.0
    return min(
        RATE_LIMIT_SAFETY_MAX_SECONDS,
        max(RATE_LIMIT_SAFETY_MIN_SECONDS, advertised * RATE_LIMIT_SAFETY_RATIO),
    )


class YouTubeRateLimited(RuntimeError):
    """Typed rate-limit evidence that must survive broad fallback handlers."""

    def __init__(self, message: str, retry_after_s: Optional[float] = None) -> None:
        self.raw = str(message or "YouTube rate limited the request")
        self.retry_after_s = (
            rate_limit_retry_after_seconds(self.raw)
            if retry_after_s is None
            else max(0.0, float(retry_after_s))
        )
        super().__init__(self.raw)


def rate_limit_error_from_messages(*messages: str) -> Optional[YouTubeRateLimited]:
    """Build a typed error from the first definite rate-limit message."""
    for message in messages:
        text = str(message or "").strip()
        if is_explicit_rate_limit(text):
            return YouTubeRateLimited(text)
    return None


class RecoveryDecision(str, Enum):
    RETRY = "retry"
    SKIP = "skip"
    CANCEL = "cancel"


@dataclass(frozen=True)
class SystemicRecoveryPolicy:
    """When repeated failures prove that continuing cannot be useful."""

    scope: str
    threshold: int
    authentication: bool = False


_AUTH_ERROR_KEYS = frozenset({
    "err_browser_cookie_access",
    "err_cookies_expired",
    "err_signin_required",
})


def systemic_recovery_policy(error: Any) -> Optional[SystemicRecoveryPolicy]:
    """Return the process-wide stop policy for a classified error, if any."""
    key = str(getattr(error, "message_key", "") or "")
    if key in _AUTH_ERROR_KEYS:
        return SystemicRecoveryPolicy("youtube_auth", 3, authentication=True)
    if key == "err_po_token":
        return SystemicRecoveryPolicy("youtube_runtime_auth", 3)
    if key == "err_bot_challenge":
        return SystemicRecoveryPolicy("youtube_access", 3)
    if key in {
        "err_network",
        "err_ssl",
        "err_no_internet",
        "err_connection_failed",
        "err_timeout",
    }:
        return SystemicRecoveryPolicy("network_connectivity", 3)
    if key in {
        "err_js_runtime",
        "err_ffmpeg_missing",
        "err_disk_permissions",
        "err_permission_denied",
    }:
        return SystemicRecoveryPolicy(f"system:{key}", 1)
    return None


@dataclass(frozen=True)
class FailureIncidentItem:
    """One stopped track represented inside a live aggregate incident."""

    key: str
    title: str
    url: str
    raw: str = ""
    artist: str = ""
    album: str = ""
    duration_sec: Optional[int] = None


@dataclass
class DownloadFailureIncident:
    """A single updating UI incident for equivalent per-track failures."""

    scope: str
    error: Any
    systemic_policy: Optional[SystemicRecoveryPolicy] = None
    incident_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    items: list[FailureIncidentItem] = field(default_factory=list)
    stopped_all: bool = False
    streak: int = 0
    revision: int = 0

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def authentication(self) -> bool:
        return bool(self.systemic_policy and self.systemic_policy.authentication)

    def add(self, item: FailureIncidentItem, *, stopped_all: bool, streak: int) -> None:
        if not any(existing.key == item.key for existing in self.items):
            self.items.append(item)
        self.stopped_all = self.stopped_all or bool(stopped_all)
        self.streak = max(self.streak, int(streak))
        self.revision += 1


@dataclass
class UserActionRequest:
    """One pre-terminal question handed from a worker to the UI thread."""

    key: str
    error: Any
    failing_url: str = ""
    recovery_scope: str = ""
    _done: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _decision: Optional[RecoveryDecision] = field(default=None, init=False, repr=False)

    def resolve(self, decision: RecoveryDecision | str) -> bool:
        """Resolve once; return False when another path already won."""
        resolved = RecoveryDecision(decision)
        with self._lock:
            if self._done.is_set():
                return False
            self._decision = resolved
            self._done.set()
            return True

    @property
    def decision(self) -> Optional[RecoveryDecision]:
        with self._lock:
            return self._decision

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)


@dataclass(frozen=True)
class StartPermit:
    """Admission token used to identify the single post-cooldown canary."""

    rate_epoch: int
    canary: bool = False


CancelCheck = Callable[[], bool]
WaitCallback = Callable[[float], None]


class DownloadRecoveryCoordinator:
    """Thread-safe process-wide admission and decision coordinator."""

    def __init__(
        self,
        *,
        default_rate_limit_wait: float = DEFAULT_RATE_LIMIT_WAIT_SECONDS,
        poll_interval: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._default_rate_limit_wait = max(0.0, float(default_rate_limit_wait))
        self._poll_interval = max(0.01, float(poll_interval))
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._resolution_gate = threading.Lock()
        self._conservative_gate = threading.Lock()
        self._conservative_next_start = 0.0
        self._conservative_waiters = 0

        self._blocked_until = 0.0
        self._rate_epoch = 0
        self._rate_owner: Optional[str] = None
        self._canary_in_flight = False
        self._rate_message = ""
        self._rate_advertised_s = 0.0
        self._rate_margin_s = 0.0

        self._systemic_streaks: dict[str, int] = {}
        self._systemic_pauses: set[str] = set()

        self._active_user_action: Optional[UserActionRequest] = None
        self._retry_generation: dict[str, int] = {}
        # A failed attempt installs a hold before it releases a rate canary or
        # ordinary permit. That makes the transition into the user-decision
        # gate atomic from peers' point of view. The failed owner may perform
        # its own bounded retries; unrelated requests remain parked.
        self._failure_holds: set[str] = set()

    def rate_remaining(self) -> float:
        with self._condition:
            return max(0.0, self._blocked_until - self._clock())

    def rate_incident_active(self) -> bool:
        """Whether the cooldown/canary incident still owns admission.

        The numeric countdown reaches zero *before* the same-track canary has
        proved that traffic may resume.  Callers that gate unrelated UI work
        must therefore use this state as well as :meth:`rate_remaining`.
        """
        with self._condition:
            return self._blocked_until > 0.0 or self._canary_in_flight

    def rate_snapshot(self) -> dict[str, Any]:
        """Return immutable UI-facing state for the current rate incident."""
        with self._condition:
            return {
                "epoch": self._rate_epoch,
                "message": self._rate_message,
                "advertised_seconds": self._rate_advertised_s,
                "margin_seconds": self._rate_margin_s,
                "remaining_seconds": max(0.0, self._blocked_until - self._clock()),
                "active": self._blocked_until > 0.0 or self._canary_in_flight,
                "canary_in_flight": self._canary_in_flight,
            }

    def note_systemic_failure(self, policy: SystemicRecoveryPolicy) -> tuple[int, bool]:
        """Record a consecutive systemic failure and stop at its threshold.

        Returns ``(streak, newly_stopped_all)``.  Once stopped, the scope stays
        closed until the user explicitly retries after repairing the condition
        or skips the collected incident.
        """
        with self._condition:
            streak = self._systemic_streaks.get(policy.scope, 0) + 1
            self._systemic_streaks[policy.scope] = streak
            newly_stopped = False
            if streak >= max(1, int(policy.threshold)):
                newly_stopped = policy.scope not in self._systemic_pauses
                self._systemic_pauses.add(policy.scope)
            self._condition.notify_all()
            return streak, newly_stopped

    def note_successful_track(self) -> None:
        """A completed track breaks only streaks that have not stopped work."""
        with self._condition:
            for scope in tuple(self._systemic_streaks):
                if scope not in self._systemic_pauses:
                    self._systemic_streaks.pop(scope, None)
            self._condition.notify_all()

    def reset_systemic(self, scope: str) -> None:
        """Release a repaired/skipped systemic incident and reset its streak."""
        with self._condition:
            self._systemic_streaks.pop(scope, None)
            self._systemic_pauses.discard(scope)
            self._condition.notify_all()

    def systemic_state(self, scope: str) -> tuple[int, bool]:
        with self._condition:
            return (
                self._systemic_streaks.get(scope, 0),
                scope in self._systemic_pauses,
            )

    def systemic_pause_active(self, scope: str = "") -> bool:
        with self._condition:
            if scope:
                return scope in self._systemic_pauses
            return bool(self._systemic_pauses)

    def acquire_resolution_slot(self, cancel_check: CancelCheck) -> bool:
        """Serialize opaque Spotify→YouTube search chains process-wide."""
        while not self._resolution_gate.acquire(timeout=self._poll_interval):
            if cancel_check():
                return False
        if cancel_check():
            self._resolution_gate.release()
            return False
        return True

    def release_resolution_slot(self) -> None:
        self._resolution_gate.release()

    def acquire_conservative_slot(self, cancel_check: CancelCheck) -> bool:
        """Acquire the process-wide conservative YouTube start slot."""
        with self._condition:
            self._conservative_waiters += 1
        acquired = False
        try:
            while not self._conservative_gate.acquire(timeout=self._poll_interval):
                if cancel_check():
                    return False
            acquired = True
        finally:
            with self._condition:
                self._conservative_waiters = max(
                    0, self._conservative_waiters - 1,
                )
        if not acquired:
            return False
        while True:
            if cancel_check():
                self._conservative_gate.release()
                return False
            with self._condition:
                remaining = self._conservative_next_start - self._clock()
            if remaining <= 0:
                return True
            time.sleep(min(self._poll_interval, remaining))

    def release_conservative_slot(self, cooldown_seconds: float = 0.0) -> None:
        with self._condition:
            if self._conservative_waiters:
                self._conservative_next_start = max(
                    self._conservative_next_start,
                    self._clock() + max(0.0, float(cooldown_seconds)),
                )
            else:
                self._conservative_next_start = 0.0
            self._condition.notify_all()
        self._conservative_gate.release()

    @property
    def active_user_action(self) -> Optional[UserActionRequest]:
        with self._condition:
            return self._active_user_action

    def wait_to_start(
        self,
        request_key: str,
        cancel_check: CancelCheck,
        on_rate_wait: Optional[WaitCallback] = None,
    ) -> Optional[StartPermit]:
        """Wait until admission opens and return a permit, or None on cancel."""
        last_reported_second: Optional[int] = None
        while True:
            if cancel_check():
                self.abandon_owner(request_key)
                if on_rate_wait is not None and last_reported_second is not None:
                    on_rate_wait(0.0)
                return None

            report_remaining: Optional[float] = None
            with self._condition:
                now = self._clock()
                if self._systemic_pauses:
                    wait_for = self._poll_interval
                elif self._active_user_action is not None:
                    wait_for = self._poll_interval
                elif self._failure_holds and request_key not in self._failure_holds:
                    wait_for = self._poll_interval
                elif self._blocked_until > now:
                    report_remaining = self._blocked_until - now
                    wait_for = min(self._poll_interval, report_remaining)
                elif self._blocked_until > 0.0:
                    # Cooldown expired. The request that actually observed it
                    # owns the canary so a peer cannot jump the queue and create
                    # a thundering herd. Cancellation hands ownership over.
                    owner_ready = self._rate_owner in {None, request_key}
                    if owner_ready and not self._canary_in_flight:
                        self._canary_in_flight = True
                        permit = StartPermit(self._rate_epoch, canary=True)
                        return permit
                    wait_for = self._poll_interval
                else:
                    return StartPermit(self._rate_epoch, canary=False)

            if report_remaining is not None and on_rate_wait is not None:
                second = max(1, int(math.ceil(report_remaining)))
                if second != last_reported_second:
                    last_reported_second = second
                    on_rate_wait(float(second))
            # Condition.wait() wakes promptly on a decision/rate transition;
            # the bounded timeout also observes external cancellation events.
            with self._condition:
                self._condition.wait(timeout=max(0.01, wait_for))

    def note_rate_limit(
        self,
        permit: StartPermit,
        request_key: str,
        message: str,
        retry_after_s: Optional[float] = None,
    ) -> tuple[float, bool]:
        """Close admission and return (effective cooldown, first notice)."""
        advertised = (
            rate_limit_retry_after_seconds(message, self._default_rate_limit_wait)
            if retry_after_s is None
            else max(0.0, float(retry_after_s))
        )
        margin = rate_limit_safety_margin_seconds(advertised)
        delay = advertised + margin
        with self._condition:
            now = self._clock()
            already_blocked = self._blocked_until > now
            # Errors from requests that were already in flight belong to the
            # same incident and must not extend the deadline once per worker.
            new_incident = not already_blocked or permit.canary
            if new_incident:
                self._rate_epoch += 1
                self._blocked_until = now + delay
                self._rate_owner = request_key
                self._rate_message = str(message or "")
                self._rate_advertised_s = advertised
                self._rate_margin_s = margin
            else:
                # Several requests may already be in flight when the first
                # response closes admission. Respect the longest advertised
                # deadline without treating each late response as a new
                # incident or repeatedly notifying the UI.
                self._blocked_until = max(self._blocked_until, now + delay)
                if delay >= self._rate_advertised_s + self._rate_margin_s:
                    self._rate_message = str(message or self._rate_message)
                    self._rate_advertised_s = advertised
                    self._rate_margin_s = margin
            self._failure_holds.discard(request_key)
            if permit.canary:
                self._canary_in_flight = False
            self._condition.notify_all()
            return delay, new_incident

    def complete_attempt(self, permit: StartPermit) -> None:
        """Release peers after a non-rate-limited canary result."""
        if not permit.canary:
            return
        with self._condition:
            if permit.rate_epoch == self._rate_epoch:
                self._blocked_until = 0.0
                self._rate_owner = None
            self._canary_in_flight = False
            self._condition.notify_all()

    def abandon_attempt(self, permit: StartPermit, request_key: str) -> None:
        """Give back a canary that was cancelled before producing a result."""
        if not permit.canary:
            return
        with self._condition:
            self._canary_in_flight = False
            if self._rate_owner == request_key:
                self._rate_owner = None
            self._condition.notify_all()

    def abandon_owner(self, request_key: str) -> None:
        with self._condition:
            if self._rate_owner == request_key:
                self._rate_owner = None
                self._canary_in_flight = False
                self._condition.notify_all()

    def hold_failure(self, request_key: str) -> None:
        """Block unrelated admission while this failed request is recovered."""
        with self._condition:
            self._failure_holds.add(request_key)
            self._condition.notify_all()

    def release_failure(self, request_key: str) -> None:
        with self._condition:
            if request_key in self._failure_holds:
                self._failure_holds.discard(request_key)
                self._condition.notify_all()

    def is_start_permit_valid(self, permit: StartPermit, request_key: str) -> bool:
        """Revalidate admission immediately before a paced network start.

        A rate limit or user prompt can arrive while a Fast-mode worker waits
        for its reserved cadence slot. Stale permits must go around the gate
        again instead of starting in a burst after that wait.
        """
        with self._condition:
            now = self._clock()
            if self._systemic_pauses:
                return False
            if self._active_user_action is not None:
                return False
            if self._failure_holds and request_key not in self._failure_holds:
                return False
            if permit.rate_epoch != self._rate_epoch:
                return False
            if self._blocked_until > now:
                return False
            if permit.canary:
                return self._canary_in_flight
            return True

    def request_user_action(
        self,
        *,
        key: str,
        error: Any,
        failing_url: str,
        notify: Callable[[UserActionRequest], None],
        cancel_check: CancelCheck,
        recovery_scope: str = "",
        hold_key: str = "",
    ) -> RecoveryDecision:
        """Serialize one UI question and block network admission around it."""
        scope = recovery_scope or f"{type(error).__name__}:{error}"
        with self._condition:
            observed_retry_generation = self._retry_generation.get(scope, 0)

        request: Optional[UserActionRequest] = None
        while request is None:
            if cancel_check():
                if hold_key:
                    self.release_failure(hold_key)
                return RecoveryDecision.CANCEL
            with self._condition:
                if self._retry_generation.get(scope, 0) != observed_retry_generation:
                    # Another prompt successfully repaired the shared config
                    # (normally cookies). Retry this already-failed peer without
                    # presenting a stale duplicate question.
                    if hold_key:
                        self._failure_holds.discard(hold_key)
                        self._condition.notify_all()
                    return RecoveryDecision.RETRY
                if self._active_user_action is None:
                    request = UserActionRequest(
                        key=key,
                        error=error,
                        failing_url=failing_url,
                        recovery_scope=scope,
                    )
                    self._active_user_action = request
                    self._condition.notify_all()
                    break
                self._condition.wait(timeout=self._poll_interval)

        try:
            try:
                notify(request)
            except Exception:  # noqa: BLE001 - never strand a worker on UI failure
                logger.exception("User-action callback failed; skipping track safely")
                request.resolve(RecoveryDecision.SKIP)

            while not request.wait(self._poll_interval):
                if cancel_check():
                    request.resolve(RecoveryDecision.CANCEL)
                    break
            decision = request.decision or RecoveryDecision.SKIP
            return decision
        finally:
            with self._condition:
                decision = request.decision or RecoveryDecision.SKIP
                if decision == RecoveryDecision.RETRY:
                    self._retry_generation[scope] = (
                        self._retry_generation.get(scope, 0) + 1
                    )
                if self._active_user_action is request:
                    self._active_user_action = None
                if hold_key:
                    self._failure_holds.discard(hold_key)
                self._condition.notify_all()


_SHARED_COORDINATOR = DownloadRecoveryCoordinator()


def shared_download_recovery_coordinator() -> DownloadRecoveryCoordinator:
    return _SHARED_COORDINATOR
