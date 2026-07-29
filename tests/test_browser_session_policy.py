from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.browser_session import (
    BrowserCookieAccessError,
    BrowserSessionFailure,
    browser_cookie_mode_supported,
    classify_browser_session_failure,
    require_supported_browser_cookie_mode,
)
from error_handler import classify_error
from utils.yt_dlp_opts import build_base_ydl_opts


@pytest.mark.parametrize("browser", ["chrome", "edge", "brave", "chromium"])
def test_windows_chromium_live_profiles_are_refused_before_ytdlp(browser):
    assert not browser_cookie_mode_supported(browser, "win32")
    with pytest.raises(BrowserCookieAccessError):
        require_supported_browser_cookie_mode(browser, "win32")


def test_firefox_and_non_windows_chromium_remain_supported():
    assert browser_cookie_mode_supported("firefox", "win32")
    assert browser_cookie_mode_supported("chrome", "darwin")
    assert browser_cookie_mode_supported("chrome", "linux")


def test_builder_fails_closed_for_windows_chromium(monkeypatch):
    host_platform = sys.platform
    # Replace this module's reference instead of mutating the process-wide
    # sys.platform object. On Linux/Python 3.12, changing the shared object
    # makes shutil.which enter a Windows-only branch without _winapi.
    monkeypatch.setattr(
        "core.browser_session.sys", SimpleNamespace(platform="win32")
    )
    with pytest.raises(BrowserCookieAccessError):
        build_base_ydl_opts(cookies_browser="edge", enable_po_token_provider=False)
    assert sys.platform == host_platform


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Could not copy Chrome cookie database: used by another process", BrowserSessionFailure.PROFILE_LOCKED),
        ("Failed to decrypt with DPAPI (App-Bound Encryption)", BrowserSessionFailure.APP_BOUND_ENCRYPTION),
        ("cookies are no longer valid", BrowserSessionFailure.COOKIES_EXPIRED),
        ("Sign in to confirm you're not a bot", BrowserSessionFailure.BOT_CHALLENGE),
        ("HTTP Error 429: Too Many Requests", BrowserSessionFailure.RATE_LIMITED),
        ("This video is unavailable in your country", BrowserSessionFailure.GEO_RESTRICTED),
        ("This video is unavailable", BrowserSessionFailure.MEDIA_UNAVAILABLE),
        ("Login required for members-only content", BrowserSessionFailure.AUTH_REQUIRED),
    ],
)
def test_failure_classification_is_action_specific(message, expected):
    assert classify_browser_session_failure(message) is expected


def test_user_facing_classifier_separates_bot_auth_rate_and_unavailable():
    cases = {
        "Sign in to confirm you're not a bot": "err_bot_challenge",
        "Login required": "err_signin_required",
        "HTTP Error 429": "err_rate_limited",
        "This video is unavailable in your country": "err_geo_restricted",
        "This video is unavailable": "err_video_unavailable",
        "BROWSER_COOKIE_UNSUPPORTED: cannot be read safely on Windows": "err_browser_cookie_access",
    }
    assert {
        classify_error(RuntimeError(raw)).message_key for raw in cases
    } == set(cases.values())
