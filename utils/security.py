"""Security helpers shared by logs, diagnostics, errors, and cookie storage.

This module deliberately has no GUI imports.  Sensitive values are replaced at
the final output boundary, and authentication files are written atomically with
owner-only permissions.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "proxyuser",
    "proxypassword",
    "secret",
    "sessionid",
    "token",
    "apikey",
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # HTTP headers and common logging/dict forms.
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|"
            r"x-app-token|x-api-key)(\s*[:=]\s*)([^\r\n,}]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
            r"refresh[_-]?token|password|passwd|secret|session[_-]?id)[\"']?"
            r"\s*[:=]\s*)([\"'])(.*?)(\2)"
        ),
        rf"\1\2{REDACTED}\2",
    ),
    # Sensitive query-string values, including YouTube's ``?key=`` form.
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth|authorization|"
            r"client[_-]?secret|cookie|key|password|secret|signature|token)=)"
            r"([^&#\s]+)"
        ),
        rf"\1{REDACTED}",
    ),
    # Credentials embedded in a proxy or other URL.
    (
        re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/@\s:]+:)[^/@\s]+(@)"),
        rf"\1{REDACTED}\2",
    ),
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {REDACTED}"),
    # Well-known credential shapes that can appear without a field label.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), REDACTED),
    # Raw high-entropy hexadecimal secrets.  Forty-character Git commit IDs
    # remain visible; 32/64-byte credential material does not.
    (re.compile(r"(?i)\b[0-9a-f]{64,}\b"), REDACTED),
)

_NETSCAPE_COOKIE_LINE = re.compile(
    r"(?m)^([^\t\r\n]+\t(?:TRUE|FALSE)\t[^\t\r\n]*\t(?:TRUE|FALSE)"
    r"\t-?\d+\t[^\t\r\n]+\t)[^\r\n]*$"
)


def _normalise_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping/config field must be hidden in output."""
    normalised = _normalise_key(key)
    return any(part in normalised for part in _SENSITIVE_KEY_PARTS)


def redact_data(value: Any, *, key: object | None = None) -> Any:
    """Recursively redact realistic secrets while preserving container shape."""
    if key is not None and is_sensitive_key(key):
        return REDACTED if value not in (None, "", b"") else value
    if isinstance(value, Mapping):
        return {k: redact_data(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, set):
        return {redact_data(item) for item in value}
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: object) -> str:
    """Return text safe for logs, diagnostics, reports, and UI details."""
    text = str(value)
    text = _NETSCAPE_COOKIE_LINE.sub(rf"\1{REDACTED}", text)
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)

    # Local profile paths are private even when they contain no credential.
    replacements: list[tuple[str, str]] = []
    for env_name, marker in (
        ("APPDATA", "<APP_DATA>"),
        ("LOCALAPPDATA", "<LOCAL_APP_DATA>"),
        ("USERPROFILE", "<USER_HOME>"),
    ):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            replacements.append((raw, marker))
    try:
        replacements.append((str(Path.home()), "<USER_HOME>"))
    except RuntimeError:
        pass
    for raw, marker in sorted(set(replacements), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(re.escape(raw), marker, text, flags=re.IGNORECASE)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the fully rendered record, including tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def restrict_path_permissions(path: str | Path, *, recursive: bool = False) -> None:
    """Restrict ``path`` to the current user, raising if enforcement fails.

    POSIX uses mode 0600/0700.  Windows uses ``icacls`` because ``chmod`` does
    not provide a meaningful owner-only ACL there.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target)

    mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if target.is_dir() else 0)
    os.chmod(target, mode)
    if os.name != "nt":
        if recursive and target.is_dir():
            for child in target.rglob("*"):
                child_mode = stat.S_IRUSR | stat.S_IWUSR
                if child.is_dir():
                    child_mode |= stat.S_IXUSR
                os.chmod(child, child_mode)
        return

    whoami = subprocess.run(
        ["whoami"], capture_output=True, text=True, timeout=10, check=False,
    )
    principal = whoami.stdout.strip()
    if whoami.returncode != 0 or not principal or "\n" in principal or "\r" in principal:
        raise OSError("could not identify the current Windows account for ACL hardening")

    grant = f"{principal}:(OI)(CI)F" if target.is_dir() else f"{principal}:(F)"
    command = ["icacls", str(target), "/inheritance:r", "/grant:r", grant, "/Q"]
    if recursive and target.is_dir():
        command.extend(["/T", "/C"])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode != 0:
        raise OSError("Windows ACL hardening failed")


def write_private_text(path: str | Path, text: str) -> Path:
    """Atomically write UTF-8 authentication data with owner-only access."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    restrict_path_permissions(destination.parent, recursive=False)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        restrict_path_permissions(temporary)
        os.replace(temporary, destination)
        restrict_path_permissions(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


@dataclass(frozen=True)
class AuthDeletionResult:
    removed: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def success(self) -> bool:
        return not self.failed


def delete_stored_auth_data(
    *,
    cookie_path: str | Path | None = None,
    profile_dir: str | Path | None = None,
) -> AuthDeletionResult:
    """Delete only BananaFlow-owned cookies and its dedicated browser profile."""
    if cookie_path is None or profile_dir is None:
        from utils.paths import get_app_browser_profile_dir, get_app_cookies_path

        cookie_path = get_app_cookies_path() if cookie_path is None else cookie_path
        profile_dir = get_app_browser_profile_dir() if profile_dir is None else profile_dir

    cookie = Path(cookie_path)
    profile = Path(profile_dir)
    removed: list[str] = []
    failed: list[str] = []

    try:
        if cookie.exists():
            cookie.unlink()
            removed.append("cookies")
    except OSError:
        failed.append("cookies")

    try:
        if profile.exists():
            shutil.rmtree(profile)
            removed.append("browser_profile")
    except OSError:
        failed.append("browser_profile")

    return AuthDeletionResult(tuple(removed), tuple(failed))
