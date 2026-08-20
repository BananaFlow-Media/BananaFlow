"""
ui/workers/thumbnail_worker.py  –  Async thumbnail image fetcher
=================================================================
Fetches a single thumbnail image from a remote URL on a background thread
and emits validated image bytes so the UI thread can decode and display them
without network I/O on the main thread.

One ThumbnailWorker is spawned per track card immediately after the card is
added to the queue panel. Workers are discarded after they emit or silently
fail; thumbnail failures are cosmetic and never surface a user dialog.
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


# Bound the cache by both count and bytes. A count-only cache can retain
# hundreds of multi-megabyte max-resolution covers and quietly consume a very
# large amount of RAM in a long-running session.
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
    """Share cache entries across thumbnail variants of the same YouTube ID."""
    video_id = extract_youtube_video_id(url)
    return f"youtube:{video_id}" if video_id else url


def clear_thumbnail_cache() -> None:
    """Clear the process-local thumbnail cache (also useful for tests)."""
    global _THUMBNAIL_CACHE_BYTES
    with _CACHE_LOCK:
        _THUMBNAIL_CACHE.clear()
        _THUMBNAIL_CACHE_BYTES = 0


def get_cached_thumbnail(url: str) -> bytes | None:
    """Retrieve validated thumbnail bytes from the memory cache."""
    key = _cache_key(url)
    with _CACHE_LOCK:
        data = _THUMBNAIL_CACHE.get(key)
        if data is not None:
            _THUMBNAIL_CACHE.move_to_end(key)
        return data


def store_cached_thumbnail(url: str, data: bytes) -> None:
    """Store validated bytes in a byte-bounded, thread-safe LRU cache."""
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
    """Validate image bytes and convert formats Qt may not decode reliably.

    The former worker accepted any HTTP 200 body larger than 200 bytes. An
    HTML error page could therefore be cached and emitted as a thumbnail.
    Pillow is already a required BananaFlow dependency, so use a real decoder
    as the trust boundary instead of a size heuristic. Decompression-bomb
    warnings are promoted to errors because remote artwork is untrusted input.
    """
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

            # Qt installations on Windows do not always ship every image-format
            # plugin. Convert other valid still images (notably WebP) to JPEG.
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


def _download_candidate(url: str, timeout: float) -> bytes | None:
    """Fetch one bounded, TLS-verified candidate and return validated bytes."""
    response = None
    try:
        # Do not pass verify=False: requests' default certificate validation is
        # intentional. A cosmetic thumbnail is never worth weakening TLS.
        with _FETCH_SLOTS:
            response = requests.get(
                url,
                timeout=max(0.1, timeout),
                headers=_HEADERS,
                stream=True,
                allow_redirects=True,
            )
            if response.status_code != 200:
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
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    return None
                chunks.append(chunk)

        return _normalise_image_bytes(b"".join(chunks))
    except requests.RequestException:
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - close is best effort
                pass


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

        video_id = extract_youtube_video_id(self._url)
        candidates = (
            get_youtube_thumbnail_candidates(self._url)
            if video_id
            else [self._url]
        )

        # The timeout is a budget for the whole fallback chain, not five
        # independent waits. A missing max-res thumbnail must not turn an
        # 8-second cosmetic fetch into a 40-second worker.
        deadline = time.monotonic() + max(0.1, float(self._timeout))
        for candidate_url in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            raw = _download_candidate(candidate_url, remaining)
            if raw is None:
                continue

            # Cache by canonical YouTube ID when possible, so hq/maxres/etc.
            # variants of one video share one entry instead of duplicating the
            # same payload in memory.
            store_cached_thumbnail(self._url, raw)
            self.thumbnail_ready.emit(self._index, raw)
            return
