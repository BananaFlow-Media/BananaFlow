"""
utils/proc.py  –  The single choke point for every child process
=================================================================
Every external program BananaFlow starts must go through this module.
It exists because a GUI application that shells out has two obligations
a bare ``subprocess.run`` does not meet:

1. **No console windows.**  ``bananaflow.exe`` is built with
   ``console=False``.  When a process with no console starts a *console*
   subsystem program (``icacls.exe``, ``ffmpeg.exe``, ``deno.exe``,
   ``node.exe``, …), Windows allocates a brand-new console for the child
   and shows it.  The user sees a black window flash.  Enough of them in
   a row is indistinguishable from malware.  ``CREATE_NO_WINDOW`` (plus
   a ``SW_HIDE`` STARTUPINFO for belt and braces) is therefore not a
   nicety — it is mandatory on every single launch site.

2. **A record of what ran.**  A child that fails, times out, or is
   killed by the OS leaves no trace in the parent's log unless someone
   writes one.  Every call here logs the child's identity, purpose,
   start time, PID, exit code, duration, and — on failure — a redacted
   tail of its output.

Privacy
-------
Command lines routinely contain cookie-file paths, proxy URLs with
credentials, and the user's home directory.  Nothing reaches the log
without passing through :func:`utils.security.redact_text`, the same
filter the log formatter itself uses.  Output tails are truncated as
well as redacted: this is a diagnostic aid, not a transcript.

Usage
-----
    from utils.proc import run_hidden

    result = run_hidden(
        ["icacls", str(target), "/inheritance:r"],
        purpose="acl-harden",
        timeout=60,
    )
    if result.returncode != 0:
        ...

For a long-running child the caller drives itself (FFmpeg with progress
parsing, for example), use :func:`popen_hidden`, or take just the
platform kwargs via :func:`no_window_kwargs` when the call site needs
full control of the ``Popen`` arguments.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger("bananaflow.proc")

# How much of a failed child's output is worth keeping in the log.
_OUTPUT_TAIL_CHARS = 800

_IS_WINDOWS = os.name == "nt"


# ──────────────────────────────────────────────────────────────────────────────
# Platform kwargs
# ──────────────────────────────────────────────────────────────────────────────

def no_window_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Return the ``Popen``/``run`` kwargs that keep a child invisible.

    On Windows this is ``CREATE_NO_WINDOW`` plus a ``STARTUPINFO`` asking
    for ``SW_HIDE``.  Either alone is sufficient for a console program;
    both together also cover a GUI-subsystem child that would otherwise
    show its own top-level window on startup.

    ``new_process_group`` adds ``CREATE_NEW_PROCESS_GROUP`` so the child
    does not receive the parent's Ctrl-C / Ctrl-Break, and its POSIX
    equivalent ``start_new_session``.  Use it for children the app stops
    explicitly (FFmpeg), not for short-lived ones it waits on.

    On POSIX there is no console to suppress, so this returns only the
    session flag when requested.
    """
    kwargs: dict[str, Any] = {}
    if _IS_WINDOWS:
        flags = subprocess.CREATE_NO_WINDOW
        if new_process_group:
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    elif new_process_group:
        kwargs["start_new_session"] = True
    return kwargs


# ──────────────────────────────────────────────────────────────────────────────
# Diagnostics
# ──────────────────────────────────────────────────────────────────────────────

def _redact(value: object) -> str:
    """Redact via utils.security, imported lazily to avoid a cycle."""
    try:
        from utils.security import redact_text
        return redact_text(value)
    except Exception:                       # pragma: no cover - defensive
        return "<unredactable>"


def _program_name(argv: Sequence[str]) -> str:
    """Basename of the executable, which is safe to log verbatim."""
    if not argv:
        return "<empty>"
    try:
        return Path(str(argv[0])).name or str(argv[0])
    except Exception:                       # pragma: no cover - defensive
        return "<unknown>"


def _describe(argv: Sequence[str]) -> str:
    """A redacted, length-capped rendering of the full command line."""
    joined = " ".join(str(part) for part in argv)
    redacted = _redact(joined)
    if len(redacted) > 500:
        redacted = redacted[:500] + "…"
    return redacted


