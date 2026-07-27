"""
core/warning_classifier.py  –  Classifies yt-dlp warning/error strings
========================================================================
``core.downloader.SilentLogger`` used to filter several yt-dlp warning
strings out of the log entirely to cut console noise. Some of those
strings (PO Token failures, invalid cookies, missing JS runtime) are not
noise — they are the actual reason a download is about to fail — so they
must stay visible. This module gives them a stable name instead of a
suppress rule, so callers can log/branch on the category rather than
re-matching yt-dlp's prose themselves.

Zero GUI imports; pattern matching only (yt-dlp does not expose a rich
exception hierarchy for these cases).
"""

from __future__ import annotations

import re
from typing import Optional

# ── Category constants ────────────────────────────────────────────────────────
# Plain strings rather than an Enum so callers can log/compare them directly.
PO_TOKEN_MISSING           = "po_token_missing"
COOKIES_EXPIRED_OR_INVALID = "cookies_expired_or_invalid"
JS_RUNTIME_MISSING         = "js_runtime_missing"
RATE_LIMITED_OR_FORBIDDEN  = "rate_limited_or_forbidden"
ACCOUNT_REQUIRED           = "account_required"
NETWORK_TRANSIENT          = "network_transient"

# Checked in order; first match wins. More specific categories (PO token,
# cookies, runtime) are checked before broader ones (account required,
# network) so an ambiguous message like "Sign in to confirm you're not a
# bot" lands on rate_limited_or_forbidden rather than account_required.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"po[ _]?token", re.I), PO_TOKEN_MISSING),
    (
        re.compile(
            r"cookies?.*(no longer valid|expired|invalid)|"
            r"could not copy .*cookie database|failed to decrypt with dpapi",
            re.I,
        ),
        COOKIES_EXPIRED_OR_INVALID,
    ),
    (re.compile(r"no supported javascript runtime", re.I), JS_RUNTIME_MISSING),
    (
        re.compile(
            r"\b403\b|forbidden|\b429\b|too many requests|rate.?limit|not a bot",
            re.I,
        ),
        RATE_LIMITED_OR_FORBIDDEN,
    ),
    (
        re.compile(
            r"sign in|private video|age.?restrict|members?.?only|premium|join this channel",
            re.I,
        ),
        ACCOUNT_REQUIRED,
    ),
    (
        re.compile(
            r"connection reset|connection refused|connection aborted|timed?\s*out|"
            r"name or service not known|temporary failure in name resolution|"
            r"network is unreachable|getaddrinfo failed",
            re.I,
        ),
        NETWORK_TRANSIENT,
    ),
]


def classify_warning(message: str) -> Optional[str]:
    """
    Return one of the module-level category constants for a raw yt-dlp
    warning/error string, or ``None`` if it doesn't match a known category.
    """
    if not message:
        return None
    for pattern, category in _PATTERNS:
        if pattern.search(message):
            return category
    return None
