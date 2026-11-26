"""
ui/workers/component_install_worker.py  –  Approved component upgrade
=====================================================================
Runs the appropriate update path on a background thread after an explicit
click: pip for source environments, or the verified versioned app-data
overlay for an installed/frozen build.

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
from utils.paths import is_frozen
from utils.security import redact_text

_TIMEOUT_SECONDS = 600          # pip resolving + downloading can be slow
_OUTPUT_TAIL_CHARS = 2000


class ComponentInstallWorker(QThread):
    """One-shot pip-upgrade runner. Create, connect, start, discard."""

    completed = Signal(bool, str)

    def __init__(self, parent=None, *, updates=None) -> None:
        super().__init__(parent)
        self._updates = updates
        self.install_failures: list[str] = []

    def run(self) -> None:
        if is_frozen():
            self._run_packaged_update()
            return
        self._run_source_update()

    def _run_packaged_update(self) -> None:
        versions = []
        downloader = self._updates is None or any(
            status.key in {"yt-dlp", "yt-dlp-ejs"} for status in self._updates
        )
        if downloader:
            try:
                from core.component_overlay import install_verified_component_update
                result = install_verified_component_update()
                versions.extend(f"{name} {version}" for name, version in result.versions)
            except Exception as exc:
                self.install_failures.append(f"yt-dlp: {redact_text(str(exc))}")
        for status in self._updates or ():
            if status.key.startswith("runtime:"):
                try:
                    from core.tool_updates import install_tool_update
                    install_tool_update(status.key.split(":")[1], status.latest_version)
                    versions.append(f"{status.display_name} {status.latest_version}")
                except Exception as exc:
                    self.install_failures.append(f"{status.display_name}: {redact_text(str(exc))}")
        detail = ", ".join(versions) if versions else "\n".join(self.install_failures)
        self.completed.emit(bool(versions), detail[-_OUTPUT_TAIL_CHARS:])

    def _run_source_update(self) -> None:
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
