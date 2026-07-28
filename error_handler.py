"""
error_handler.py  –  Centralised error classification & user-friendly messages
===============================================================================
Responsibilities
----------------
* Translate raw yt-dlp / network / OS exceptions into structured ErrorInfo
  objects with a clear severity, a short headline, and a full detail string.
* Provide a connectivity probe so the UI can distinguish "no internet" from
  "bad URL" or "private video".
* Expose helper functions the GUI can call directly without importing
  exception types from yt-dlp or requests.

Design decisions
----------------
* Zero GUI imports – this module is UI-agnostic.
* All classification is done via string matching on the exception message;
  yt-dlp does not expose a rich exception hierarchy, so pattern matching is
  the only reliable approach.
* The `probe_connectivity` function checks a known-reliable HTTPS endpoint
  with a short timeout to distinguish network failures from service errors.
"""

from __future__ import annotations

import re
import socket
from typing import Optional

from utils.security import redact_data, redact_text

from enum import Enum, auto
from dataclasses import dataclass, field
from core.retry_policy import is_retriable as _is_retriable
from core.warning_classifier import (
    ACCOUNT_REQUIRED,
    BROWSER_COOKIE_ACCESS_BLOCKED,
    COOKIES_EXPIRED_OR_INVALID,
    JS_RUNTIME_MISSING,
    NETWORK_TRANSIENT,
    PO_TOKEN_MISSING,
    RATE_LIMITED_OR_FORBIDDEN,
)


# ──────────────────────────────────────────────────────────────────────────────
# Public data types
# ──────────────────────────────────────────────────────────────────────────────

class ErrorSeverity(Enum):
    WARNING  = auto()   # Non-fatal; operation continues
    ERROR    = auto()   # Operation failed; user should be informed
    CRITICAL = auto()   # Application-level failure (bad config, missing FFmpeg…)


@dataclass
class ErrorInfo:
    """Structured error ready for display.

    ``headline``/``detail`` are always canonical English (this module is
    GUI-free and cannot know the UI language). ``message_key`` + params
    identify the ERROR_TEXTS_EN templates they were rendered from, so
    the UI layer (AppWindow._localized_error_info) can re-render the
    same content in the user's language. ``doctor_key``/``doctor_params``
    carry the YouTube Doctor enrichment (see _enrich_with_doctor) in the
    same translatable form; the English enrichment is already appended
    to ``detail`` for non-UI consumers (logs, CLI).
    """
    severity:  ErrorSeverity
    headline:  str               # Short title for a dialog / status bar
    detail:    str               # Full explanation shown in dialog body
    raw:       str = ""          # Original exception message (for logging)
    retriable: bool = False
    message_key:    str  = ""    # base key; UI uses {key}_title / {key}_detail
    message_params: dict = field(default_factory=dict)
    doctor_key:     str  = ""    # DoctorCheck.message_key of the enrichment
    doctor_params:  dict = field(default_factory=dict)

    def is_fatal(self) -> bool:
        return self.severity == ErrorSeverity.CRITICAL

    def status_line(self) -> str:
        """A one-line summary for a status area. Emoji-free by design: the GUI
        maps ``severity`` to a themed status icon (ui.components.status_icon)
        and renders ``headline`` as plain text; a CLI can prefix its own
        marker. Embedding coloured emoji here would leak into the footer and
        clash with the flat icon set."""
        return self.headline

    def severity_kind(self) -> str:
        """Stable severity token ('warning' | 'error' | 'critical') for the UI
        to map to a status icon without importing the ErrorSeverity enum."""
        return {
            ErrorSeverity.WARNING:  "warning",
            ErrorSeverity.ERROR:    "error",
            ErrorSeverity.CRITICAL: "critical",
        }[self.severity]


# ──────────────────────────────────────────────────────────────────────────────
# Connectivity probe
# ──────────────────────────────────────────────────────────────────────────────

# Probe targets: try each in order; succeed on the first that responds.
_PROBE_TARGETS = [
    ("dns.google",       443),
    ("8.8.8.8",          53),
    ("one.one.one.one",  443),
]


