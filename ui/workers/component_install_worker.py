"""
ui/workers/component_install_worker.py  –  In-place component upgrade
======================================================================
Runs the pip upgrade command from core.component_updates on a background
thread. Only ever started after the user explicitly clicked "Update
components" in the update prompt — nothing in the app runs this
automatically — and only in source/venv mode (core.component_updates.
can_update_in_place() gates the button that launches it).

The upgraded packages are already imported into the running process, so
the new version only takes effect after BananaFlow restarts; the completion
message the UI shows says so.

Signal summary
--------------
completed(bool success, str output_tail)
    Emitted exactly once. ``output_tail`` carries the last part of pip's
    combined stdout/stderr for the failure dialog (never shown on
    success beyond logging).
"""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QThread, Signal

from core.component_updates import pip_upgrade_command

_TIMEOUT_SECONDS = 600          # pip resolving + downloading can be slow
_OUTPUT_TAIL_CHARS = 2000


class ComponentInstallWorker(QThread):
    """One-shot pip-upgrade runner. Create, connect, start, discard."""

    completed = Signal(bool, str)

    def run(self) -> None:
        command = pip_upgrade_command()
        creationflags = 0
        if sys.platform == "win32":
            # Don't flash a console window from a GUI app.
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_SECONDS,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.completed.emit(False, str(exc)[-_OUTPUT_TAIL_CHARS:])
            return

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        self.completed.emit(proc.returncode == 0, output[-_OUTPUT_TAIL_CHARS:].strip())
