"""Explicit restart handoff, invoked only after an accepted, drained shutdown.

No restart path calls QApplication.quit() to bypass the main window's close
veto. The entry point launches the replacement only after app.exec() has
returned and its last startup worker has joined.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys
from typing import Any


@dataclass(frozen=True)
class RestartRequest:
    prepared_update: Any = None


def restart_block_reason(window) -> str:
    """Translation key for active user work that restart must never cancel."""
    download = getattr(window, "_download_ctrl", None)
    if download is not None and download.is_downloading():
        return "restart_busy_downloads"
    converter = getattr(window, "_converter_panel", None)
    worker = getattr(converter, "_worker", None)
    if worker is not None and worker.isRunning():
        return "restart_busy_conversion"
    metadata = getattr(window, "_metadata_ctrl", None)
    if metadata is not None and metadata.has_active_shutdown_work():
        return "restart_busy_metadata"
    return ""


def launch_after_shutdown(window) -> bool:
    """Launch exactly once, and never when close was vetoed or bypassed.

    An app-update helper verifies again and waits for this PID to exit before
    replacing files. Failure leaves the persisted download available to retry.
    """
    request = getattr(window, "_restart_request", None)
    if request is None or not getattr(window, "_shutdown_accepted", False):
        return False
    if getattr(window, "_restart_launched", False):
        return False
    if request.prepared_update is not None:
        from core.app_updates import launch_prepared_app_update
        launch_prepared_app_update(request.prepared_update)
    else:
        arguments = sys.argv[1:] if getattr(sys, "frozen", False) else list(sys.argv)
        environment = dict(os.environ)
        # PyInstaller restart must be a fresh instance, not a child reusing the
        # temporary extraction directory that this process is about to remove.
        if getattr(sys, "frozen", False):
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        kwargs = {"cwd": os.getcwd(), "env": environment, "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, *arguments], **kwargs)
    window._restart_launched = True
    return True
