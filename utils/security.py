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


def _current_user_sid() -> str:
    """Return the current process token's user SID as ``*S-1-5-...``.

    ``icacls`` accepts a principal either by name or, with a leading
    asterisk, by SID.  The SID is strictly better here:

    * It needs no child process.  The previous implementation shelled out
      to ``whoami.exe`` on every call, and because a windowed build has
      no console, Windows gave each one a brand-new console window that
      flashed on screen.  ACL hardening happens on every startup and on
      every config save, so those flashes came in bursts.
    * It is locale-independent.  A localised Windows install renders
      well-known account *names* in the system language, and matching
      them by string is fragile; a SID never changes.

    Raises OSError if the token cannot be read, so the caller still fails
    closed rather than silently leaving a file world-readable.
    """
    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TokenUser = 1

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Declaring the signatures is mandatory, not tidiness. Without a
    # restype, ctypes assumes ``int`` and truncates the 64-bit pseudo-handle
    # GetCurrentProcess returns, so OpenProcessToken fails with
    # ERROR_INVALID_HANDLE on any 64-bit build.
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HPVOID] if hasattr(
        wintypes, "HPVOID"
    ) else [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise OSError(f"OpenProcessToken failed ({ctypes.get_last_error()})")

    try:
        size = wintypes.DWORD()
        # First call sizes the buffer; it is expected to fail with
        # ERROR_INSUFFICIENT_BUFFER (122).
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TokenUser, buffer, size, ctypes.byref(size)
        ):
            raise OSError(f"GetTokenInformation failed ({ctypes.get_last_error()})")

        # TOKEN_USER is a SID_AND_ATTRIBUTES whose first member is the SID pointer.
        sid_pointer = ctypes.cast(
            buffer, ctypes.POINTER(ctypes.c_void_p)
        ).contents.value
        string_sid = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(string_sid)):
            raise OSError(f"ConvertSidToStringSidW failed ({ctypes.get_last_error()})")
        try:
            value = string_sid.value or ""
        finally:
            kernel32.LocalFree(
                ctypes.cast(string_sid, ctypes.c_void_p)
            )
    finally:
        kernel32.CloseHandle(token)

    if not value.startswith("S-1-"):
        raise OSError("could not identify the current Windows account for ACL hardening")
    return f"*{value}"


_CACHED_PRINCIPAL: str | None = None


def _acl_principal() -> str:
    """Cached principal for ``icacls``.

    The identity of the process owner cannot change while the process
    runs, so this is resolved at most once.  The previous code paid for
    an account lookup on every single hardening call.
    """
    global _CACHED_PRINCIPAL
    if _CACHED_PRINCIPAL is None:
        _CACHED_PRINCIPAL = _current_user_sid()
    return _CACHED_PRINCIPAL


def restrict_path_permissions(path: str | Path, *, recursive: bool = False) -> None:
    """Restrict ``path`` to the current user, raising if enforcement fails.

    POSIX uses mode 0600/0700.  Windows uses ``icacls`` because ``chmod``
    does not provide a meaningful owner-only ACL there.  The ``icacls``
    child is started through :mod:`utils.proc`, so it runs with no
    console window and its exit code and output are logged.
    """
    target = Path(path).resolve()
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

    from utils.proc import run_hidden

    principal = _acl_principal()
    grant = f"{principal}:(OI)(CI)F" if target.is_dir() else f"{principal}:(F)"
    command = ["icacls", str(target), "/inheritance:r", "/grant:r", grant, "/Q"]
    if recursive and target.is_dir():
        command.extend(["/T", "/C"])

    # Authentication paths themselves are sensitive operational data.  The
    # command is intentionally omitted from logs for this ACL-only subprocess.
    result = run_hidden(command, purpose="acl-harden", timeout=60, log_command=False)
    if result.returncode != 0:
        raise OSError("Windows ACL hardening failed")


_HARDENED_DIRECTORIES: set[str] = set()


def harden_directory_once(directory: str | Path) -> None:
    """Apply owner-only permissions to ``directory`` at most once per process.

    A directory's ACL does not drift while the app is running, so
    re-applying it on every write is pure cost.  It used to be a
    *visible* cost: each application spawned ``icacls`` (and, before it,
    ``whoami``), and ``AppConfig.save()`` alone triggered three of them.
    Config is saved whenever a setting or an option-bar control changes,
    so a normal session produced bursts of console windows.
    """
    resolved = str(Path(directory).resolve())
    if resolved in _HARDENED_DIRECTORIES:
        return
    restrict_path_permissions(resolved, recursive=False)
    _HARDENED_DIRECTORIES.add(resolved)


def write_private_text(path: str | Path, text: str) -> Path:
    """Atomically write UTF-8 authentication data with owner-only access."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    harden_directory_once(destination.parent)
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


def write_private_bytes(path: str | Path, payload: bytes) -> Path:
    """Atomically write binary authentication data with owner-only access."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    harden_directory_once(destination.parent)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
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
    default_cookie_path = cookie_path is None
    if cookie_path is None or profile_dir is None:
        from utils.paths import get_app_browser_profile_dir, get_app_cookies_path

        cookie_path = get_app_cookies_path() if cookie_path is None else cookie_path
        profile_dir = get_app_browser_profile_dir() if profile_dir is None else profile_dir

    cookie = Path(cookie_path)
    profile = Path(profile_dir)
    removed: list[str] = []
    failed: list[str] = []

    cookie_targets = [cookie]
    if default_cookie_path:
        from utils.paths import get_legacy_app_cookies_path
        legacy = get_legacy_app_cookies_path()
        if legacy not in cookie_targets:
            cookie_targets.append(legacy)
    cookie_removed = False
    cookie_failed = False
    for target in cookie_targets:
        try:
            if target.exists():
                target.unlink()
                cookie_removed = True
        except OSError:
            cookie_failed = True
    if cookie_removed:
        removed.append("cookies")
    if cookie_failed:
        failed.append("cookies")

    try:
        if profile.exists():
            shutil.rmtree(profile)
            removed.append("browser_profile")
    except OSError:
        failed.append("browser_profile")

    return AuthDeletionResult(tuple(removed), tuple(failed))
