"""Background preparation/restoration of verified application updates."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class AppUpdateWorker(QThread):
    """A release downloads on explicit approval; None only reads local state."""

    progress = Signal(object, object)  # byte counts can exceed Qt's signed int
    completed = Signal(object, str)  # PreparedAppUpdate | None, error

    def __init__(self, release=None, parent=None) -> None:
        super().__init__(parent)
        self._release = release
        self.update_status = {}

    def run(self) -> None:
        try:
            from core.app_updates import get_app_update_status, get_prepared_app_update, prepare_app_update
            if self.isInterruptionRequested():
                return
            if self._release is None:
                result = get_prepared_app_update()
                self.update_status = get_app_update_status()
            else:
                result = prepare_app_update(
                    self._release,
                    progress=lambda done, total: self.progress.emit(done, total),
                    cancelled=self.isInterruptionRequested,
                )
            if not self.isInterruptionRequested():
                self.completed.emit(result, "")
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.completed.emit(None, str(exc)[-1000:])
