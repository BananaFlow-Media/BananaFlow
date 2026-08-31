"""Integration regressions for YouTube admission and request pacing.

All tests are deterministic and network-free.  The real yt-dlp rate-limit
wording is retained verbatim because its overlapping "account" and "video
unavailable" phrases previously obscured the rate-limit classification.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.download_orchestrator import DownloadOrchestrator
from core.download_recovery import (
    DownloadRecoveryCoordinator,
    is_explicit_rate_limit,
    rate_limit_error_from_messages,
)
from core.downloader import (
    DownloadEngine,
    DownloadProgress,
    DownloadRequest,
    DownloadStatus,
    MediaType,
    SilentLogger,
    _combine_failure_evidence,
)
from error_handler import classify_error

REAL_YOUTUBE_RATE_LIMIT = (
    "ERROR: [youtube] 4gVaR9SXlVo: Video unavailable. This content isn't "
    "available, try again later. Your account has been rate-limited by "
    "YouTube for up to an hour. It is recommended to use `-t sleep` to add "
    "a delay between video requests to avoid exceeding the rate limit. For "
    "more information, refer to  https://github.com/yt-dlp/yt-dlp/wiki/"
    "Extractors#this-content-isnt-available-try-again-later"
)


def test_real_rate_limit_is_classified_and_not_duplicated_in_final_error() -> None:
    assert is_explicit_rate_limit(REAL_YOUTUBE_RATE_LIMIT)
    assert classify_error(Exception(REAL_YOUTUBE_RATE_LIMIT)).message_key == (
        "err_rate_limited"
    )
    assert rate_limit_error_from_messages(REAL_YOUTUBE_RATE_LIMIT) is not None

    ytdlp_logger = SilentLogger()
    ytdlp_logger._remember_failure_evidence(REAL_YOUTUBE_RATE_LIMIT)
    combined = _combine_failure_evidence(
        ytdlp_logger.failure_evidence,
        REAL_YOUTUBE_RATE_LIMIT,
    )

    assert combined == REAL_YOUTUBE_RATE_LIMIT
    assert combined.count("Your account has been rate-limited") == 1


@pytest.mark.parametrize(
    ("target", "mode", "expected_fragment_concurrency"),
    [
        ("https://www.youtube.com/watch?v=TESTVIDEOAAA", "fast", 5),
        ("https://www.youtube.com/watch?v=TESTVIDEOAAA", "conservative", 1),
        ("ytsearch1:Artist Track", "fast", 5),
        ("ytsearch1:Artist Track", "conservative", 1),
    ],
)
def test_youtube_url_and_search_targets_use_request_sleep_in_both_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    mode: str,
    expected_fragment_concurrency: int,
) -> None:
    monkeypatch.setattr(
        "core.downloader.get_app_cookies_path",
        lambda: tmp_path / "cookies-not-configured.txt",
    )
    request = DownloadRequest(
        url=target,
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
        youtube_reliability_mode=mode,
    )

    options = DownloadEngine()._build_ydl_opts(request)

    assert options["sleep_interval_requests"] == 0.75
    assert options["concurrent_fragment_downloads"] == expected_fragment_concurrency


class _RecordingCallbacks:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.statuses: list[tuple[str, str]] = []

    def on_track_status(self, key: str, status: str) -> None:
        with self._condition:
            self.statuses.append((key, status))
            self._condition.notify_all()

    def wait_for_status(self, key: str, status: str, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while (key, status) not in self.statuses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


def _finish_request(request: DownloadRequest) -> None:
    assert request.on_finished is not None
    request.on_finished(
        DownloadProgress(
            status=DownloadStatus.FINISHED,
            url=request.url,
            title=request.forced_title or "",
            fraction=1.0,
            output_path=f"{request.forced_title or 'track'}.mp3",
        )
    )


class _RateLimitOnceEngine:
    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.attempts: list[tuple[str, float]] = []
        self.limited_key = ""

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, request: DownloadRequest) -> None:
        key = request.forced_title or request.url
        with self._lock:
            should_limit = not self.limited_key
            if should_limit:
                self.limited_key = key
            self.attempts.append((key, time.monotonic()))

        if should_limit:
            assert request.on_error is not None
            request.on_error(
                DownloadProgress(
                    status=DownloadStatus.ERROR,
                    url=request.url,
                    error_message="HTTP Error 429: Too Many Requests",
                )
            )
            return
        _finish_request(request)


def _youtube_job(key: str, output_dir: Path, *, mode: str) -> tuple[str, DownloadRequest]:
    return (
        key,
        DownloadRequest(
            url=f"https://www.youtube.com/watch?v={key:0<11}"[:43],
            output_dir=str(output_dir),
            media_type=MediaType.AUDIO,
            forced_title=key,
            youtube_reliability_mode=mode,
        ),
    )


def test_rate_limit_cooldown_retries_same_track_then_paces_released_peers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.download_orchestrator._warm_up_yt_dlp_plugins",
        lambda: None,
    )
    cooldown = 0.04
    cadence = 0.04
    coordinator = DownloadRecoveryCoordinator(
        default_rate_limit_wait=cooldown,
        poll_interval=0.002,
    )
    engine = _RateLimitOnceEngine()
    orchestrator = DownloadOrchestrator(
        engine=engine,
        callbacks=_RecordingCallbacks(),
        max_workers=3,
        recovery_coordinator=coordinator,
    )

    result = orchestrator.run_batch(
        [
            _youtube_job("owner", tmp_path, mode="fast"),
            _youtube_job("peer-a", tmp_path, mode="fast"),
            _youtube_job("peer-b", tmp_path, mode="fast"),
        ],
        delay_range=(cadence, cadence),
    )

    assert result.completed == 3
    assert result.failed == 0
    assert len(engine.attempts) == 4

    attempt_keys = [key for key, _started_at in engine.attempts]
    assert attempt_keys[:2] == [engine.limited_key, engine.limited_key]
    assert engine.attempts[1][1] - engine.attempts[0][1] >= cooldown * 0.7

    peer_attempts = [
        attempt for attempt in engine.attempts if attempt[0] != engine.limited_key
    ]
    assert len(peer_attempts) == 2
    assert peer_attempts[0][1] >= engine.attempts[1][1]
    assert peer_attempts[1][1] - peer_attempts[0][1] >= cadence * 0.7


class _BlockingFirstEngine:
    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.starts: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()
        self.release_first.set()

    def download(self, request: DownloadRequest) -> None:
        key = request.forced_title or request.url
        with self._lock:
            self.starts.append(key)
            first = len(self.starts) == 1
        if first:
            self.first_started.set()
            if not self.release_first.wait(timeout=2.0):
                raise AssertionError("test did not release the conservative gate owner")
        _finish_request(request)


def test_waiter_cancels_promptly_behind_conservative_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.download_orchestrator._warm_up_yt_dlp_plugins",
        lambda: None,
    )
    callbacks = _RecordingCallbacks()
    engine = _BlockingFirstEngine()
    orchestrator = DownloadOrchestrator(
        engine=engine,
        callbacks=callbacks,
        max_workers=2,
        recovery_coordinator=DownloadRecoveryCoordinator(poll_interval=0.002),
    )
    state: dict[str, object] = {}

    def run_batch() -> None:
        try:
            state["result"] = orchestrator.run_batch(
                [
                    _youtube_job("first", tmp_path, mode="conservative"),
                    _youtube_job("second", tmp_path, mode="conservative"),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to assertion below
            state["error"] = exc

    thread = threading.Thread(target=run_batch, daemon=True)
    thread.start()
    assert engine.first_started.wait(timeout=1.0)

    first_key = engine.starts[0]
    waiting_key = "second" if first_key == "first" else "first"
    assert callbacks.wait_for_status(waiting_key, "waiting")

    orchestrator.cancel_track(waiting_key)
    assert callbacks.wait_for_status(waiting_key, "cancelled")
    assert engine.starts == [first_key]

    engine.release_first.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "batch did not finish after releasing gate owner"
    assert "error" not in state, state.get("error")
    assert engine.starts == [first_key]


class _ThreeAuthFailuresEngine:
    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._condition = threading.Condition()
        self.starts: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, request: DownloadRequest) -> None:
        key = request.forced_title or request.url
        with self._condition:
            self.starts.append(key)
            number = len(self.starts)
            self._condition.notify_all()
        if number <= 3:
            assert request.on_error is not None
            request.on_error(DownloadProgress(
                status=DownloadStatus.ERROR,
                url=request.url,
                error_message="Sign in to view this video",
            ))
            return
        _finish_request(request)

    def wait_for_starts(self, count: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.starts) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


def test_orchestrator_continues_after_first_two_auth_failures_and_stops_at_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.download_orchestrator._warm_up_yt_dlp_plugins",
        lambda: None,
    )
    coordinator = DownloadRecoveryCoordinator(poll_interval=0.002)
    engine = _ThreeAuthFailuresEngine()
    orchestrator = DownloadOrchestrator(
        engine=engine,
        callbacks=_RecordingCallbacks(),
        max_workers=1,
        recovery_coordinator=coordinator,
    )
    state: dict[str, object] = {}

    def run_batch() -> None:
        state["result"] = orchestrator.run_batch([
            _youtube_job(f"track-{number}", tmp_path, mode="fast")
            for number in range(1, 6)
        ], delay_range=(0.0, 0.0))

    thread = threading.Thread(target=run_batch, daemon=True)
    thread.start()
    assert engine.wait_for_starts(3)
    time.sleep(0.04)

    assert engine.starts == ["track-1", "track-2", "track-3"]
    assert coordinator.systemic_state("youtube_auth") == (3, True)
    assert thread.is_alive()

    coordinator.reset_systemic("youtube_auth")
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    result = state["result"]
    assert result.failed == 3
    assert result.completed == 2
    assert engine.starts == [
        "track-1", "track-2", "track-3", "track-4", "track-5"
    ]