def probe_connectivity(timeout: float = 3.0) -> bool:
    """
    Return True if at least one probe target is reachable.
    Uses a raw TCP connection (no HTTP), so it works even when
    requests / yt-dlp are not installed.
    """
    for host, port in _PROBE_TARGETS:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except OSError:
            continue
    return False


# ──────────────────────────────────────────────────────────────────────────────
# FFmpeg presence check
# ──────────────────────────────────────────────────────────────────────────────

def check_ffmpeg() -> bool:
    """Return True if FFmpeg is available — bundled with the EXE or on PATH.

    Bundled binaries (next to ``bananaflow.exe`` for a frozen build, or
    under ``packaging/ffmpeg/`` during development) take priority over
    a system PATH copy so the EXE always uses its known-good LGPL
    build, never a random GPL build the user happens to have.
    """
    from utils.paths import get_ffmpeg_executable
    return get_ffmpeg_executable() is not None


# ──────────────────────────────────────────────────────────────────────────────
# Pattern tables for yt-dlp error messages
# (checked in order; first match wins)
# ──────────────────────────────────────────────────────────────────────────────

# Canonical English texts — single source for both this module's output
# and the UI's "en" translation table (ui.i18n injects this dict
# verbatim, so the two can never drift). Each error has a stable base
# key; "{key}_title" is the dialog headline, "{key}_detail" the body.
ERROR_TEXTS_EN: dict[str, str] = {
    "err_po_token_title": "PO Token required",
    "err_po_token_detail":
        "YouTube requires a PO Token for this video. Run YouTube Doctor "
        "and update or reinstall BananaFlow if the bundled PO Token Provider "
        "stack is not ready. Source installs should install the po-token "
        "extra and run the provider staging helper.",

    "err_cookies_expired_title": "YouTube cookies expired",
    "err_cookies_expired_detail":
        "Your YouTube cookies appear expired or invalid. Re-export cookies "
        "and try again.",

    "err_browser_cookie_access_title": "Browser cookies cannot be read safely",
    "err_browser_cookie_access_detail":
        "Windows protects and locks live Chrome, Edge, and Brave profiles. "
        "BananaFlow will not bypass those protections. Open the sign-in helper "
        "to use a separate BananaFlow profile, or import a cookies.txt file.",

    "err_bot_challenge_title": "YouTube requested a human verification",
    "err_bot_challenge_detail":
        "YouTube presented an anti-bot challenge. Stop repeated attempts and "
        "wait before trying again. If the content requires your account, use "
        "BananaFlow's sign-in helper; changing videos will not fix this challenge.",

    "err_js_runtime_title": "No JavaScript runtime found",
    "err_js_runtime_detail":
        "No supported JavaScript runtime was found. Install Deno or Node "
        "22+ and run YouTube Doctor again.",

    "err_signin_required_title": "Sign-in required",
    "err_signin_required_detail":
        "This video appears to require login (age-restricted or "
        "account-required). Configure YouTube cookies if you have access "
        "to the content.\n\n"
        "Use BananaFlow's isolated sign-in helper, or import a cookies.txt "
        "file in Settings. BananaFlow does not need access to your regular browser profile.",

    "err_video_unavailable_title": "Video unavailable",
    "err_video_unavailable_detail":
        "This video is private, deleted, or not available in your region.",

    "err_safe_match_not_found_title": "No safe recording match found",
    "err_safe_match_not_found_detail":
        "BananaFlow found search results, but none proved the same artist, "
        "song, duration, and recording version. The track was not downloaded "
        "to avoid silently substituting a cover, live take, remix, or other version.",

    "err_geo_restricted_title": "Geo-restricted content",
    "err_geo_restricted_detail":
        "This content is not available in your country.\n\n"
        "Consider using a VPN or a region-specific cookies file.",

    "err_rate_limited_title": "Rate limited by YouTube",
    "err_rate_limited_detail":
        "YouTube blocked this request or rate-limited it.\n\n"
        "Keep Conservative Mode enabled, wait a few minutes, and avoid "
        "repeated retries — retrying immediately tends to make rate-limiting worse.",

    "err_403_title": "Access denied (403)",
    "err_403_detail":
        "YouTube blocked this request or rate-limited it.\n\n"
        "This usually means the video requires authentication, or automated "
        "requests are being throttled. Keep Conservative Mode enabled, wait, "
        "and avoid repeated retries. Try adding a cookies file in Settings if "
        "you have access to the content.",

    "err_copyright_title": "Content blocked due to copyright",
    "err_copyright_detail":
        "This video has been restricted due to a copyright claim and cannot be downloaded.",

    "err_unsupported_url_title": "Unsupported URL",
    "err_unsupported_url_detail":
        "yt-dlp could not find a supported extractor for this URL.\n\n"
        "Check that the URL is a direct video, playlist, or album link.",

    "err_truncated_url_title": "Incomplete video link",
    "err_truncated_url_detail":
        "This YouTube link is missing characters from its video ID, so it "
        "doesn't point to a real video.\n\n"
        "Copy the full link again (from the address bar or the Share button) "
        "and try again.",

    "err_network_title": "Network error",
    "err_network_detail":
        "A network error occurred while communicating with the server.\n\n"
        "Check your internet connection and try again.",

    "err_ssl_title": "SSL / Certificate error",
    "err_ssl_detail":
        "A secure connection could not be established.\n\n"
        "Your system clock may be wrong, or a firewall is intercepting HTTPS traffic.",

    "err_ffmpeg_missing_title": "FFmpeg not found",
    "err_ffmpeg_missing_detail":
        "yt-dlp requires FFmpeg to merge or convert audio/video.\n\n"
        "Install FFmpeg and make sure it is on your system PATH.\n\n"
        "  Windows : winget install Gyan.FFmpeg\n"
        "  macOS   : brew install ffmpeg\n"
        "  Linux   : sudo apt install ffmpeg",

    "err_disk_permissions_title": "Disk / permissions error",
    "err_disk_permissions_detail":
        "Could not write the downloaded file.\n\n"
        "Either the disk is full or you do not have write permission "
        "to the output folder. Choose a different folder in Settings.",

    # Ad-hoc (non-pattern-table) errors raised in classify_error
    "err_no_internet_title": "No internet connection",
    "err_no_internet_detail":
        "Could not reach the internet.\n\n"
        "Please check your network connection and try again.",

    "err_connection_failed_title": "Connection failed",
    "err_connection_failed_detail":
        "Could not connect to the server.\n\n"
        "The service may be temporarily unavailable.",

    "err_timeout_title": "Request timed out",
    "err_timeout_detail":
        "The server did not respond in time.\n\nTry again in a moment.",

    "err_permission_denied_title": "Permission denied",
    "err_permission_denied_detail":
        "Cannot write to the output folder.\n\n"
        "Choose a different folder in Settings.",

    "err_generic_title": "Download failed",
    "err_generic_detail":
        "An unexpected error occurred:\n\n{short}\n\n"
        "If this persists, check your internet connection and try again.",

    # Prefix for the YouTube Doctor enrichment line appended to detail.
    "err_doctor_prefix": "YouTube Doctor: ",
}


