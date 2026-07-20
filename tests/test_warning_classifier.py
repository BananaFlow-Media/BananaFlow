"""
tests/test_warning_classifier.py  –  yt-dlp warning classification
======================================================================
"""

from __future__ import annotations

import pytest

from core.warning_classifier import (
    ACCOUNT_REQUIRED,
    COOKIES_EXPIRED_OR_INVALID,
    JS_RUNTIME_MISSING,
    NETWORK_TRANSIENT,
    PO_TOKEN_MISSING,
    RATE_LIMITED_OR_FORBIDDEN,
    classify_warning,
)


class TestClassifyWarning:

    @pytest.mark.parametrize("message, expected", [
        ("WARNING: Unable to fetch GVS PO Token", PO_TOKEN_MISSING),
        ("ERROR: [youtube] po_token verification failed", PO_TOKEN_MISSING),
        ("WARNING: YouTube account cookies are no longer valid", COOKIES_EXPIRED_OR_INVALID),
        ("Cookies are expired, please re-export", COOKIES_EXPIRED_OR_INVALID),
        ("ERROR: No supported JavaScript runtime could be found", JS_RUNTIME_MISSING),
        ("HTTP Error 403: Forbidden", RATE_LIMITED_OR_FORBIDDEN),
        ("HTTP Error 429: Too Many Requests", RATE_LIMITED_OR_FORBIDDEN),
        ("ERROR: Sign in to confirm you’re not a bot", RATE_LIMITED_OR_FORBIDDEN),
        ("ERROR: Sign in to confirm you're not a bot", RATE_LIMITED_OR_FORBIDDEN),
        ("ERROR: Private video. Sign in if you've been granted access", ACCOUNT_REQUIRED),
        ("This video is age-restricted", ACCOUNT_REQUIRED),
        ("Join this channel to get access to members-only content", ACCOUNT_REQUIRED),
        ("Connection reset by peer", NETWORK_TRANSIENT),
        ("Read timed out", NETWORK_TRANSIENT),
        ("Temporary failure in name resolution", NETWORK_TRANSIENT),
    ])
    def test_known_categories(self, message, expected):
        assert classify_warning(message) == expected

    def test_unrelated_message_returns_none(self):
        assert classify_warning("Some totally unrelated informational note") is None

    def test_empty_string_returns_none(self):
        assert classify_warning("") is None

    def test_none_safe(self):
        assert classify_warning(None) is None

    def test_bot_detection_wins_over_account_required(self):
        # "Sign in to confirm you're not a bot" contains "sign in" (which
        # would match ACCOUNT_REQUIRED on its own) but is really a
        # rate-limit/bot-check response, not a login-required video.
        msg = "ERROR: [youtube] abc123: Sign in to confirm you're not a bot"
        assert classify_warning(msg) == RATE_LIMITED_OR_FORBIDDEN
