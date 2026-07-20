"""
utils/cookie_validator.py  –  Netscape cookies.txt freshness checker
=====================================================================
Parses a Netscape-format cookies file and reports whether the session
cookies are still valid (not all expired).

Zero GUI imports — pure stdlib only.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ui.i18n import t
from utils.security import write_private_text

logger = logging.getLogger(__name__)


def _is_youtube_domain(domain: str) -> bool:
    """True only for youtube.com itself or one of its subdomains.

    A substring test would also match unrelated hosts such as
    "youtube.com.evil.example", so the domain column is compared exactly.
    """
    host = domain.strip().lstrip(".").lower()
    return host == "youtube.com" or host.endswith(".youtube.com")


def check_cookies_valid(path: str | Path) -> tuple[bool, str]:
    """
    Parse a Netscape cookies.txt file and check expiry.

    Returns
    -------
    (True, "")
        If the file is valid and at least one non-expired cookie exists.
    (False, warning_message)
        If the file is missing, unreadable, or all cookies have expired.
    """
    p = Path(path)
    if not p.exists():
        return False, t("cookies_file_not_found", path=p)

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, t("cookies_read_error", exc=exc)

    now = time.time()
    total = 0
    expired = 0
    has_login_info = False

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        total += 1
        domain, name = parts[0], parts[5]
        try:
            expiry = int(parts[4])
        except (ValueError, IndexError):
            continue
        # 0 means session cookie (no expiry) — treat as valid
        if expiry == 0:
            if name == "LOGIN_INFO" and _is_youtube_domain(domain):
                has_login_info = True
            continue
        if expiry < now:
            expired += 1
            continue
        if name == "LOGIN_INFO" and _is_youtube_domain(domain):
            has_login_info = True

    if total == 0:
        return False, t("cookies_empty_or_invalid")

    if expired == total:
        return False, t("cookies_all_expired")

    if expired > 0:
        pct = int(expired / total * 100)
        logger.debug("[CookieValidator] %d/%d cookies expired (%d%%)", expired, total, pct)

    # LOGIN_INFO is the marker YouTube sets only for a genuinely signed-in
    # session (visited youtube.com while logged in) — without it, cookies
    # exported while merely signed into a Google account elsewhere still
    # get treated as an anonymous visitor for gated content.
    if not has_login_info:
        return False, t("cookies_missing_login_info")

    return True, ""


_COOKIES_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.haxx.se/rfc/cookie_spec.html\n"
    "# This is a generated file! Do not edit.\n\n"
)


def _parse_cookie_lines(path: Path) -> dict[tuple[str, str, str], str]:
    """Return {(domain, cookie_path, name): raw_line} for a Netscape cookies file."""
    entries: dict[tuple[str, str, str], str] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 7:
            continue
        domain, _include, cpath, _secure, _expiry, name, _value = parts[:7]
        entries[(domain, cpath, name)] = line
    return entries


def merge_cookies_file(source: str | Path, dest: str | Path) -> None:
    """
    Merge Netscape-format cookies from ``source`` into ``dest``, one cookie
    at a time (keyed by domain + path + name).

    * A cookie present in both is replaced by the ``source`` version.
    * A cookie only in ``source`` is added.
    * A cookie only in ``dest`` (e.g. for an unrelated site) is kept as-is.

    Creates ``dest`` (with parent directories) if it doesn't exist yet.
    """
    source = Path(source)
    dest = Path(dest)

    merged = _parse_cookie_lines(dest)
    merged.update(_parse_cookie_lines(source))

    write_private_text(dest, _COOKIES_HEADER + "\n".join(merged.values()) + "\n")
