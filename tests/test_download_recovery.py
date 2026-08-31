"""Deterministic coverage for shared download recovery coordination.

These tests use only local threads and short injected cooldowns.  They pin the
exact YouTube wording observed in the field so broad ``account`` / ``video
unavailable`` matching cannot silently turn a rate limit back into an auth or
permanent-media failure.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from core.download_recovery import (
    DownloadRecoveryCoordinator,
    RecoveryDecision,
    UserActionRequest,
    YouTubeRateLimited,
    is_explicit_rate_limit,
    rate_limit_error_from_messages,
    rate_limit_retry_after_seconds,
    rate_limit_safety_margin_seconds,
    systemic_recovery_policy,
)
from core.retry_policy import RetryPolicy, is_retriable, retry_download
from core.match_errors import YouTubeSearchNoResults
from error_handler import classify_error


REAL_YOUTUBE_RATE_LIMIT = (
    "ERROR: [youtube] 4gVaR9SXlVo: Video unavailable. This content isn't "
    "available, try again later. Your account has been rate-limited by "
    "YouTube for up to an hour. It is recommended to use `-t sleep` to add "
    "a delay between video requests to avoid exceeding the rate limit. For "
    "more information, refer to https://github.com/yt-dlp/yt-dlp/wiki/"
    "Extractors#this-content-isnt-available-try-again-later"
)


def _start_call(target):
    """Run a blocking coordinator call without letting failures disappear."""
    state: dict[str, object] = {}

    def run() -> None:
        try:
            state["result"] = target()
        except BaseException as exc:  # pragma: no cover - asserted by caller
            state["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, state


def _join_cleanly(thread: threading.Thread, state: dict[str, object]) -> None:
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "recovery wait did not unblock"
    assert "error" not in state, state.get("error")


def test_real_youtube_rate_limit_outranks_account_and_video_unavailable() -> None:
    assert is_explicit_rate_limit(REAL_YOUTUBE_RATE_LIMIT)
    assert rate_limit_retry_after_seconds(REAL_YOUTUBE_RATE_LIMIT) == 3600.0
    assert is_retriable(REAL_YOUTUBE_RATE_LIMIT) is True
    assert classify_error(Exception(REAL_YOUTUBE_RATE_LIMIT)).message_key == "err_rate_limited"

    typed = rate_limit_error_from_messages(
        "HTTP Error 403: Forbidden",
        REAL_YOUTUBE_RATE_LIMIT,
    )
    assert isinstance(typed, YouTubeRateLimited)
    assert typed.raw == REAL_YOUTUBE_RATE_LIMIT
    assert typed.retry_after_s == 3600.0


def test_zero_item_search_is_not_reported_as_a_lost_output_file() -> None:
    error = classify_error(YouTubeSearchNoResults(
        "YouTube search returned 0 items; no output file was expected"
    ))

    assert error.message_key == "err_no_search_results"
    assert error.retriable is True
    assert "no file was created or lost" in error.detail
    string_only = classify_error(Exception(
        "YouTube search returned 0 items; no output file was expected"
    ))
    assert string_only.message_key == "err_no_search_results"
    assert string_only.retriable is True


@pytest.mark.parametrize(
    ("message", "default", "expected"),
    [
        ("Retry-After: 17.5", 3600.0, 17.5),
        ("Please wait for 2 minutes before retrying", 3600.0, 120.0),
        ("Try again in 3 seconds", 3600.0, 3.0),
        ("definite rate limit without a duration", 7.0, 7.0),
        ("Retry-After: 999999", 3600.0, 24.0 * 60.0 * 60.0),
    ],
)
def test_rate_limit_delay_parser_handles_supported_forms_and_ceiling(
    message: str,
    default: float,
    expected: float,
) -> None:
    assert rate_limit_retry_after_seconds(message, default=default) == expected


def test_bare_fragment_index_429_is_not_rate_limit_evidence() -> None:
    fragment_progress = "[download] Downloading fragment 429 of 912"

    assert is_explicit_rate_limit(fragment_progress) is False
    assert rate_limit_error_from_messages(fragment_progress) is None
    assert is_explicit_rate_limit("HTTP Error 429: Too Many Requests") is True


def test_rate_limited_for_ten_minutes_parses_six_hundred_seconds() -> None:
    message = "Your account has been rate-limited for 10 minutes."

    assert is_explicit_rate_limit(message) is True
    assert rate_limit_retry_after_seconds(message) == 600.0


def test_retry_loop_hands_explicit_rate_limit_off_without_short_retries(
    monkeypatch,
) -> None:
    calls = 0

    def fail_once_into_shared_cooldown() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(REAL_YOUTUBE_RATE_LIMIT)

    monkeypatch.setattr(
        "core.retry_policy.time.sleep",
        lambda _seconds: pytest.fail("explicit rate limit used local backoff"),
    )
    result = retry_download(
        fail_once_into_shared_cooldown,
        RetryPolicy(max_retries=3, base_delay_s=99.0),
        job_key="rate-limited-track",
    )

    assert result == REAL_YOUTUBE_RATE_LIMIT
    assert calls == 1


def test_rate_gate_admits_same_track_canary_before_waiting_peer() -> None:
    coordinator = DownloadRecoveryCoordinator(
        default_rate_limit_wait=0.06,
        poll_interval=0.005,
    )
    initial = coordinator.wait_to_start("limited", lambda: False)
    assert initial is not None and initial.canary is False
    delay, first_notice = coordinator.note_rate_limit(
        initial,
        "limited",
        REAL_YOUTUBE_RATE_LIMIT,
        retry_after_s=0.06,
    )
    assert delay == 0.06
    assert first_notice is True

    owner_thread, owner_state = _start_call(
        lambda: coordinator.wait_to_start("limited", lambda: False)
    )
    peer_thread, peer_state = _start_call(
        lambda: coordinator.wait_to_start("peer", lambda: False)
    )

    _join_cleanly(owner_thread, owner_state)
    owner_permit = owner_state["result"]
    assert owner_permit is not None and owner_permit.canary is True

    # Expiry alone must not release the queue: the same track probes first.
    time.sleep(0.02)
    assert peer_thread.is_alive()
    assert "result" not in peer_state

    coordinator.complete_attempt(owner_permit)
    _join_cleanly(peer_thread, peer_state)
    peer_permit = peer_state["result"]
    assert peer_permit is not None and peer_permit.canary is False


def test_later_longer_inflight_limit_extends_incident_without_second_notice() -> None:
    now = [100.0]
    coordinator = DownloadRecoveryCoordinator(clock=lambda: now[0])
    first_permit = coordinator.wait_to_start("first", lambda: False)
    later_permit = coordinator.wait_to_start("later-inflight", lambda: False)
    assert first_permit is not None
    assert later_permit is not None

    first_delay, first_notice = coordinator.note_rate_limit(
        first_permit,
        "first",
        "YouTube rate-limited this request for 10 minutes.",
    )
    assert (first_delay, first_notice) == (630.0, True)
    assert coordinator.rate_remaining() == 630.0

    # This response was already in flight when the first one closed the gate.
    # It extends the same incident to its later/longer deadline, but must not
    # cause another modal/status notice.
    now[0] += 5.0
    later_delay, later_notice = coordinator.note_rate_limit(
        later_permit,
        "later-inflight",
        REAL_YOUTUBE_RATE_LIMIT,
    )
    assert (later_delay, later_notice) == (3660.0, False)
    assert coordinator.rate_remaining() == 3660.0


def test_rate_limit_snapshot_keeps_exact_message_and_safety_margin() -> None:
    now = [10.0]
    coordinator = DownloadRecoveryCoordinator(clock=lambda: now[0])
    permit = coordinator.wait_to_start("limited", lambda: False)
    assert permit is not None

    effective, first = coordinator.note_rate_limit(
        permit,
        "limited",
        REAL_YOUTUBE_RATE_LIMIT,
    )

    assert first is True
    assert effective == 3660.0
    assert rate_limit_safety_margin_seconds(3600) == 60.0
    assert rate_limit_safety_margin_seconds(600) == 30.0
    assert rate_limit_safety_margin_seconds(0.05) == 0.0
    assert coordinator.rate_snapshot() == {
        "epoch": 1,
        "message": REAL_YOUTUBE_RATE_LIMIT,
        "advertised_seconds": 3600.0,
        "margin_seconds": 60.0,
        "remaining_seconds": 3660.0,
        "active": True,
        "canary_in_flight": False,
    }


def test_auth_stops_all_exactly_on_third_consecutive_failure() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)
    policy = systemic_recovery_policy(
        SimpleNamespace(message_key="err_cookies_expired")
    )
    assert policy is not None and policy.threshold == 3 and policy.authentication

    assert coordinator.note_systemic_failure(policy) == (1, False)
    assert coordinator.systemic_pause_active() is False
    assert coordinator.note_systemic_failure(policy) == (2, False)
    assert coordinator.systemic_pause_active() is False
    assert coordinator.note_systemic_failure(policy) == (3, True)
    assert coordinator.systemic_state("youtube_auth") == (3, True)

    peer_thread, peer_state = _start_call(
        lambda: coordinator.wait_to_start("peer", lambda: False)
    )
    time.sleep(0.03)
    assert peer_thread.is_alive()

    coordinator.reset_systemic("youtube_auth")
    _join_cleanly(peer_thread, peer_state)
    assert coordinator.systemic_state("youtube_auth") == (0, False)


def test_success_breaks_unpaused_auth_streak_but_not_an_active_stop() -> None:
    coordinator = DownloadRecoveryCoordinator()
    policy = systemic_recovery_policy(
        SimpleNamespace(message_key="err_signin_required")
    )
    assert policy is not None

    coordinator.note_systemic_failure(policy)
    coordinator.note_systemic_failure(policy)
    coordinator.note_successful_track()
    assert coordinator.systemic_state(policy.scope) == (0, False)

    coordinator.note_systemic_failure(policy)
    coordinator.note_systemic_failure(policy)
    coordinator.note_systemic_failure(policy)
    coordinator.note_successful_track()
    assert coordinator.systemic_state(policy.scope) == (3, True)


def test_rate_gate_wait_is_cancellable() -> None:
    coordinator = DownloadRecoveryCoordinator(
        default_rate_limit_wait=10.0,
        poll_interval=0.005,
    )
    initial = coordinator.wait_to_start("limited", lambda: False)
    assert initial is not None
    coordinator.note_rate_limit(
        initial,
        "limited",
        REAL_YOUTUBE_RATE_LIMIT,
        retry_after_s=10.0,
    )

    cancel = threading.Event()
    entered_wait = threading.Event()
    thread, state = _start_call(
        lambda: coordinator.wait_to_start(
            "peer",
            cancel.is_set,
            on_rate_wait=lambda _remaining: entered_wait.set(),
        )
    )
    assert entered_wait.wait(timeout=1.0)

    cancel.set()
    _join_cleanly(thread, state)
    assert state["result"] is None


def test_start_permit_becomes_stale_after_rate_transition() -> None:
    now = [50.0]
    coordinator = DownloadRecoveryCoordinator(clock=lambda: now[0])
    stale_permit = coordinator.wait_to_start("paced-peer", lambda: False)
    limiter_permit = coordinator.wait_to_start("limiter", lambda: False)
    assert stale_permit is not None
    assert limiter_permit is not None
    assert coordinator.is_start_permit_valid(stale_permit, "paced-peer") is True

    coordinator.note_rate_limit(
        limiter_permit,
        "limiter",
        "HTTP Error 429: Too Many Requests",
        retry_after_s=1.0,
    )
    assert coordinator.is_start_permit_valid(stale_permit, "paced-peer") is False

    # Even after the wall-clock deadline, the old epoch cannot be reused to
    # bypass the same-track canary admission step.
    now[0] += 2.0
    assert coordinator.is_start_permit_valid(stale_permit, "paced-peer") is False


def test_failure_hold_blocks_unrelated_starts_but_allows_owner_retry() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)
    coordinator.hold_failure("failed-owner")

    owner_permit = coordinator.wait_to_start("failed-owner", lambda: False)
    assert owner_permit is not None
    assert coordinator.is_start_permit_valid(owner_permit, "failed-owner") is True

    peer_thread, peer_state = _start_call(
        lambda: coordinator.wait_to_start("unrelated-peer", lambda: False)
    )
    time.sleep(0.02)
    assert peer_thread.is_alive()
    assert "result" not in peer_state

    coordinator.release_failure("failed-owner")
    _join_cleanly(peer_thread, peer_state)
    assert peer_state["result"] is not None


def test_identical_user_errors_prompt_serially_after_each_skip() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)
    notified: list[UserActionRequest] = []
    notification = threading.Event()

    def notify(request: UserActionRequest) -> None:
        notified.append(request)
        notification.set()

    def ask(key: str):
        return coordinator.request_user_action(
            key=key,
            error="same failure",
            failing_url=f"https://example.test/{key}",
            notify=notify,
            cancel_check=lambda: False,
        )

    first_thread, first_state = _start_call(lambda: ask("first"))
    second_thread, second_state = _start_call(lambda: ask("second"))

    assert notification.wait(timeout=1.0)
    assert len(notified) == 1
    time.sleep(0.02)
    assert len(notified) == 1, "a second prompt opened while the first was unresolved"

    first_request = notified[0]
    notification.clear()
    assert first_request.resolve(RecoveryDecision.SKIP) is True
    assert first_request.resolve(RecoveryDecision.RETRY) is False
    _join_cleanly(first_thread if first_request.key == "first" else second_thread,
                  first_state if first_request.key == "first" else second_state)

    assert notification.wait(timeout=1.0)
    assert len(notified) == 2
    second_request = notified[1]
    assert second_request.error == first_request.error
    assert second_request.resolve(RecoveryDecision.SKIP) is True

    _join_cleanly(first_thread, first_state)
    _join_cleanly(second_thread, second_state)
    assert first_state["result"] == RecoveryDecision.SKIP
    assert second_state["result"] == RecoveryDecision.SKIP
    assert coordinator.active_user_action is None


def test_successful_user_retry_releases_waiting_duplicate_without_new_prompt() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)
    notified: list[UserActionRequest] = []
    notification = threading.Event()

    def notify(request: UserActionRequest) -> None:
        notified.append(request)
        notification.set()

    def ask(key: str):
        return coordinator.request_user_action(
            key=key,
            error="stale cookies",
            failing_url=f"https://example.test/{key}",
            notify=notify,
            cancel_check=lambda: False,
        )

    first_thread, first_state = _start_call(lambda: ask("first"))
    second_thread, second_state = _start_call(lambda: ask("second"))
    assert notification.wait(timeout=1.0)
    assert len(notified) == 1

    assert notified[0].resolve(RecoveryDecision.RETRY) is True
    _join_cleanly(first_thread, first_state)
    _join_cleanly(second_thread, second_state)

    assert len(notified) == 1
    assert first_state["result"] == RecoveryDecision.RETRY
    assert second_state["result"] == RecoveryDecision.RETRY
    assert coordinator.active_user_action is None


def test_retry_generation_is_scoped_and_does_not_release_heterogeneous_error() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)
    notified: list[UserActionRequest] = []
    first_notification = threading.Event()
    second_notification = threading.Event()

    def notify(request: UserActionRequest) -> None:
        notified.append(request)
        if len(notified) == 1:
            first_notification.set()
        else:
            second_notification.set()

    first_thread, first_state = _start_call(
        lambda: coordinator.request_user_action(
            key="auth-track",
            error="stale cookies",
            failing_url="https://example.test/auth",
            recovery_scope="youtube-auth",
            notify=notify,
            cancel_check=lambda: False,
        )
    )
    assert first_notification.wait(timeout=1.0)

    second_thread, second_state = _start_call(
        lambda: coordinator.request_user_action(
            key="disk-track",
            error="permission denied",
            failing_url="https://example.test/disk",
            recovery_scope="output-permission",
            notify=notify,
            cancel_check=lambda: False,
        )
    )
    time.sleep(0.02)
    assert len(notified) == 1

    assert notified[0].recovery_scope == "youtube-auth"
    assert notified[0].resolve(RecoveryDecision.RETRY) is True
    _join_cleanly(first_thread, first_state)

    assert second_notification.wait(timeout=1.0)
    assert len(notified) == 2
    assert notified[1].recovery_scope == "output-permission"
    assert notified[1].resolve(RecoveryDecision.SKIP) is True
    _join_cleanly(second_thread, second_state)

    assert first_state["result"] == RecoveryDecision.RETRY
    assert second_state["result"] == RecoveryDecision.SKIP


def test_active_user_action_blocks_new_start_admission_until_resolved() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)
    notification = threading.Event()
    request_box: list[UserActionRequest] = []

    def notify(request: UserActionRequest) -> None:
        request_box.append(request)
        notification.set()

    action_thread, action_state = _start_call(
        lambda: coordinator.request_user_action(
            key="failed",
            error="needs a decision",
            failing_url="https://example.test/failed",
            notify=notify,
            cancel_check=lambda: False,
        )
    )
    assert notification.wait(timeout=1.0)

    start_thread, start_state = _start_call(
        lambda: coordinator.wait_to_start("next", lambda: False)
    )
    time.sleep(0.02)
    assert start_thread.is_alive()
    assert "result" not in start_state

    assert request_box[0].resolve(RecoveryDecision.SKIP) is True
    _join_cleanly(action_thread, action_state)
    _join_cleanly(start_thread, start_state)
    assert start_state["result"] is not None


def test_user_action_callback_failure_skips_instead_of_stranding_worker() -> None:
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.005)

    def broken_notify(_request: UserActionRequest) -> None:
        raise RuntimeError("UI unavailable")

    decision = coordinator.request_user_action(
        key="track",
        error="failure",
        failing_url="https://example.test/track",
        notify=broken_notify,
        cancel_check=lambda: False,
    )

    assert decision == RecoveryDecision.SKIP
    assert coordinator.active_user_action is None
