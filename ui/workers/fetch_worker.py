"""
ui/workers/fetch_worker.py  –  Playlist / track metadata extraction worker
===========================================================================
Wraps PlaylistParser.parse() in a QThread and translates its Python callbacks
into Qt signals so the UI thread can safely update widgets in response.

Signal summary
--------------
track_found(dict)     One track resolved; dict keys match TrackCard.__init__ args.
progress_msg(str)     Human-readable status string for the status bar.
soft_error(str)       Non-fatal per-item error (private video, geo-block, etc.).
                      Parsing continues after this is emitted.
finished(ParseResult) Parsing complete (success, partial, or cancelled).
error(ErrorInfo)      Irrecoverable failure; ErrorInfo.severity drives dialog type.

Threading model
---------------
The worker is created fresh for each Fetch request and started with .start().
Cancel mid-flight by calling worker.cancel() from any thread – this delegates
to PlaylistParser.cancel() which sets a threading.Event checked between items.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

from error_handler import classify_error, ErrorInfo
from core.playlist_parser import (
    ParseResult,
    PlaylistParser,
    SourcePlatform,
    TrackMeta,
)


class FetchWorker(QThread):
    """
    Background thread that fetches playlist / track metadata and emits
    each resolved track incrementally so the queue panel populates live.

    Parameters
    ----------
    url          : YouTube or Spotify URL to parse.
    cookies_file : Optional path to a Netscape cookies.txt for authenticated
                   requests (age-gated / private content).
    parent       : Optional Qt parent object.
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    track_found  = Signal(dict, int, int)     # One track; keys: title, artist,
                                            # duration, platform, thumbnail_url, track_url
    progress_msg = Signal(str)      # Status bar message
    soft_error   = Signal(str)      # Non-fatal per-item warning
    finished     = Signal(object)   # ParseResult  (always emitted on clean exit)
    error        = Signal(object)   # ErrorInfo    (emitted instead of finished
                                    #               on irrecoverable failure)

    # ── Platform label map (SourcePlatform → string used by TrackCard) ────────
    _PLATFORM_STR: dict[SourcePlatform, str] = {
        SourcePlatform.YOUTUBE:       "youtube",
        SourcePlatform.YOUTUBE_MUSIC: "ytmusic",
        SourcePlatform.SPOTIFY:       "spotify",
        SourcePlatform.GENERIC:       "generic",
    }

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        url:          str,
        cookies_file: Optional[str] = None,
        proxy_url:    Optional[str] = None,
        proxy_token:  Optional[str] = None,
        channel_tabs: Optional[list[str]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._url     = url
        self._cookies = cookies_file
        self._proxy_url = proxy_url
        self._proxy_token  = proxy_token
        self._channel_tabs = channel_tabs
        self._parser       = PlaylistParser()

    def cancel(self) -> None:
        """
        Thread-safe cancellation.
        Sets a threading.Event inside PlaylistParser that is checked between
        every item so the thread exits cleanly after the current item finishes.
        """
        self._parser.cancel()

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Entry point executed on the worker thread."""

        def on_item(track: TrackMeta, idx: int, total: Optional[int]) -> None:
            """Called by PlaylistParser for every resolved track."""
            self.track_found.emit({
                "title":         track.title,
                "artist":        track.artist,
                "duration":      track.duration_str,
                "platform":      self._PLATFORM_STR.get(track.platform, "youtube"),
                "thumbnail_url": track.thumbnail_url,
                "track_url":     track.url,
                "album":         track.album,
                "parent_artist": track.parent_artist,
                "release_type":  track.release_type,
                "category":      track.category,
                "album_index":   track.album_index,
                "total_tracks":  track.total_tracks,
            }, idx, total or 0)

        def on_progress(msg: str) -> None:
            """Called by PlaylistParser with status messages."""
            self.progress_msg.emit(msg)

        def on_error(msg: str) -> None:
            """Called by PlaylistParser for non-fatal per-item failures."""
            self.soft_error.emit(msg)

        try:
            result: ParseResult = self._parser.parse(
                self._url,
                cookies_file=self._cookies,
                proxy_url=self._proxy_url,
                proxy_token=self._proxy_token,
                channel_tabs=self._channel_tabs,
                on_item=on_item,
                on_progress=on_progress,
                on_error=on_error,
            )
            # Always emit finished – the UI inspects result.cancelled / result.error
            # to decide what to show.
            self.finished.emit(result)

        except Exception as exc:  # noqa: BLE001
            # Translate any uncaught exception into a structured ErrorInfo so the
            # UI can display the right dialog without importing yt-dlp internals.
            err: ErrorInfo = classify_error(exc)
            self.error.emit(err)