# Each entry: (compiled regex, message_key, severity, code).
#
# ``message_key`` selects the "{key}_title" / "{key}_detail" pair from
# ERROR_TEXTS_EN above. ``code`` is a stable failure category from
# core.warning_classifier (or None where no category applies) —
# Doctor-linking (_enrich_with_doctor) keys off this, never off the
# headline/detail text, so rewording a message can never silently break
# the link.
_YTDLP_PATTERNS: list[tuple[re.Pattern, str, ErrorSeverity, Optional[str]]] = [
    (
        re.compile(r"no identity-safe youtube match|no safe recording match", re.I),
        "err_safe_match_not_found",
        ErrorSeverity.WARNING,
        None,
    ),
    (
        re.compile(
            r"browser_cookie_unsupported|could not copy .*cookie database|"
            r"database is locked|failed to decrypt with dpapi|app.?bound encryption",
            re.I,
        ),
        "err_browser_cookie_access",
        ErrorSeverity.ERROR,
        BROWSER_COOKIE_ACCESS_BLOCKED,
    ),
    (
        re.compile(r"confirm (?:that )?you(?:'|’| a)?re not a bot|bot challenge|unusual traffic", re.I),
        "err_bot_challenge",
        ErrorSeverity.ERROR,
        RATE_LIMITED_OR_FORBIDDEN,
    ),
    # PO Token required — checked first: needs a ready PO Token Provider
    # stack, not cookies or a retry.
    (
        re.compile(r"po[ _]?token", re.I),
        "err_po_token",
        ErrorSeverity.ERROR,
        PO_TOKEN_MISSING,
    ),
    # Cookies present but expired/invalid — checked before the generic
    # "account" sign-in pattern below, which would otherwise catch the
    # word "account" in this message first.
    (
        re.compile(r"cookies?.*(no longer valid|expired|invalid)", re.I),
        "err_cookies_expired",
        ErrorSeverity.ERROR,
        COOKIES_EXPIRED_OR_INVALID,
    ),
    # No supported JS runtime for signature/player solving.
    (
        re.compile(r"no supported javascript runtime", re.I),
        "err_js_runtime",
        ErrorSeverity.ERROR,
        JS_RUNTIME_MISSING,
    ),
    # Age-gated / sign-in required
    (
        re.compile(r"sign in|age.?gated|account|login", re.I),
        "err_signin_required",
        ErrorSeverity.ERROR,
        ACCOUNT_REQUIRED,
    ),
    # Geo-blocked must precede the generic unavailable pattern: yt-dlp can say
    # "This video is unavailable in your country", which is not a stale match.
    (
        re.compile(r"(?:not available|unavailable) in your country|geo.?block|geo.?restrict", re.I),
        "err_geo_restricted",
        ErrorSeverity.ERROR,
        None,
    ),
    # Rate-limited / throttled
    (
        re.compile(r"429|too many requests|rate.?limit|throttl", re.I),
        "err_rate_limited",
        ErrorSeverity.ERROR,
        RATE_LIMITED_OR_FORBIDDEN,
    ),
    # HTTP 403
    (
        re.compile(r"\b403\b|forbidden", re.I),
        "err_403",
        ErrorSeverity.ERROR,
        RATE_LIMITED_OR_FORBIDDEN,
    ),
    # Private / deleted video — evaluated after actionable HTTP evidence. A
    # multi-client yt-dlp attempt can emit 429/403 first and end with the
    # generic phrase "video unavailable"; the concrete transport response
    # must win so users do not retry or change authentication for the wrong
    # reason.
    (
        re.compile(r"private video|video (?:is )?unavailable|has been removed|no longer available", re.I),
        "err_video_unavailable",
        ErrorSeverity.WARNING,
        None,
    ),
    # Copyright / DMCA takedown
    (
        re.compile(r"copyright|dmca|blocked in some countries on copyright", re.I),
        "err_copyright",
        ErrorSeverity.ERROR,
        None,
    ),
    # Truncated video ID (e.g. a copy-pasted link missing its last character)
    (
        re.compile(r"incomplete youtube id|looks truncated", re.I),
        "err_truncated_url",
        ErrorSeverity.WARNING,
        None,
    ),
    # Invalid / unsupported URL
    (
        re.compile(r"unsupported url|no video formats|ie_key|extractor", re.I),
        "err_unsupported_url",
        ErrorSeverity.ERROR,
        None,
    ),
    # Network-level errors surfaced inside yt-dlp
    (
        re.compile(r"connection reset|connection refused|timed? ?out|name or service not known|"
                   r"temporary failure in name resolution|network is unreachable", re.I),
        "err_network",
        ErrorSeverity.ERROR,
        NETWORK_TRANSIENT,
    ),
    # SSL
    (
        re.compile(r"ssl|certificate", re.I),
        "err_ssl",
        ErrorSeverity.ERROR,
        None,
    ),
    # FFmpeg missing (detected inside yt-dlp)
    (
        re.compile(r"ffmpeg|ffprobe|postprocessor", re.I),
        "err_ffmpeg_missing",
        ErrorSeverity.CRITICAL,
        None,
    ),
    # Disk full / permissions
    (
        re.compile(r"no space left|permission denied|read.?only", re.I),
        "err_disk_permissions",
        ErrorSeverity.CRITICAL,
        None,
    ),
]


