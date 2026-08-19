"""
ui/workers/thumbnail_worker.py  –  Async thumbnail image fetcher
=================================================================
Fetches a single thumbnail image from a remote URL on a background thread
and emits the raw bytes so the UI thread can decode and display it without
any I/O on the main thread.

One ThumbnailWorker is spawned per track card immediately after the card is
added to the queue panel.  Workers are daemon threads (Qt default for QThread
when no parent is set) and are discarded after they emit or silently fail.

Signal summary
--------------
thumbnail_ready(int, bytes)
    Emitted on success with the track's 1-based index and the raw image bytes
    (JPEG or PNG).  The receiving slot decodes the bytes into a QPixmap.
    Nothing is emitted on failure – the card simply keeps its placeholder.
"""

import collections
import io
import threading
from PySide6.QtCore import QThread, Signal
import requests
from utils.artwork_cleaner import extract_youtube_video_id, get_youtube_thumbnail_candidates

# In-memory LRU cache for fetched thumbnail bytes (max 500 items)
_CACHE_MAX_SIZE = 500
_THUMBNAIL_CACHE: collections.OrderedDict[str, bytes] = collections.OrderedDict()
_CACHE_LOCK = threading.Lock()


def get_cached_thumbnail(url: str) -> bytes | None:
    """Retrieve raw thumbnail bytes from memory cache if available."""
    with _CACHE_LOCK:
        if url in _THUMBNAIL_CACHE:
            _THUMBNAIL_CACHE.move_to_end(url)
            return _THUMBNAIL_CACHE[url]
    return None


def store_cached_thumbnail(url: str, data: bytes) -> None:
    """Store raw thumbnail bytes into the in-memory LRU cache."""
    if not url or not data:
        return
    with _CACHE_LOCK:
        _THUMBNAIL_CACHE[url] = data
        _THUMBNAIL_CACHE.move_to_end(url)
        if len(_THUMBNAIL_CACHE) > _CACHE_MAX_SIZE:
            _THUMBNAIL_CACHE.popitem(last=False)


class ThumbnailWorker(QThread):
    """
    Fetches one thumbnail and emits its raw bytes.

    Parameters
    ----------
    track_index : 1-based index that identifies which TrackCard this thumbnail
                  belongs to.  Passed back in the thumbnail_ready signal so
                  the receiving slot can route the pixmap to the right card.
    url         : Full HTTPS URL to the thumbnail image.
    timeout     : HTTP request timeout in seconds (default 8).
    parent      : Optional Qt parent object.
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    thumbnail_ready = Signal(int, bytes)
    # (track_index, raw_image_bytes)
    # Nothing is emitted on failure – placeholder image stays visible.

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        track_index: int,
        url:         str,
        timeout:     int = 8,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._index   = track_index
        self._url     = url
        self._timeout = timeout

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Entry point executed on the worker thread."""
        if not self._url:
            return

        # 1. Instant Cache Hit
        cached = get_cached_thumbnail(self._url)
        if cached:
            self.thumbnail_ready.emit(self._index, cached)
            return

        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }

            # 2. Determine URL candidates (with fallback hierarchy for YouTube)
            video_id = extract_youtube_video_id(self._url)
            if video_id:
                candidates = get_youtube_thumbnail_candidates(self._url)
            else:
                candidates = [self._url]

            raw_bytes: bytes | None = None

            for candidate_url in candidates:
                # Check cache for candidate
                cached_candidate = get_cached_thumbnail(candidate_url)
                if cached_candidate:
                    raw_bytes = cached_candidate
                    break

                try:
                    resp = requests.get(
                        candidate_url,
                        timeout=self._timeout,
                        headers=headers,
                        stream=False,
                        verify=False,
                    )
                    if resp.status_code == 200 and resp.content:
                        # Some YouTube 404s or placeholders might return small 1x1 GIF / HTML
                        if len(resp.content) > 200:
                            raw_bytes = resp.content
                            store_cached_thumbnail(candidate_url, raw_bytes)
                            break
                except Exception:
                    continue

            if raw_bytes:
                # Convert WebP to JPEG if necessary for Qt Windows compatibility
                is_webp = raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP"
                if is_webp:
                    try:
                        from PIL import Image
                        img = Image.open(io.BytesIO(raw_bytes))
                        buf = io.BytesIO()
                        img.convert("RGB").save(buf, format="JPEG", quality=90)
                        raw_bytes = buf.getvalue()
                    except Exception:
                        pass  # Fallback to raw bytes if PIL conversion fails

                store_cached_thumbnail(self._url, raw_bytes)
                self.thumbnail_ready.emit(self._index, raw_bytes)

        except Exception:  # noqa: BLE001
            # Silently discard – the TrackCard keeps its grey placeholder.
            pass

