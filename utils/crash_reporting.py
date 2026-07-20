"""
utils/crash_reporting.py  –  Nothing fails silently
====================================================
A packaged BananaFlow build is compiled with ``console=False``.  That has
a consequence which is easy to miss and expensive in the field:
``sys.stdout`` and ``sys.stderr`` do not exist.  Python's default
handling of an unhandled exception is to *print a traceback to stderr* —
so in the packaged app the default handling is to discard it.

Three classes of failure were invisible as a result:

``sys.excepthook``
    An exception escaping the main thread.  PyInstaller's windowed
    traceback dialog may show it, but nothing records it.

``threading.excepthook``
    An exception escaping a worker thread — which is every
    ``ui/workers/*.py`` ``QThread.run()``.  Qt does not catch it, the
    worker's completion signal is never emitted, and the UI simply waits
    forever.  From the user's side the button did nothing at all.

Qt's own message handler
    ``qWarning``/``qCritical``/``qFatal`` output, including the
    diagnostics Qt emits before it aborts.

:func:`install` routes all three into the rotating log file, which is
already redacted by ``RedactingFormatter``.  It does not change what the
app *does* on failure — it only guarantees there is a record.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

logger = logging.getLogger("bananaflow.crash")

_installed = False


def _log_unhandled(exc_type, exc_value, exc_traceback) -> None:
    logger.critical(
        "Unhandled exception in main thread",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def _log_thread_exception(args: Any) -> None:
    # threading.ExceptHookArgs: (exc_type, exc_value, exc_traceback, thread)
    thread = getattr(args, "thread", None)
    name = getattr(thread, "name", "<unknown>")
    if args.exc_type is SystemExit:
        return
    logger.critical(
        "Unhandled exception in worker thread %r — its completion signal was "
        "never emitted, so whatever the user started will appear to hang",
        name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _install_qt_message_handler() -> None:
    """Forward Qt's own diagnostics into the log.  Best-effort."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:                       # pragma: no cover - no Qt present
        return

    levels = {
        QtMsgType.QtDebugMsg:    logging.DEBUG,
        QtMsgType.QtInfoMsg:     logging.INFO,
        QtMsgType.QtWarningMsg:  logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg:    logging.CRITICAL,
    }

    qt_logger = logging.getLogger("bananaflow.qt")

    def handler(mode, context, message) -> None:
        location = ""
        try:
            if context is not None and context.file:
                location = f" ({context.file}:{context.line})"
        except Exception:                   # pragma: no cover - defensive
            location = ""
        qt_logger.log(levels.get(mode, logging.INFO), "%s%s", message, location)

    qInstallMessageHandler(handler)


def install() -> None:
    """Install every hook.  Idempotent; safe to call before Qt exists."""
    global _installed
    if _installed:
        return

    sys.excepthook = _log_unhandled
    threading.excepthook = _log_thread_exception
    _install_qt_message_handler()

    _installed = True
    logger.debug("Crash reporting hooks installed")