def _make_error(
    message_key: str,
    severity: ErrorSeverity,
    raw: str = "",
    *,
    retriable: bool = False,
    message_params: Optional[dict] = None,
    doctor_key: str = "",
    doctor_params: Optional[dict] = None,
    detail_suffix: str = "",
) -> ErrorInfo:
    """Build an ErrorInfo whose English text is rendered from
    ERROR_TEXTS_EN while carrying the key+params for UI translation."""
    params = redact_data(dict(message_params or {}))
    safe_raw = redact_text(raw)
    return ErrorInfo(
        severity=severity,
        headline=ERROR_TEXTS_EN[f"{message_key}_title"].format(**params),
        detail=ERROR_TEXTS_EN[f"{message_key}_detail"].format(**params) + detail_suffix,
        raw=safe_raw,
        retriable=retriable,
        message_key=message_key,
        message_params=params,
        doctor_key=doctor_key,
        doctor_params=redact_data(dict(doctor_params or {})),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main classifier
# ──────────────────────────────────────────────────────────────────────────────

def classify_error(
    exc: Exception,
    *,
    cookies_file: str = "",
    cookies_browser: str = "",
) -> ErrorInfo:
    """
    Convert any exception raised during fetch/download into an ErrorInfo.

    Handles:
    - yt-dlp.utils.DownloadError  (most common)
    - requests.exceptions.*
    - OSError / PermissionError
    - Any other Exception (generic fallback)

    ``cookies_file``/``cookies_browser`` are optional and only used to
    enrich a handful of YouTube-specific messages (PO Token, JS runtime,
    sign-in) with a concrete finding from the existing offline YouTube
    Doctor checks (core.youtube_doctor) — see _enrich_with_doctor(). No
    network calls are made either way.
    """
    raw_msg = str(exc)

    # ── yt-dlp DownloadError ──────────────────────────────────────────────────
    # yt-dlp wraps its errors in DownloadError with a verbose message string.
    # We pattern-match on the string because yt-dlp doesn't expose sub-types.
    try:
        import yt_dlp.utils as _ydl_utils
        if isinstance(exc, _ydl_utils.DownloadError):
            return _match_patterns(raw_msg, cookies_file=cookies_file, cookies_browser=cookies_browser)
    except ImportError:
        pass

    # ── requests exceptions ───────────────────────────────────────────────────
    try:
        import requests.exceptions as _req_exc
        if isinstance(exc, _req_exc.ConnectionError):
            if not probe_connectivity():
                return _make_error("err_no_internet", ErrorSeverity.ERROR, raw_msg)
            return _make_error("err_connection_failed", ErrorSeverity.ERROR, raw_msg)
        if isinstance(exc, _req_exc.Timeout):
            return _make_error("err_timeout", ErrorSeverity.ERROR, raw_msg)
        if isinstance(exc, _req_exc.HTTPError):
            return _match_patterns(raw_msg, cookies_file=cookies_file, cookies_browser=cookies_browser)
    except ImportError:
        pass

    # ── OS / file system ──────────────────────────────────────────────────────
    if isinstance(exc, PermissionError):
        return _make_error("err_permission_denied", ErrorSeverity.CRITICAL, raw_msg)
    if isinstance(exc, OSError):
        return _match_patterns(
            raw_msg, default_severity=ErrorSeverity.CRITICAL,
            cookies_file=cookies_file, cookies_browser=cookies_browser,
        )

    # ── Catch-all: still try pattern matching on the message ──────────────────
    return _match_patterns(raw_msg, cookies_file=cookies_file, cookies_browser=cookies_browser)


# Stable failure codes (from core.warning_classifier — see _YTDLP_PATTERNS
# above) for which an offline YouTube Doctor check can give the user a more
# specific answer than the static message alone. Maps code -> which
# core.youtube_doctor check to consult. Deliberately keyed by code, not by
# headline/detail text, so rewording a message can never silently break
# this link.
_DOCTOR_LINKED_CODES = {
    PO_TOKEN_MISSING: "po_token_provider",
    JS_RUNTIME_MISSING: "js_runtime",
    ACCOUNT_REQUIRED: "cookies",
}


def _enrich_with_doctor(
    code: Optional[str],
    *,
    cookies_file: str,
    cookies_browser: str,
):
    """
    Return the YouTube Doctor check whose finding should be appended to
    the error detail for the handful of failure codes in
    _DOCTOR_LINKED_CODES, or None when there is nothing to add. Fully
    offline — reuses core.youtube_doctor's existing checks, never runs a
    network probe or re-derives cookie/runtime logic. The caller renders
    the English suffix; the check's message_key/params travel on the
    ErrorInfo so the UI can render the same finding translated.
    """
    doctor_category = _DOCTOR_LINKED_CODES.get(code)
    if doctor_category is None:
        return None

    from core.youtube_doctor import (
        DoctorStatus,
        check_cookies,
        check_js_runtimes,
        check_po_token_provider,
    )

    if doctor_category == "po_token_provider":
        check, _detections = check_po_token_provider()
        # Never suppress: whether a provider is detected or not, that
        # fact changes the guidance (see check_po_token_provider's two
        # distinct messages) — unlike js_runtime/cookies below, a PASS
        # here isn't "nothing to add", it's a different thing to say.
        suppress = False
    elif doctor_category == "js_runtime":
        check, _statuses = check_js_runtimes()
        suppress = check.status == DoctorStatus.PASS
    else:  # "cookies"
        check, diag = check_cookies(cookies_file, cookies_browser)
        # Worth mentioning for a sign-in-required failure even when
        # Doctor's own severity is PASS: "no cookies configured" is only
        # informational on its own, but directly actionable here.
        suppress = check.status == DoctorStatus.PASS and diag.mode != "none"

    return None if suppress else check


def _match_patterns(
    raw_msg: str,
    default_severity: ErrorSeverity = ErrorSeverity.ERROR,
    *,
    cookies_file: str = "",
    cookies_browser: str = "",
) -> ErrorInfo:
    """Apply the pattern table; return a generic ErrorInfo if nothing matches."""
    for pattern, message_key, severity, code in _YTDLP_PATTERNS:
        if pattern.search(raw_msg):
            doctor_check = _enrich_with_doctor(
                code,
                cookies_file=cookies_file, cookies_browser=cookies_browser,
            )
            detail_suffix = ""
            doctor_key = ""
            doctor_params: dict = {}
            if doctor_check is not None:
                prefix = ERROR_TEXTS_EN["err_doctor_prefix"]
                detail_suffix = f"\n\n{prefix}{doctor_check.message}"
                doctor_key = doctor_check.message_key
                doctor_params = doctor_check.message_params
            return _make_error(
                message_key, severity, raw_msg,
                retriable=_is_retriable(raw_msg),
                doctor_key=doctor_key,
                doctor_params=doctor_params,
                detail_suffix=detail_suffix,
            )
    # Generic fallback
    return _make_error(
        "err_generic", default_severity, raw_msg,
        retriable=_is_retriable(raw_msg),
        message_params={"short": raw_msg[:200]},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pre-flight checks  (called once at startup from main.py and by `cli --doctor`)
# ──────────────────────────────────────────────────────────────────────────────


def check_playwright() -> bool:
    """Return True if the playwright Chromium browser is available.

    Playwright is an optional runtime dependency used by the channel
    scraper, cookie wizard, and universal stream extractor. Official
    packaged builds bundle the Chromium browser (~300 MB) inside the
    app; source installs run ``playwright install chromium`` once
    (see scripts/install_playwright.ps1).

    A missing browser is a warning, not a fatal error: most download
    flows do not need Playwright.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            from pathlib import Path
            return bool(exe) and Path(exe).exists()
    except Exception:
        return False


def check_output_dir_writable(path: str) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for the configured download directory.

    Used by both the GUI preflight and the CLI ``--doctor`` so the
    user sees the same diagnostic on both paths. Creates the
    directory if it does not exist (matches DownloadController
    behaviour); the failure mode is permission denied.
    """
    from pathlib import Path
    try:
        p = Path(path).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        # Round-trip a small file to confirm write permission.
        probe = p / ".bananaflow_write_probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return True, str(p)
    except (OSError, PermissionError) as exc:
        return False, f"{path!r}: {exc}"


def check_cookies_file_valid(path: str) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for the configured cookies.txt.

    Empty path = nothing configured = treated as OK (None is a valid
    user choice). Returns False only when the file is set but
    unreadable or malformed.
    """
    if not path:
        return True, "no cookies file configured"
    try:
        from utils.cookie_validator import check_cookies_valid
        ok, msg = check_cookies_valid(path)
        return bool(ok), msg or ("OK" if ok else "cookies file invalid")
    except Exception as exc:
        return False, f"cookies validator raised: {exc}"


# Canonical English texts for startup preflight warnings — single source
# for both this module's ``warning_text()`` (CLI / log output) and the
# UI's "en" translation table (ui.i18n injects this dict verbatim, so the
# two can never drift). Shell commands and package names are left as-is;
# they are correct in any language.
PREFLIGHT_TEXTS_EN: dict[str, str] = {
    "preflight_ffmpeg_missing": (
        "⚠  FFmpeg was not found on your PATH.\n\n"
        "Audio/video conversion and thumbnail embedding will not work.\n\n"
        "If you installed BananaFlow via the official EXE, FFmpeg should be\n"
        "bundled in the app folder. If you are running from source:\n"
        "  Windows : winget install Gyan.FFmpeg\n"
        "  macOS   : brew install ffmpeg\n"
        "  Linux   : sudo apt install ffmpeg\n\n"
        "Then restart BananaFlow."
    ),
    "preflight_no_internet": (
        "⚠  No internet connection detected.\n\n"
        "Fetching metadata and downloading will fail until the connection is restored."
    ),
    "preflight_output_dir_not_writable": (
        "⚠  The configured download folder is not writable:\n{detail}\n\n"
        "Choose a different folder in Settings or check permissions."
    ),
    "preflight_cookies_invalid": (
        "⚠  The configured cookies.txt is invalid or unreadable:\n{detail}\n\n"
        "Re-export cookies or clear the cookies file in Settings."
    ),
    "preflight_playwright_missing": (
        "ℹ  Playwright Chromium is not installed.\n\n"
        "Most downloads work without it, but these features are disabled:\n"
        "  • Channel and artist discography scraping\n"
        "  • Cookie sign-in wizard\n"
        "  • Universal stream extractor (generic video sites)\n\n"
        "Run `python -m playwright install chromium` from the install folder\n"
        "(or use the bundled `scripts/install_playwright.ps1`) to enable them."
    ),
}


@dataclass
class PreflightWarning:
    """One startup warning, keyed for UI translation.

    ``key`` + ``params`` select a PREFLIGHT_TEXTS_EN template exactly the
    way DoctorCheck/ErrorInfo carry message_key/message_params — the GUI
    renders ``t(key, **params)`` in the active language; ``render()``
    gives the canonical English text for CLI/log output.
    """
    key:    str
    params: dict = field(default_factory=dict)

    def render(self) -> str:
        return PREFLIGHT_TEXTS_EN[self.key].format(**self.params)


@dataclass
class PreflightResult:
    ffmpeg_ok:      bool
    network_ok:     bool
    output_dir_ok:  bool
    cookies_ok:     bool
    playwright_ok:  bool
    warnings:       list[PreflightWarning]
    # Per-check detail lines for the --doctor CLI output. The GUI uses
    # ``warnings`` for the MessageBox; --doctor prints details too.
    details:        list[str]

    def all_ok(self) -> bool:
        # Playwright is optional; not having it is a warning, not a
        # blocker. Cookies validity is informational unless the user
        # explicitly configured a file (handled in run_preflight).
        return self.ffmpeg_ok and self.network_ok and self.output_dir_ok

    def warning_text(self) -> str:
        """Canonical English text — used by the CLI and as a log fallback
        if the GUI can't be shown. The GUI itself renders each warning
        via ``t(warning.key, **warning.params)`` (see ui.i18n.
        render_preflight_warnings) so this never needs to be Hebrew."""
        return "\n\n".join(w.render() for w in self.warnings)

    def detail_text(self) -> str:
        return "\n".join(self.details)


def run_preflight(
    output_dir: str = "",
    cookies_file: str = "",
) -> PreflightResult:
    """
    Run startup checks. Returns a PreflightResult the GUI can inspect.
    Does NOT raise – all failures are captured into the result.

    Parameters
    ----------
    output_dir   : Optional path to the configured download folder. When
                   provided, writability is checked. Empty string skips
                   the check (used by the CLI before any download).
    cookies_file : Optional path to a cookies.txt. When non-empty, the
                   file is validated (existence + minimal Netscape
                   header). Empty string skips the check.
    """
    warnings: list[PreflightWarning] = []
    details: list[str] = []

    from utils.paths import get_bundled_ffmpeg_dir, get_ffmpeg_executable

    ffmpeg_exe = get_ffmpeg_executable()
    bundled_ffmpeg_dir = get_bundled_ffmpeg_dir()
    ffmpeg_ok = ffmpeg_exe is not None
    if ffmpeg_ok and bundled_ffmpeg_dir is not None:
        details.append(f"FFmpeg          : OK (bundled: {ffmpeg_exe})")
    elif ffmpeg_ok:
        details.append(f"FFmpeg          : OK (PATH: {ffmpeg_exe})")
    else:
        details.append("FFmpeg          : MISSING")
    if not ffmpeg_ok:
        warnings.append(PreflightWarning("preflight_ffmpeg_missing"))

    network_ok = probe_connectivity()
    details.append(f"Network         : {'OK' if network_ok else 'OFFLINE'}")
    if not network_ok:
        warnings.append(PreflightWarning("preflight_no_internet"))

    output_dir_ok = True
    if output_dir:
        output_dir_ok, output_detail = check_output_dir_writable(output_dir)
        details.append(
            f"Output directory: {'OK' if output_dir_ok else 'NOT WRITABLE'}  ({output_detail})"
        )
        if not output_dir_ok:
            warnings.append(PreflightWarning(
                "preflight_output_dir_not_writable", {"detail": output_detail},
            ))

    cookies_ok = True
    if cookies_file:
        cookies_ok, cookies_detail = check_cookies_file_valid(cookies_file)
        details.append(
            f"Cookies file    : {'OK' if cookies_ok else 'INVALID'}  ({cookies_detail})"
        )
        if not cookies_ok:
            warnings.append(PreflightWarning(
                "preflight_cookies_invalid", {"detail": cookies_detail},
            ))
    else:
        details.append("Cookies file    : not configured (optional)")

    playwright_ok = check_playwright()
    details.append(
        f"Playwright      : {'OK' if playwright_ok else 'NOT INSTALLED'} "
        "(needed for channel scraping, cookie wizard, universal extractor)"
    )
    if not playwright_ok:
        warnings.append(PreflightWarning("preflight_playwright_missing"))

    return PreflightResult(
        ffmpeg_ok=ffmpeg_ok,
        network_ok=network_ok,
        output_dir_ok=output_dir_ok,
        cookies_ok=cookies_ok,
        playwright_ok=playwright_ok,
        warnings=warnings,
        details=details,
    )
