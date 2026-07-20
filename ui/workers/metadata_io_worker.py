"""Cooperative generic worker for immutable Phase 12 IO tasks."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.metadata_io import CancellationToken, MetadataIOError


class MetadataIOWorker(QThread):
    result_ready = Signal(object, object)  # request identity, result
    error = Signal(object, object)         # request identity, IOErrorInfo

    def __init__(self, request_identity, operation, parent=None) -> None:
        super().__init__(parent)
        self.request_identity = request_identity
        self._operation = operation
        self.cancellation = CancellationToken()

    def cancel(self) -> None:
        self.cancellation.cancel()

    def run(self) -> None:
        try:
            result = self._operation(self.cancellation)
        except MetadataIOError as exc:
            self.error.emit(self.request_identity, exc.info)
        except Exception:
            from core.metadata_io import IOErrorInfo, IOErrorKind
            self.error.emit(self.request_identity, IOErrorInfo(IOErrorKind.WRITE_FAILED))
        else:
            try:
                self.cancellation.raise_if_cancelled()
            except MetadataIOError as exc:
                self.error.emit(self.request_identity, exc.info)
                return
            self.result_ready.emit(self.request_identity, result)
