"""
tests/test_retry_policy.py  –  Retry logic unit tests
======================================================
"""

from __future__ import annotations

import threading

import pytest

from core.downloader import SilentLogger
from core.retry_policy import (
    RetryPolicy,
    is_retriable,
    retry_download,
)
from core.warning_classifier import classify_warning


class TestIsRetriable:

    def test_rate_limit(self):
        assert is_retriable("HTTP Error 429: Too Many Requests") is True

    def test_timeout(self):
        assert is_retriable("Read timed out") is True

    def test_503(self):
        assert is_retriable("503 Service Unavailable") is True

    def test_connection_reset(self):
        assert is_retriable("Connection reset by peer") is True

    def test_private_video_not_retriable(self):
        assert is_retriable("This video is private video") is False

    def test_geo_block_not_retriable(self):
        assert is_retriable("not available in your country") is False

    def test_sign_in_not_retriable(self):
        assert is_retriable("Sign in to confirm your age") is False

    def test_generic_error_not_retriable(self):
        assert is_retriable("Something random happened") is False

    def test_permanent_takes_priority(self):
        # Even if message contains "timeout", "private video" wins
        assert is_retriable("private video timeout") is False

    # ── Reliability-hardening phase 2: auth/PO-token/cookies/403 must not
    # trigger blind repeated retries, while transient network/file-lock
    # errors still can. ──────────────────────────────────────────────────

    def test_po_token_not_retriable(self):
        assert is_retriable("Unable to fetch GVS PO Token") is False

    def test_cookies_invalid_not_retriable(self):
        assert is_retriable("YouTube account cookies are no longer valid") is False

    def test_http_403_not_retriable(self):
        assert is_retriable("HTTP Error 403: Forbidden") is False

    def test_bot_check_not_retriable(self):
        assert is_retriable("Sign in to confirm you're not a bot") is False

    def test_429_still_retriable(self):
        # 429 (rate-limit) is distinct from 403 (forbidden) — still worth
        # a backoff retry.
        assert is_retriable("HTTP Error 429: Too Many Requests") is True

    def test_file_lock_retriable(self):
        assert is_retriable(
            "The process cannot access the file because it is "
            "being used by another process"
        ) is True

    def test_sharing_violation_retriable(self):
        assert is_retriable("[WinError 32] Sharing violation") is True


class TestRetryPolicy:

    def test_delay_exponential(self):
        p = RetryPolicy(base_delay_s=1.0, backoff_factor=2.0, max_delay_s=30.0)
        assert p.delay_for_attempt(0) == 1.0
        assert p.delay_for_attempt(1) == 2.0
        assert p.delay_for_attempt(2) == 4.0
        assert p.delay_for_attempt(3) == 8.0

    def test_delay_capped(self):
        p = RetryPolicy(base_delay_s=10.0, backoff_factor=3.0, max_delay_s=30.0)
        assert p.delay_for_attempt(2) == 30.0  # 10*9=90 → capped to 30


