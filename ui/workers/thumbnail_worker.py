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

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
import requests


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

        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            
            response = requests.get(
                self._url,
                timeout=self._timeout,
                headers={
                    # Some CDNs reject requests without a browser User-Agent
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                },
                stream=False,
                verify=False,
            )
            response.raise_for_status()

            raw: bytes = response.content
            if raw:
                # Many Qt Windows installations lack the WebP imageformats plugin.
                # If the image is WebP, we decode it with Pillow and emit JPEG bytes.
                is_webp = raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'
                if is_webp:
                    try:
                        from PIL import Image
                        import io
                        img = Image.open(io.BytesIO(raw))
                        buf = io.BytesIO()
                        img.convert("RGB").save(buf, format="JPEG", quality=90)
                        raw = buf.getvalue()
                    except Exception as e:
                        pass # Fallback to raw bytes if PIL fails or is unavailable
                
                self.thumbnail_ready.emit(self._index, raw)

        except Exception:  # noqa: BLE001
            # Silently discard – the TrackCard keeps its grey placeholder.
            # We intentionally do NOT emit an error signal; a missing thumbnail
            # is cosmetic and must never surface a dialog to the user.
            pass
