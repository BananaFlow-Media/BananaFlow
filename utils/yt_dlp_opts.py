"""
utils/yt_dlp_opts.py  –  Shared yt-dlp configuration builder
=============================================================
Centralises every yt-dlp option that is common across the three backend
modules that call yt-dlp (downloader, playlist_parser, search_engine):

  * Native Cookie injection (file or automatic browser extraction)
  * Robust retry counts + extractor retries
  * Optional HTTP proxy for all requests
  
Design
------
* Zero GUI imports – pure stdlib + yt-dlp.
* Returns a plain dict so callers can merge or override individual keys.
* Caller is responsible for adding module-specific keys (format, outtmpl,
  progress_hooks, postprocessors, skip_download, extract_flat, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from utils.security import restrict_path_permissions

logger = logging.getLogger(__name__)

CHROME_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

# Preference order for the JS runtime yt-dlp uses to run YouTube's player
# logic (signature/n-challenge/EJS solving). Deno is the most actively
# maintained target for yt-dlp's EJS solver, so it goes first. Node is
# accepted only from 22+ (yt-dlp itself only requires 20+, but older 20/21
# builds have shown flakier EJS behavior in practice). QuickJS is a small,
# dependency-free fallback. Bun is deliberately not auto-selected — it is
# not a recommended default runtime for yt-dlp's YouTube player logic.
_JS_RUNTIMES_PREFERENCE = ("deno", "node", "quickjs")

_JS_RUNTIME_EXE_NAMES = {
    "deno": "deno",
    "node": "node",
    "quickjs": "qjs",
}

_NODE_MIN_MAJOR_VERSION = 22

_POT_FAILURE_THRESHOLD = 2
_POT_CIRCUIT_COOLDOWN_SECONDS = 300.0
_pot_circuit_lock = threading.Lock()
_pot_failure_count = 0
_pot_circuit_open_until = 0.0
_pot_attempts_total = 0
_pot_diagnostic_messages = 0
_pot_first_diagnostic_logged = False
_bgutil_stderr_capture_installed = False
_cookie_diagnostic_messages = 0
_diagnostic_scope_local = threading.local()
_diagnostic_scope_counts: dict[str, dict[str, int]] = {}


@contextmanager
def diagnostic_scope(scope_id: Optional[str]) -> Iterator[None]:
    """Attribute diagnostics emitted on this thread to one batch/run."""
    previous = getattr(_diagnostic_scope_local, "scope_id", None)
    _diagnostic_scope_local.scope_id = scope_id
    try:
        yield
    finally:
        if previous is None:
            try:
                del _diagnostic_scope_local.scope_id
            except AttributeError:
                pass
        else:
            _diagnostic_scope_local.scope_id = previous


def _note_diagnostic_scope_locked(metric: str) -> None:
    scope_id = getattr(_diagnostic_scope_local, "scope_id", None)
    if not scope_id:
        return
    bucket = _diagnostic_scope_counts.setdefault(
        scope_id,
        {"attempts": 0, "po_diagnostics": 0, "cookie_diagnostics": 0},
    )
    bucket[metric] += 1


def diagnostic_scope_metrics(
    scope_id: Optional[str], *, clear: bool = False,
) -> dict[str, int]:
    """Return diagnostics owned by one run, never another concurrent run."""
    empty = {"attempts": 0, "po_diagnostics": 0, "cookie_diagnostics": 0}
    if not scope_id:
        return empty
    with _pot_circuit_lock:
        result = dict(_diagnostic_scope_counts.get(scope_id, empty))
        if clear:
            _diagnostic_scope_counts.pop(scope_id, None)
        return result


def reset_po_token_provider_circuit() -> None:
    """Reset process-wide PO-token telemetry (test/support hook only)."""
    global _pot_failure_count, _pot_circuit_open_until
    global _pot_attempts_total, _pot_diagnostic_messages, _pot_first_diagnostic_logged
    global _cookie_diagnostic_messages
    with _pot_circuit_lock:
        _pot_failure_count = 0
        _pot_circuit_open_until = 0.0
        _pot_attempts_total = 0
        _pot_diagnostic_messages = 0
        _pot_first_diagnostic_logged = False
        _cookie_diagnostic_messages = 0
        _diagnostic_scope_counts.clear()


def _circuit_open_locked(now: float) -> bool:
    global _pot_failure_count, _pot_circuit_open_until
    if _pot_circuit_open_until and now >= _pot_circuit_open_until:
        _pot_failure_count = 0
        _pot_circuit_open_until = 0.0
    return _pot_circuit_open_until > now


def po_token_provider_circuit_open() -> bool:
    with _pot_circuit_lock:
        return _circuit_open_locked(time.monotonic())


def note_po_token_provider_attempt_failure() -> bool:
    """Record one failed bgutil process invocation and maybe open its breaker.

    This is deliberately the sole authoritative failure counter. yt-dlp can
    rephrase one subprocess failure as several warnings/errors, so those
    messages are diagnostic evidence only and must not spend attempt budget.
    The breaker is process-wide with a cooldown: concurrent batches cannot
    reset or accidentally re-enable a provider that has just failed.
    """
    global _pot_failure_count, _pot_circuit_open_until, _pot_attempts_total
    now = time.monotonic()
    with _pot_circuit_lock:
        if _circuit_open_locked(now):
            return False
        _pot_failure_count += 1
        _pot_attempts_total += 1
        _note_diagnostic_scope_locked("attempts")
        if _pot_failure_count < _POT_FAILURE_THRESHOLD:
            return False
        _pot_circuit_open_until = now + _POT_CIRCUIT_COOLDOWN_SECONDS
    logger.warning(
        "[yt-dlp][po_token] bgutil provider failed %d actual process attempts; disabling it for %.0fs",
        _POT_FAILURE_THRESHOLD, _POT_CIRCUIT_COOLDOWN_SECONDS,
    )
    return True


def note_po_token_provider_failure(message: str) -> bool:
    """Compatibility wrapper for explicit callers that observed an attempt."""
    lower = (message or "").lower()
    if not any(token in lower for token in (
        "potokenprovidererror", "failed while generating pot",
        "failed to generate an integrity token", "unable to fetch gvs po token",
        "po_token_missing",
    )):
        return False
    return note_po_token_provider_attempt_failure()


def note_po_token_provider_diagnostic(message: str) -> bool:
    """Count a yt-dlp PO-token message; return True only for the first one."""
    global _pot_diagnostic_messages, _pot_first_diagnostic_logged
    lower = (message or "").lower()
    if not any(token in lower for token in (
        "potokenprovidererror", "failed while generating pot",
        "failed to generate an integrity token", "unable to fetch gvs po token",
        "po_token_missing", "po token",
    )):
        return False
    with _pot_circuit_lock:
        _pot_diagnostic_messages += 1
        _note_diagnostic_scope_locked("po_diagnostics")
        first = not _pot_first_diagnostic_logged
        _pot_first_diagnostic_logged = True
        return first


def po_token_provider_metrics() -> dict[str, object]:
    """A monotonic telemetry snapshot for batch summaries and diagnostics."""
    with _pot_circuit_lock:
        now = time.monotonic()
        return {
            "attempts": _pot_attempts_total,
            "diagnostics": _pot_diagnostic_messages,
            "circuit_open": _circuit_open_locked(now),
            "cooldown_remaining": max(0.0, _pot_circuit_open_until - now),
        }


def note_cookie_diagnostic(message: str) -> bool:
    """Record an expired/invalid-cookie diagnostic without echoing its prose.

    yt-dlp can emit this once per request and the preflight validator can emit
    the same result once per job.  The batch owner reports one actionable
    summary at completion; detailed copies remain available only at debug.
    Returns whether *message* belongs to this coalesced category.
    """
    global _cookie_diagnostic_messages
    if not re.search(
        r"cookies?.*(no longer valid|expired|invalid)|"
        r"could not copy .*cookie database|failed to decrypt with dpapi",
        message or "",
        re.I,
    ):
        return False
    with _pot_circuit_lock:
        _cookie_diagnostic_messages += 1
        _note_diagnostic_scope_locked("cookie_diagnostics")
    return True


def cookie_diagnostic_metrics() -> dict[str, int]:
    """Return a monotonic count for batch-scoped cookie-warning summaries."""
    with _pot_circuit_lock:
        return {"diagnostics": _cookie_diagnostic_messages}


def _is_bgutil_script_command(command: object) -> bool:
    if not isinstance(command, (list, tuple)):
        return False
    return any(
        str(part).endswith(("generate_once.ts", "generate_once.js"))
        for part in command
    )


def install_bgutil_stderr_capture() -> None:
    """Capture bgutil child stderr instead of letting it flood the console.

    The third-party provider invokes ``yt_dlp.utils.Popen.run`` without a
    ``stderr`` argument, which inherits the app console.  Wrap only the two
    bgutil script commands; all other yt-dlp subprocesses retain their exact
    upstream behaviour. Details stay in debug logging. A non-zero bgutil
    process exit is the one authoritative circuit-breaker event; yt-dlp's
    related messages are merely coalesced diagnostics.
    """
    global _bgutil_stderr_capture_installed
    with _pot_circuit_lock:
        if _bgutil_stderr_capture_installed:
            return
        try:
            from yt_dlp.utils import Popen
        except Exception:
            return
        original_run = Popen.run

        def capturing_run(command, *args, **kwargs):
            if not _is_bgutil_script_command(command):
                return original_run(command, *args, **kwargs)
            kwargs.setdefault("stderr", subprocess.PIPE)
            stdout, stderr, returncode = original_run(command, *args, **kwargs)
            if stderr:
                text = str(stderr).strip()
                if text:
                    logger.debug("[yt-dlp][po_token][bgutil stderr] %s", text)
            if returncode:
                note_po_token_provider_attempt_failure()
            return stdout, stderr, returncode

        Popen.run = staticmethod(capturing_run)
        _bgutil_stderr_capture_installed = True

_CHROMIUM_LOCAL_STATE_PATHS = {
    "chrome": {
        "win32":  r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State",
        "darwin": "~/Library/Application Support/Google/Chrome/Local State",
        "linux":  "~/.config/google-chrome/Local State",
    },
    "edge": {
        "win32":  r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Local State",
        "darwin": "~/Library/Application Support/Microsoft Edge/Local State",
        "linux":  "~/.config/microsoft-edge/Local State",
    },
    "brave": {
        "win32":  r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Local State",
        "darwin": "~/Library/Application Support/BraveSoftware/Brave-Browser/Local State",
        "linux":  "~/.config/BraveSoftware/Brave-Browser/Local State",
    },
}

def _node_major_version(version_output: str) -> Optional[int]:
    """Parse the major version out of `node --version` output (e.g. 'v22.11.0' -> 22)."""
    match = re.search(r"v?(\d+)", version_output or "")
    return int(match.group(1)) if match else None


def _get_node_version_output(node_path: str) -> str:
    """Run ``<node_path> --version`` and return stdout, or '' on any failure.

    node is a console program, so from a windowed build this must be
    launched hidden or it flashes a console window during runtime
    detection — which happens on the way into every yt-dlp call.
    """
    from utils.proc import run_hidden

    result = run_hidden(
        [node_path, "--version"], purpose="js-runtime-version", timeout=5,
    )
    return result.stdout or ""


def _detect_js_runtimes() -> dict[str, dict]:
    """Find the best available JS runtime on PATH and return a js_runtimes
    dict for yt-dlp, honoring ``_JS_RUNTIMES_PREFERENCE``. Node is skipped
    unless it meets ``_NODE_MIN_MAJOR_VERSION``.
    """
    for name in _JS_RUNTIMES_PREFERENCE:
        path = shutil.which(_JS_RUNTIME_EXE_NAMES[name])
        if not path:
            continue
        if name == "node":
            major = _node_major_version(_get_node_version_output(path))
            if major is None or major < _NODE_MIN_MAJOR_VERSION:
                continue
        return {name: {"path": path}}
    return {"deno": {}}  # fallback: let yt-dlp try to find deno itself


def _detect_bundled_pot_provider_args() -> dict[str, dict[str, list[str]]]:
    """Return official yt-dlp extractor args for a bundled PO provider."""
    try:
        from core.runtime_components import (
            bundled_pot_provider_extractor_args,
            ensure_plugin_dir_registered,
        )
        # Startup deliberately skips this to avoid importing yt-dlp before
        # the main window exists. Every path that builds yt-dlp options
        # passes through here, which is the correct, cheap moment to do it.
        ensure_plugin_dir_registered()
        return bundled_pot_provider_extractor_args()
    except Exception:
        return {}


def _detect_last_used_chromium_profile(browser: str) -> Optional[str]:
    """Return Chrome/Edge/Brave's last-used profile directory, when known."""
    paths = _CHROMIUM_LOCAL_STATE_PATHS.get(browser)
    if not paths:
        return None

    raw_path = paths.get(sys.platform)
    if not raw_path:
        raw_path = paths.get("linux")
    if not raw_path:
        return None

    local_state_path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not local_state_path.exists():
        return None

    try:
        data = json.loads(local_state_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    profile_info = data.get("profile")
    if not isinstance(profile_info, dict):
        return None

    last_used = profile_info.get("last_used")
    if isinstance(last_used, str) and last_used.strip():
        return last_used.strip()
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Main builder
# ──────────────────────────────────────────────────────────────────────────────

def build_base_ydl_opts(
    *,
    cookies_file:         Optional[str]  = None,
    cookies_browser:      Optional[str]  = None,
    logger:               Any            = None,
    quiet:                bool           = True,
    retries:              int            = 10,
    socket_timeout:       int            = 20,
    proxy:                Optional[str]  = None,
    enable_po_token_provider: bool       = True,
    respect_po_token_circuit: bool       = True,
) -> dict[str, Any]:
    """
    Return a base yt-dlp options dict with BananaFlow's standard network,
    cookie, and retry settings applied. Lets yt-dlp handle impersonation natively.
    """
    opts: dict[str, Any] = {
        # ── Network resilience ────────────────────────────────────────────────
        "nocheckcertificate":              True,
        "retries":                         retries,
        "fragment_retries":                retries,
        "extractor_retries":               5,
        "socket_timeout":                  socket_timeout,
        "abort_on_unavailable_fragment":   False,
        "concurrent_fragment_downloads":   5,

        # ── Verbosity ─────────────────────────────────────────────────────────
        "quiet":       quiet,
        "no_warnings": False,
        "color":       "no_color",

        # ── JS runtime for YouTube player decryption ──────────────────────────
        "js_runtimes": _detect_js_runtimes(),
    }

    # ── Logger (optional) ─────────────────────────────────────────────────────
    circuit_allows_provider = (
        not respect_po_token_circuit or not po_token_provider_circuit_open()
    )
    if enable_po_token_provider and circuit_allows_provider:
        bundled_provider_args = _detect_bundled_pot_provider_args()
        if bundled_provider_args:
            install_bgutil_stderr_capture()
            opts["extractor_args"] = bundled_provider_args

    if logger is not None:
        opts["logger"] = logger

    # ── Cookie injection ──────────────────────────────────────────────────────
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        from core.browser_session import require_supported_browser_cookie_mode
        require_supported_browser_cookie_mode(cookies_browser)
        profile = _detect_last_used_chromium_profile(cookies_browser)
        opts["cookiesfrombrowser"] = (cookies_browser, profile, None, None)

    # ── Optional HTTP/HTTPS/SOCKS proxy ──────────────────────────────────────
    if proxy:
        opts["proxy"] = proxy

    return opts


@contextmanager
def temp_cookies_copy(cookies_file: Optional[str]) -> Iterator[Optional[str]]:
    """
    Yield a private, throwaway copy of ``cookies_file`` for the duration of
    one yt-dlp call.

    yt-dlp treats ``cookiefile`` as read-write and rewrites it after every
    session. Concurrent yt-dlp instances pointed at the *same* path (e.g.
    parallel Spotify-to-YouTube match lookups) race on that file and can
    corrupt it — a private copy removes the shared mutable state; whatever
    yt-dlp does to its copy is discarded when the temp file is deleted.
    """
    if not cookies_file or not os.path.exists(cookies_file):
        yield cookies_file
        return

    fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="bananaflow_cookies_")
    os.close(fd)
    try:
        shutil.copyfile(cookies_file, tmp_path)
        restrict_path_permissions(tmp_path)
        yield tmp_path
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def build_search_ydl_opts(
    *,
    cookies_file:    Optional[str] = None,
    cookies_browser: Optional[str] = None,
    logger:          Any           = None,
    max_results:     int           = 15,
    proxy:           Optional[str] = None,
) -> dict[str, Any]:
    """
    Variant for metadata-only search operations (no download).
    """
    opts = build_base_ydl_opts(
        cookies_file=cookies_file,
        cookies_browser=cookies_browser,
        logger=logger,
        quiet=True,
        retries=3,
        socket_timeout=10,
        proxy=proxy,
    )
    opts.update({
        "extract_flat":  True,
        "skip_download": True,
        # ignoreerrors intentionally omitted: we want real errors to propagate
        "playlistend":   max_results,
    })
    return opts


def build_parse_ydl_opts(
    *,
    cookies_file:    Optional[str] = None,
    cookies_browser: Optional[str] = None,
    logger:          Any           = None,
    proxy:           Optional[str] = None,
) -> dict[str, Any]:
    """
    Variant for playlist/URL metadata extraction (PlaylistParser).
    """
    opts = build_base_ydl_opts(
        cookies_file=cookies_file,
        cookies_browser=cookies_browser,
        logger=logger,
        quiet=True,
        retries=5,
        proxy=proxy,
    )
    opts.update({
        "extract_flat":  "in_playlist",
        "skip_download": True,
        "ignoreerrors":  True,
    })
    return opts