class TestRetryDownload:

    def test_success_no_retry(self):
        calls = [0]
        def fn():
            calls[0] += 1
        result = retry_download(fn, RetryPolicy(max_retries=3))
        assert result is None
        assert calls[0] == 1

    def test_permanent_error_no_retry(self):
        def fn():
            raise Exception("This video is private video")
        result = retry_download(fn, RetryPolicy(max_retries=3), job_key="test")
        assert result is not None
        assert "private" in result

    def test_retriable_error_retries(self):
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 3:
                raise Exception("HTTP Error 429: Too Many Requests")
        policy = RetryPolicy(max_retries=3, base_delay_s=0.01)
        result = retry_download(fn, policy, job_key="test")
        assert result is None  # succeeded on 3rd attempt
        assert calls[0] == 3

    def test_retriable_exhausted(self):
        def fn():
            raise Exception("503 Service Unavailable")
        policy = RetryPolicy(max_retries=2, base_delay_s=0.01)
        result = retry_download(fn, policy, job_key="test")
        assert result is not None
        assert "503" in result

    # ── End-to-end: prove the *actual retry loop* (not just is_retriable())
    # refuses to retry auth/PO-token/cookies/403, and still retries
    # transient network/file-lock errors. ────────────────────────────────

    def test_po_token_error_not_retried_by_loop(self):
        calls = [0]
        def fn():
            calls[0] += 1
            raise Exception("Unable to fetch GVS PO Token")
        result = retry_download(fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="test")
        assert result is not None
        assert calls[0] == 1

    def test_cookies_invalid_error_not_retried_by_loop(self):
        calls = [0]
        def fn():
            calls[0] += 1
            raise Exception("YouTube account cookies are no longer valid")
        result = retry_download(fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="test")
        assert result is not None
        assert calls[0] == 1

    def test_http_403_error_not_retried_by_loop(self):
        calls = [0]
        def fn():
            calls[0] += 1
            raise Exception("HTTP Error 403: Forbidden")
        result = retry_download(fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="test")
        assert result is not None
        assert calls[0] == 1

    def test_temporary_media_transfer_403_gets_one_fresh_bounded_attempt(self):
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise Exception(
                    "unable to download video data: HTTP Error 403: Forbidden"
                )
        result = retry_download(
            fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="dubai"
        )
        assert result is None
        assert calls[0] == 2

    @pytest.mark.parametrize("earlier_evidence, expected_category", [
        (
            "ERROR: Could not copy Chrome cookie database",
            "browser_cookie_access_blocked",
        ),
        ("ERROR: Database is locked", "browser_cookie_access_blocked"),
        ("ERROR: Failed to decrypt with DPAPI", "browser_cookie_access_blocked"),
        (
            "ERROR: App-bound encryption prevented cookie access",
            "browser_cookie_access_blocked",
        ),
        ("ERROR: No supported JavaScript runtime", "js_runtime_missing"),
        (
            "ERROR: PoTokenProviderError: provider configuration is invalid",
            "po_token_missing",
        ),
        ("ERROR: Please sign in to view this video", "account_required"),
        (
            "WARNING: YouTube account cookies are no longer valid",
            "cookies_expired_or_invalid",
        ),
        ("WARNING: Unable to fetch GVS PO Token", "po_token_missing"),
        (
            "ERROR: Sign in to confirm you're not a bot",
            "rate_limited_or_forbidden",
        ),
    ])
    def test_all_permanent_logger_evidence_outranks_later_transfer_403(
        self, earlier_evidence, expected_category,
    ):
        """Use the same combined evidence shape produced by DownloadEngine."""
        ytdlp_logger = SilentLogger()
        ytdlp_logger.warning(earlier_evidence)
        assert classify_warning(earlier_evidence) == expected_category
        assert ytdlp_logger.failure_evidence == earlier_evidence
        combined = (
            f"{ytdlp_logger.failure_evidence} | "
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )
        calls = [0]

        def fn():
            calls[0] += 1
            raise Exception(combined)

        result = retry_download(
            fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="combined",
        )
        assert result is not None
        assert calls[0] == 1

    def test_logger_retained_generic_403_outranks_later_transfer_403(self):
        ytdlp_logger = SilentLogger()
        ytdlp_logger.warning("ERROR: HTTP Error 403: Forbidden")
        combined = (
            f"{ytdlp_logger.failure_evidence} | "
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )
        calls = [0]

        def fn():
            calls[0] += 1
            raise Exception(combined)

        result = retry_download(
            fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="generic-403",
        )
        assert result is not None
        assert calls[0] == 1

    def test_logger_retained_network_transient_still_allows_transfer_retry(self):
        ytdlp_logger = SilentLogger()
        ytdlp_logger.warning("ERROR: Connection reset by peer")
        combined = (
            f"{ytdlp_logger.failure_evidence} | "
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )
        calls = [0]

        def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise Exception(combined)

        result = retry_download(
            fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="network",
        )
        assert result is None
        assert calls[0] == 2

    def test_bot_check_error_not_retried_by_loop(self):
        calls = [0]
        def fn():
            calls[0] += 1
            raise Exception("Sign in to confirm you're not a bot")
        result = retry_download(fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="test")
        assert result is not None
        assert calls[0] == 1

    def test_network_error_retried_by_loop(self):
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 2:
                raise Exception("Connection reset by peer")
        result = retry_download(fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="test")
        assert result is None
        assert calls[0] == 2

    def test_file_lock_error_retried_by_loop(self):
        calls = [0]
        def fn():
            calls[0] += 1
            if calls[0] < 3:
                raise Exception(
                    "The process cannot access the file because it is "
                    "being used by another process"
                )
        result = retry_download(fn, RetryPolicy(max_retries=3, base_delay_s=0.01), job_key="test")
        assert result is None
        assert calls[0] == 3

    def test_cancel_during_backoff(self):
        ev = threading.Event()
        ev.set()  # pre-cancel
        def fn():
            raise Exception("429 rate limited")
        policy = RetryPolicy(max_retries=5, base_delay_s=10.0)
        result = retry_download(fn, policy, cancel_event=ev, job_key="test")
        assert result == "Cancelled"
