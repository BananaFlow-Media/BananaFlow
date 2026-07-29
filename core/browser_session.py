"""Safe browser-session policy and typed failure classification.

BananaFlow never copies, unlocks, decrypts, or modifies a user's live
Chromium profile. Chrome's Windows App-Bound Encryption and profile locking
make that both unreliable and the wrong security boundary. The supported
recovery is an app-owned isolated browser profile or an explicitly imported
cookies.txt file, both stored with owner-only permissions.
"""

from __future__ import annotations

import re
import sys
from enum import Enum


class BrowserSessionFailure(str, Enum):
    UNSUPPORTED_LIVE_PROFILE = "unsupported_live_profile"
    PROFILE_LOCKED = "profile_locked"
    APP_BOUND_ENCRYPTION = "app_bound_encryption"
    PROFILE_MISSING = "profile_missing"
    COOKIES_EXPIRED = "cookies_expired"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    BOT_CHALLENGE = "bot_challenge"
    GEO_RESTRICTED = "geo_restricted"
    MEDIA_UNAVAILABLE = "media_unavailable"
    UNKNOWN = "unknown"


class BrowserCookieAccessError(RuntimeError):
    failure = BrowserSessionFailure.UNSUPPORTED_LIVE_PROFILE


_WINDOWS_CHROMIUM = frozenset({"chrome", "edge", "brave", "chromium"})


def browser_cookie_mode_supported(browser: str, platform: str | None = None) -> bool:
    platform = sys.platform if platform is None else platform
    return not (platform == "win32" and (browser or "").casefold() in _WINDOWS_CHROMIUM)


def require_supported_browser_cookie_mode(
    browser: str, platform: str | None = None,
) -> None:
    if browser_cookie_mode_supported(browser, platform):
        return
    raise BrowserCookieAccessError(
        "BROWSER_COOKIE_UNSUPPORTED: live Chrome, Edge, and Brave profiles "
        "cannot be read safely on Windows; use BananaFlow sign-in or import cookies.txt"
    )


_FAILURE_PATTERNS: tuple[tuple[BrowserSessionFailure, re.Pattern[str]], ...] = (
    (BrowserSessionFailure.UNSUPPORTED_LIVE_PROFILE,
     re.compile(r"browser_cookie_unsupported|cannot be read safely on windows", re.I)),
    (BrowserSessionFailure.PROFILE_LOCKED,
     re.compile(r"could not copy .*cookie database|database is locked|sharing violation|used by another process", re.I)),
    (BrowserSessionFailure.APP_BOUND_ENCRYPTION,
     re.compile(r"app.?bound|failed to decrypt with dpapi|decrypt.*cookie", re.I)),
    (BrowserSessionFailure.PROFILE_MISSING,
     re.compile(r"browser profile.*(?:not found|missing)|could not find.*cookies", re.I)),
    (BrowserSessionFailure.COOKIES_EXPIRED,
     re.compile(r"cookies?.*(?:no longer valid|expired|invalid)", re.I)),
    (BrowserSessionFailure.BOT_CHALLENGE,
     re.compile(r"confirm (?:that )?you(?:'|’| a)?re not a bot|bot challenge|unusual traffic", re.I)),
    (BrowserSessionFailure.RATE_LIMITED,
     re.compile(r"\b429\b|too many requests|rate.?limit|throttl", re.I)),
    (BrowserSessionFailure.GEO_RESTRICTED,
     re.compile(r"(?:not available|unavailable) in (?:your )?(?:country|region)|geo.?restrict", re.I)),
    (BrowserSessionFailure.MEDIA_UNAVAILABLE,
     re.compile(r"private video|video (?:is )?unavailable|has been removed|no longer available", re.I)),
    (BrowserSessionFailure.AUTH_REQUIRED,
     re.compile(r"sign in|login required|age.?gated|members?.only|account required", re.I)),
)


def classify_browser_session_failure(message: str) -> BrowserSessionFailure:
    for failure, pattern in _FAILURE_PATTERNS:
        if pattern.search(message or ""):
            return failure
    return BrowserSessionFailure.UNKNOWN
