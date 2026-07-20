"""Cancellation-aware QThread boundary for authoritative refresh planning."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.file_refresh_service import FileRefreshService
from core.metadata_io import CancellationToken, IOErrorKind, MetadataIOError


class FilesystemRefreshWorker(QThread):
    plan_ready = Signal(object)
    refresh_failed = Signal(str)
    refresh_cancelled = Signal()

    def __init__(self, *, root: Path, batch, generation: int,
                 content_revision: int, change_revision: int, tracked,
                 recursive: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.root = Path(root)
        self.batch = batch
        self.generation = int(generation)
        self.content_revision = int(content_revision)
        self.change_revision = int(change_revision)
        self.tracked = tuple(tracked)
        self.recursive = bool(recursive)
        self._cancellation = CancellationToken()

    def cancel(self) -> None:
        self._cancellation.cancel()

    def run(self) -> None:
        try:
            plan = FileRefreshService().build_plan(
                root=self.root, batch=self.batch, generation=self.generation,
                content_revision=self.content_revision,
                change_revision=self.change_revision, tracked=self.tracked,
                recursive=self.recursive, cancellation=self._cancellation)
            self._cancellation.raise_if_cancelled()
            self.plan_ready.emit(plan)
        except MetadataIOError as exc:
            if exc.info.kind is IOErrorKind.CANCELLED:
                self.refresh_cancelled.emit()
            else:
                self.refresh_failed.emit(exc.info.kind.value)
        except Exception as exc:
            self.refresh_failed.emit(str(exc))
