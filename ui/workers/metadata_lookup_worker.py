"""Cooperative QThreads for explicit online metadata and Artwork requests."""
from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal
import httpx

from core.metadata_lookup import (
    ArtworkResult, CancellationToken, LookupResult, LookupState, ProviderError,
    ProviderErrorKind, ReleaseDetailResult,
)

logger = logging.getLogger(__name__)


def _unknown_provider_error() -> ProviderError:
    """The retryable catch-all a worker reports when nothing else matched."""
    return ProviderError(
        ProviderErrorKind.UNKNOWN, "meta_online_provider_error", True,
    )


class MetadataLookupWorker(QThread):
    result_ready = Signal(object)

    def __init__(self, provider, request, parent=None) -> None:
        super().__init__(parent)
        self.provider = provider
        self.request = request
        self.token = CancellationToken()

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        # ``result_ready`` is the only thing that takes the panel out of its
        # "searching" state. An exception escaping here emitted nothing and
        # printed the traceback to a stderr that does not exist in a
        # windowed build, so the lookup appeared to hang forever.
        try:
            self.result_ready.emit(self.provider.lookup(self.request, self.token))
        except Exception:                   # noqa: BLE001 - must not escape a QThread
            logger.exception("Online metadata lookup failed")
            self.result_ready.emit(LookupResult(
                request=self.request,
                state=LookupState.ERROR,
                error=_unknown_provider_error(),
            ))


class ReleaseDetailWorker(QThread):
    result_ready = Signal(object)

    def __init__(self, provider, request, parent=None) -> None:
        super().__init__(parent); self.provider = provider; self.request = request
        self.token = CancellationToken()

    def cancel(self) -> None: self.token.cancel()

    def run(self) -> None:
        try:
            self.result_ready.emit(
                self.provider.lookup_release_detail(self.request, self.token)
            )
        except Exception:                   # noqa: BLE001 - must not escape a QThread
            logger.exception("Release detail lookup failed")
            self.result_ready.emit(ReleaseDetailResult(
                request=self.request,
                state=LookupState.ERROR,
                error=_unknown_provider_error(),
            ))


class ArtworkLookupWorker(QThread):
    result_ready = Signal(object)

    def __init__(self, provider, request, *, operation="preview", selected=None, parent=None) -> None:
        super().__init__(parent)
        self.provider = provider
        self.request = request
        self.operation = operation
        self.selected = selected
        self.token = CancellationToken()

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        try:
            if self.operation == "final":
                candidates = (self.selected,) if self.selected else ()
                selected = self.selected
                data = b"" if selected is None else self.provider.download_full(selected, self.token)
            else:
                candidates = self.provider.list_artwork(self.request.release_id, self.token)
                selected = self.selected or (candidates[0] if candidates else None)
                data = b"" if selected is None else self.provider.download_preview(selected, self.token)
            if self.token.cancelled:
                self.result_ready.emit(self._failure(ProviderErrorKind.CANCELLED, "meta_online_cancelled")); return
            if selected is None:
                self.result_ready.emit(self._failure(ProviderErrorKind.NO_ARTWORK, "meta_online_artwork_none")); return
            self.result_ready.emit(ArtworkResult(self.request, LookupState.READY, candidates, selected, data))
        except httpx.TimeoutException:
            self.result_ready.emit(self._failure(ProviderErrorKind.TIMEOUT, "meta_online_timeout", True))
        except httpx.TransportError:
            self.result_ready.emit(self._failure(ProviderErrorKind.OFFLINE, "meta_online_offline", True))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                kind, key, retryable = ProviderErrorKind.NO_ARTWORK, "meta_online_artwork_none", False
            elif exc.response.status_code == 429:
                kind, key, retryable = ProviderErrorKind.RATE_LIMITED, "meta_online_rate_limited", True
            else:
                kind, key, retryable = ProviderErrorKind.UNAVAILABLE, "meta_online_provider_unavailable", True
            self.result_ready.emit(self._failure(kind, key, retryable))
        except ValueError as exc:
            message = str(exc).casefold()
            if "mime" in message: kind, key = ProviderErrorKind.INVALID_MIME, "meta_online_artwork_invalid_mime"
            elif "large" in message or "size" in message: kind, key = ProviderErrorKind.ARTWORK_TOO_LARGE, "meta_online_artwork_too_large"
            else: kind, key = ProviderErrorKind.INVALID_ARTWORK, "meta_online_artwork_invalid"
            self.result_ready.emit(self._failure(kind, key))
        except Exception:
            self.result_ready.emit(self._failure(ProviderErrorKind.UNKNOWN, "meta_online_provider_error", True))

    def _failure(self, kind, key, retryable=False):
        state = LookupState.CANCELLED if kind is ProviderErrorKind.CANCELLED else (
            LookupState.OFFLINE if kind is ProviderErrorKind.OFFLINE else LookupState.ERROR)
        return ArtworkResult(self.request, state, error=ProviderError(kind, key, retryable))
