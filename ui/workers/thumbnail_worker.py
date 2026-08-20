"""
ui/workers/thumbnail_worker.py  –  Async thumbnail image fetcher
=================================================================
Fetches a single thumbnail image from a remote URL on a background thread
and emits validated image bytes so the UI thread can decode and display them
without network I/O on the main thread.
"""

from __future__ import annotations

import collections
import io
import threading
import time
import warnings

from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import QThread, Signal
import requests

from utils.artwork_cleaner import extract_youtube_video_id, get_youtube_thumbnail_candidates


_CACHE_MAX_ITEMS = 256
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_MAX_CONCURRENT_FETCHES = 8
_THUMBNAIL_CACHE: collections.OrderedDict[str, bytes] = collections.OrderedDict()
_THUMBNAIL_CACHE_BYTES = 0
_CACHE_LOCK = threading.Lock()
_FETCH_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_FETCHES)


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


def _cache_key(url: str) -> str:
    video_id = extract_youtube_video_id(url)
    return f"youtube:{video_id}" if video_id else url


def clear_thumbnail_cache() -> None:
    global _THUMBNAIL_CACHE_BYTES
    with _CACHE_LOCK:
        _THUMBNAIL_CACHE.clear()
        _THUMBNAIL_CACHE_BYTES = 0


def get_cached_thumbnail(url: str) -> bytes | None:
    key = _cache_key(url)
    with _CACHE_LOCK:
        data = _THUMBNAIL_CACHE.get(key)
        if data is not None:
            _THUMBNAIL_CACHE.move_to_end(key)
        return data


def store_cached_thumbnail(url: str, data: bytes) -> None:
    global _THUMBNAIL_CACHE_BYTES
    if not url or not data or len(data) > _CACHE_MAX_BYTES:
        return

    key = _cache_key(url)
    with _CACHE_LOCK:
        previous = _THUMBNAIL_CACHE.pop(key, None)
        if previous is not None:
            _THUMBNAIL_CACHE_BYTES -= len(previous)

        _THUMBNAIL_CACHE[key] = data
        _THUMBNAIL_CACHE_BYTES += len(data)
        _THUMBNAIL_CACHE.move_to_end(key)

        while (
            len(_THUMBNAIL_CACHE) > _CACHE_MAX_ITEMS
            or _THUMBNAIL_CACHE_BYTES > _CACHE_MAX_BYTES
        ):
            _, evicted = _THUMBNAIL_CACHE.popitem(last=False)
            _THUMBNAIL_CACHE_BYTES -= len(evicted)


def _normalise_image_bytes(raw: bytes) -> bytes | None:
    if not raw:
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as probe:
                probe.verify()
                image_format = (probe.format or "").upper()

            if image_format in {"JPEG", "PNG"}:
                return raw

            with Image.open(io.BytesIO(raw)) as image:
                image.seek(0)
                converted = image.convert("RGB")
                out = io.BytesIO()
                converted.save(out, format="JPEG", quality=90)
                return out.getvalue()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ):
        return None


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _download_candidate(url: str, deadline: float) -> bytes | None:
    """Fetch one candidate within an absolute total deadline.

    The deadline covers contention for the global fetch slot, connection/read
    waits and streamed body processing. requests' timeout is an inactivity
    timeout, so the explicit monotonic checks are required for a real total
    budget when a server keeps trickling chunks.
    """
    response = None
    acquired = False
    try:
        wait_budget = _remaining(deadline)
        if wait_budget <= 0:
            return None
        acquired = _FETCH_SLOTS.acquire(timeout=wait_budget)
        if not acquired:
            return None

        request_budget = _remaining(deadline)
        if request_budget <= 0:
            return None

        response = requests.get(
            url,
            timeout=max(0.1, request_budget),
            headers=_HEADERS,
            stream=True,
            allow_redirects=True,
        )
        if response.status_code != 200 or _remaining(deadline) <= 0:
            return None

        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and not (
            content_type.startswith("image/")
            or content_type == "application/octet-stream"
        ):
            return None

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > _MAX_RESPONSE_BYTES:
                    return None
            except (TypeError, ValueError):
                pass

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if _remaining(deadline) <= 0:
                return None
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                return None
            chunks.append(chunk)

        if _remaining(deadline) <= 0:
            return None
        return _normalise_image_bytes(b"".join(chunks))
    except requests.RequestException:
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - close is best effort
                pass
        if acquired:
            _FETCH_SLOTS.release()


def _candidate_order(url: str) -> list[str]:
    """Prefer extractor-provided artwork for fast UI, then quality fallbacks."""
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return [url] if url else []

    fallback = get_youtube_thumbnail_candidates(url)
    return list(dict.fromkeys([url, *fallback]))


class ThumbnailWorker(QThread):
    """Fetch one thumbnail and emit its validated bytes on success."""

    thumbnail_ready = Signal(int, bytes)

    def __init__(
        self,
        track_index: int,
        url: str,
        timeout: int = 8,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index = track_index
        self._url = url
        self._timeout = timeout

    def run(self) -> None:
        if not self._url:
            return

        cached = get_cached_thumbnail(self._url)
        if cached is not None:
            self.thumbnail_ready.emit(self._index, cached)
            return

        candidates = _candidate_order(self._url)
        deadline = time.monotonic() + max(0.1, float(self._timeout))

        for candidate_url in candidates:
            if _remaining(deadline) <= 0:
                break

            raw = _download_candidate(candidate_url, deadline)
            if raw is None:
                continue

            store_cached_thumbnail(self._url, raw)
            self.thumbnail_ready.emit(self._index, raw)
            return
