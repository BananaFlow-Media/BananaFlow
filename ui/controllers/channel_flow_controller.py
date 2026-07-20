"""
ui/controllers/channel_flow_controller.py  –  YouTube channel import orchestrator
==================================================================================
Coordinates the multi-step channel import flow:

  Phase 1  TabSelectDialog (auto-discovers tabs via Playwright, user picks)
  Phase 2  ChannelScrapeWorker scrapes selected tabs with yt-dlp
  Phase 3  DuplicateDetector finds cross-tab duplicates (sync, fast)
  Phase 4  ConflictResolutionDialog (only shown when duplicates exist)
  Phase 5  Build final list of TrackMeta dicts and emit tracks_ready

AppWindow calls `run()` and connects `tracks_ready` to its `_add_track_to_queue`.

File/folder mapping
-------------------
Regular tab item (e.g. "סרטונים"):
    parent_artist = channel_name
    category      = tab_name         → folder: {channel}/{tab_name}/
    release_type  = "video"
    album_index   = 0                → no numbering

Playlist item:
    parent_artist = channel_name
    category      = "פלייליסטים"     → folder: {channel}/פלייליסטים/{playlist_name}/
    release_type  = "ep"             → is_grouped=True + forced_index used
    album         = playlist_name
    album_index   = playlist_index   → 01 - Title.mp3
    total_tracks  = len(playlist)
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from config import AppConfig
from core.duplicate_detector import (
    DuplicateGroup, DuplicateDecision, VideoInfo,
    detect_duplicates, apply_decisions,
)
from ui.i18n import t

logger = logging.getLogger(__name__)


class ChannelFlowController(QObject):
    """
    Signals
    -------
    tracks_ready(list)   Each item is a dict matching FetchWorker.track_found format.
    status_update(str)   Status bar messages.
    finished()           Flow completed (whether or not any tracks were added).
    cancelled()          User cancelled at any step.
    """

    tracks_ready  = Signal(list)   # list[dict]  — one per VideoInfo to download
    status_update = Signal(str)
    finished      = Signal()
    cancelled     = Signal()

    def __init__(
        self,
        channel_url:   str,
        channel_name:  str,          # pre-filled from classify_url; dialog may update it
        config:        AppConfig,
        parent_widget: QWidget,
        parent:        QObject = None,
    ) -> None:
        super().__init__(parent)
        self._url          = channel_url
        self._channel_name = channel_name
        self._cfg          = config
        self._widget       = parent_widget   # Qt parent for modal dialogs

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Blocking on the main thread: shows modal dialogs in sequence.
        The heavy work (Playwright, yt-dlp) runs in background QThreads
        owned by the dialogs themselves.
        """
        from ui.dialogs.tab_select_dialog import TabSelectDialog
        from PySide6.QtWidgets import QDialog

        self.status_update.emit(t("channel_discovering_tabs"))

        dialog = TabSelectDialog(
            channel_url=self._url,
            cookies_file=self._cfg.cookies_file,
            proxy_url=getattr(self._cfg, "youtube_proxy_url", None),
            parent=self._widget,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_update.emit(t("channel_import_cancelled"))
            self.cancelled.emit()
            self.finished.emit()
            return

        # Dialog completed: pick up results
        if dialog.channel_name:
            self._channel_name = dialog.channel_name

        tab_results: dict[str, list[VideoInfo]] = dialog.scrape_results
        total_raw = sum(len(v) for v in tab_results.values())

        self.status_update.emit(t("channel_items_found", n=total_raw))

        # ── Duplicate detection ────────────────────────────────────────────────
        groups: list[DuplicateGroup] = detect_duplicates(tab_results)

        if groups:
            self.status_update.emit(t("channel_duplicates_found", n=len(groups)))
            decisions = self._run_conflict_dialog(groups)
            if decisions is None:
                # User cancelled conflict dialog
                self.status_update.emit(t("channel_import_cancelled"))
                self.cancelled.emit()
                self.finished.emit()
                return
            tab_results = apply_decisions(tab_results, decisions)

        # ── Build track list ──────────────────────────────────────────────────
        tracks = self._build_tracks(tab_results)
        total_final = len(tracks)

        self.status_update.emit(t("channel_adding_to_queue", n=total_final))
        self.tracks_ready.emit(tracks)
        self.finished.emit()

    # ── Conflict resolution ───────────────────────────────────────────────────

    def _run_conflict_dialog(
        self, groups: list[DuplicateGroup]
    ) -> Optional[list[DuplicateDecision]]:
        from ui.dialogs.conflict_resolution_dialog import ConflictResolutionDialog
        from PySide6.QtWidgets import QDialog

        dlg = ConflictResolutionDialog(groups=groups, parent=self._widget)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.decisions

    # ── Track builder ─────────────────────────────────────────────────────────

    def _build_tracks(self, tab_results: dict[str, list[VideoInfo]]) -> list[dict]:
        """
        Convert VideoInfo objects into dicts that match FetchWorker.track_found
        format and _add_track_to_queue expectations.

        The download_controller.py will read parent_artist / category /
        release_type / album_index to build the correct folder path:

          Regular video → {channel}/{tab_name}/{title}.mp3
          Playlist item → {channel}/פלייליסטים/{playlist}/{01 - title}.mp3
        """
        tracks: list[dict] = []

        # Count items per playlist so total_tracks is accurate
        playlist_sizes: dict[str, int] = {}
        for videos in tab_results.values():
            for v in videos:
                if v.playlist_name:
                    playlist_sizes[v.playlist_name] = (
                        playlist_sizes.get(v.playlist_name, 0) + 1
                    )

        for videos in tab_results.values():
            for v in videos:
                if not v.video_id:
                    continue

                is_playlist_item = bool(v.playlist_name)

                if is_playlist_item:
                    track = {
                        "title":         v.title,
                        "artist":        "",          # filled by yt-dlp at download time
                        "duration":      _fmt_duration(v.duration_sec),
                        "platform":      "youtube",
                        "thumbnail_url": v.thumbnail_url,
                        "track_url":     v.url,
                        "album":         v.playlist_name,
                        "parent_artist": self._channel_name,
                        # "ep" triggers is_grouped=True and forced_index in download_controller
                        "release_type":  "ep",
                        # ``category`` stays in its canonical Hebrew form so the
                        # download controller's category-based comparisons work
                        # unchanged. The folder path is localized later via
                        # ``localized_folder_name`` at the path-building step.
                        "category":      "פלייליסטים",
                        "album_index":   v.playlist_index,
                        "total_tracks":  playlist_sizes.get(v.playlist_name, 0),
                    }
                else:
                    track = {
                        "title":         v.title,
                        "artist":        "",
                        "duration":      _fmt_duration(v.duration_sec),
                        "platform":      "youtube",
                        "thumbnail_url": v.thumbnail_url,
                        "track_url":     v.url,
                        "album":         self._channel_name,
                        "parent_artist": self._channel_name,
                        "release_type":  "video",
                        "category":      v.tab_name,
                        "album_index":   0,
                        "total_tracks":  0,
                    }

                tracks.append(track)

        return tracks


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_duration(sec: Optional[float]) -> str:
    if not sec:
        return ""
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