def _tail(text: object) -> str:
    """Redacted trailing slice of a child's output, for failure diagnosis."""
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return _redact(str(text).strip())[-_OUTPUT_TAIL_CHARS:]


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of a completed child process.

    Mirrors the parts of ``subprocess.CompletedProcess`` call sites use,
    plus the diagnostic fields this module records.  ``timed_out`` and
    ``error`` distinguish "ran and failed" from "never ran" — a
    distinction the old bare ``subprocess.run`` call sites lost.
    """
    purpose:     str
    program:     str
    returncode:  int
    stdout:      str = ""
    stderr:      str = ""
    duration_ms: int = 0
    pid:         Optional[int] = None
    timed_out:   bool = False
    error:       str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.error


# ──────────────────────────────────────────────────────────────────────────────
# Runners
# ──────────────────────────────────────────────────────────────────────────────

def run_hidden(
    argv: Sequence[str],
    *,
    purpose: str,
    timeout: Optional[float] = None,
    text: bool = True,
    check: bool = False,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str | Path] = None,
    capture_output: bool = True,
    new_process_group: bool = False,
    log_command: bool = True,
) -> ProcessResult:
    """Run ``argv`` to completion with no console window, and log it.

    Never raises for an ordinary child failure: a non-zero exit, a
    timeout, and a missing executable all come back as a
    :class:`ProcessResult` the caller can inspect.  ``check=True``
    restores the raising behaviour for call sites that want it.

    ``purpose`` is a short stable slug ("acl-harden", "ffprobe-hls",
    "pot-provider-healthcheck") identifying *why* the app started this
    child.  It is what makes the log readable when several different
    subsystems are shelling out.
    """
    program = _program_name(argv)
    started = time.monotonic()

    # The record's own asctime is the start time — utils.logging_config
    # stamps every line — so this deliberately does not format its own.
    if log_command:
        logger.debug(
            "[proc] start purpose=%s program=%s cmd=%s",
            purpose, program, _describe(argv),
        )
    else:
        logger.debug("[proc] start purpose=%s program=%s", purpose, program)

    kwargs: dict[str, Any] = dict(no_window_kwargs(new_process_group=new_process_group))
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    kwargs["stdin"] = subprocess.DEVNULL
    if text:
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    if env is not None:
        kwargs["env"] = env
    if cwd is not None:
        kwargs["cwd"] = str(cwd)

    try:
        completed = subprocess.run(argv, timeout=timeout, check=False, **kwargs)
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "[proc] TIMEOUT purpose=%s program=%s after=%dms limit=%ss output=%r",
            purpose, program, duration_ms, timeout, _tail(exc.stdout) or _tail(exc.stderr),
        )
        result = ProcessResult(
            purpose=purpose, program=program, returncode=-1,
            duration_ms=duration_ms, timed_out=True,
            error=f"timed out after {timeout}s",
        )
        if check:
            raise
        return result
    except (OSError, ValueError) as exc:
        # FileNotFoundError (not installed / not on PATH) lands here, as
        # does a malformed argv.  The child never started.
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "[proc] LAUNCH-FAILED purpose=%s program=%s after=%dms error=%s",
            purpose, program, duration_ms, _redact(exc),
        )
        result = ProcessResult(
            purpose=purpose, program=program, returncode=-1,
            duration_ms=duration_ms, error=str(exc),
        )
        if check:
            raise
        return result

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""

    if completed.returncode == 0:
        logger.debug(
            "[proc] ok purpose=%s program=%s rc=0 duration=%dms",
            purpose, program, duration_ms,
        )
    else:
        logger.warning(
            "[proc] FAILED purpose=%s program=%s rc=%d duration=%dms "
            "cmd=%s stderr=%r stdout=%r",
            purpose, program, completed.returncode, duration_ms,
            _describe(argv) if log_command else "[redacted]", _tail(stderr), _tail(stdout),
        )

    result = ProcessResult(
        purpose=purpose,
        program=program,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, list(argv), stdout, stderr,
        )
    return result


def popen_hidden(
    argv: Sequence[str],
    *,
    purpose: str,
    new_process_group: bool = True,
    log_command: bool = True,
    **kwargs: Any,
) -> subprocess.Popen:
    """Start a long-running child with no console window, and log the start.

    The caller owns the returned ``Popen`` — it reads the pipes, waits,
    and decides what to do on failure.  Pair with :func:`log_exit` once
    the exit code is known so the log records the full lifecycle.

    Raises whatever ``Popen`` raises; a child that cannot be started is
    logged first so the failure is never silent.
    """
    program = _program_name(argv)
    if log_command:
        logger.debug(
            "[proc] spawn purpose=%s program=%s cmd=%s",
            purpose, program, _describe(argv),
        )
    else:
        logger.debug("[proc] spawn purpose=%s program=%s", purpose, program)
    merged: dict[str, Any] = dict(no_window_kwargs(new_process_group=new_process_group))
    merged.update(kwargs)
    try:
        proc = subprocess.Popen(argv, **merged)
    except (OSError, ValueError) as exc:
        logger.error(
            "[proc] SPAWN-FAILED purpose=%s program=%s error=%s",
            purpose, program, _redact(exc),
        )
        raise
    logger.info(
        "[proc] spawned purpose=%s program=%s pid=%s", purpose, program, proc.pid,
    )
    return proc


def log_exit(
    proc: subprocess.Popen,
    *,
    purpose: str,
    returncode: Optional[int] = None,
    stderr_tail: object = "",
) -> None:
    """Record how a :func:`popen_hidden` child ended.

    Call once the exit code is known.  A negative code on POSIX (or a
    large one on Windows) usually means the child was killed by a signal
    or crashed rather than exiting normally, which is exactly the case
    that otherwise leaves no trace.
    """
    code = proc.returncode if returncode is None else returncode
    program = _program_name(getattr(proc, "args", ()) or ())
    if code == 0:
        logger.debug("[proc] exit purpose=%s program=%s pid=%s rc=0",
                     purpose, program, proc.pid)
    else:
        logger.warning(
            "[proc] EXIT-NONZERO purpose=%s program=%s pid=%s rc=%s stderr=%r",
            purpose, program, proc.pid, code, _tail(stderr_tail),
        )
