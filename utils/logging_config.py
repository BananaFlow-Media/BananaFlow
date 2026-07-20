"""
utils/logging_config.py  –  Centralised logging for BananaFlow
======================================================================
Call ``setup_logging()`` once from main.py before any other import.
Every module then just does::

    import logging
    logger = logging.getLogger(__name__)

Log files are written to ``~/.bananaflow/logs/`` with automatic rotation
(5 MB per file, 3 backups kept).  A concise console handler is also
attached so ``--debug`` runs surface everything in the terminal.

Thread safety
-------------
Python's logging module is thread-safe by design.  The ``card_key``
context filter added here lets download-worker log lines carry the
track identifier without passing it through every function signature.

Zero GUI imports.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

from utils.security import RedactingFormatter, harden_directory_once


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_LOG_DIR_NAME = "logs"
_LOG_FILE     = "bananaflow.log"
_MAX_BYTES    = 5 * 1024 * 1024   # 5 MB per file
_BACKUP_COUNT = 3

_FILE_FMT = (
    "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s"
)
_CONSOLE_FMT = (
    "%(levelname)-8s  %(name)s: %(message)s"
)
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ──────────────────────────────────────────────────────────────────────────────
# Context filter (optional per-track key for worker threads)
# ──────────────────────────────────────────────────────────────────────────────

class _CardKeyFilter(logging.Filter):
    """Inject ``card_key`` into every record so the format string can use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "card_key"):
            record.card_key = ""  # type: ignore[attr-defined]
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(
    *,
    debug: bool = False,
    log_dir: Optional[str] = None,
) -> Path:
    """
    Configure the root logger with a rotating file handler and a console
    handler.

    Parameters
    ----------
    debug :
        When True the console handler is set to DEBUG and third-party
        loggers (yt-dlp, urllib3, etc.) are left at their default level.
        When False the console is WARNING-only and noisy libs are muted.
    log_dir :
        Override the log directory.  Defaults to the per-platform
        app-data ``logs/`` dir resolved by ``utils.paths.get_log_dir``.

    Returns
    -------
    Path
        The directory where log files are written.
    """
    # Resolve log directory. The app-data location is owned by
    # utils.paths (single source of truth) so Windows %APPDATA%, macOS
    # ~/Library/Application Support, and Linux XDG all resolve correctly.
    if log_dir is None:
        from utils.paths import get_log_dir
        resolved_dir = get_log_dir()
    else:
        resolved_dir = Path(log_dir)

    resolved_dir.mkdir(parents=True, exist_ok=True)
    log_path = resolved_dir / _LOG_FILE

    # Harden the log directory, but never let that kill startup. This runs
    # at import time in main.py, before QApplication exists and before any
    # handler is attached, so an exception here used to escape as an
    # unhandled error with no log line and no window — the user saw only a
    # bare crash dialog. Logs are redacted by RedactingFormatter, so a
    # failed ACL tightening is a warning, not a reason to refuse to start.
    # It is recorded below, once handlers exist to record it.
    hardening_error = ""
    try:
        harden_directory_once(resolved_dir)
    except Exception as exc:                # noqa: BLE001 - reported below
        hardening_error = str(exc)

    # ── Root logger ───────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)          # handlers decide what passes
    root.handlers.clear()                 # idempotent re-calls

    # ── Rotating file handler (always DEBUG) ──────────────────────────────
    fh = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(RedactingFormatter(_FILE_FMT, datefmt=_DATE_FMT))
    fh.addFilter(_CardKeyFilter())
    root.addHandler(fh)

    # ── Console handler ───────────────────────────────────────────────────
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG if debug else logging.WARNING)
    ch.setFormatter(RedactingFormatter(_CONSOLE_FMT))
    ch.addFilter(_CardKeyFilter())
    root.addHandler(ch)

    # ── Mute noisy third-party loggers in non-debug mode ──────────────────
    if not debug:
        for name in (
            "urllib3", "httpx", "httpcore", "yt_dlp",
            "PIL", "mutagen", "PySide6",
        ):
            logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging initialised  (debug=%s, path=%s)", debug, log_path,
    )
    if hardening_error:
        logging.getLogger(__name__).warning(
            "Could not restrict permissions on the log directory: %s", hardening_error,
        )
    return resolved_dir
