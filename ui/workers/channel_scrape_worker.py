"""
ui/workers/channel_scrape_worker.py  –  Channel tab scraping worker (QThread)
===============================================================================
Scrapes selected YouTube channel tabs using yt-dlp flat-playlist extraction.
Emits incremental progress per tab so the UI can update a progress bar.

For the "פלייליסטים" tab:
  1. Fetches the list of playlists from /@handle/playlists
  2. Expands EACH playlist to get its individual video IDs
  3. Emits each playlist's videos under the "פלייליסטים" tab key, with
     playlist_name / playlist_index set on every VideoInfo

This makes duplicate detection simple: all items share the same tab key
("פלייליסטים") but differ by playlist_name.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.channel_tab_discoverer import TabInfo
from core.duplicate_detector import VideoInfo

logger = logging.getLogger(__name__)


class ChannelScrapeWorker(QThread):
    """
    Signals
    -------
    tab_started(tab_name, str)
        Emitted when we begin scraping a tab.
    tab_progress(tab_name, current, total)
        Emitted while expanding playlists (total = playlist count).
    tab_done(tab_name, count)
        Emitted when a tab is fully scraped with how many items were found.
    all_done(dict)
        Emitted when ALL tabs are finished.
        Payload: {tab_name: list[VideoInfo]}
    error(str)
        Emitted on a fatal error.
    """

    tab_started  = Signal(str)              # tab_name
    tab_progress = Signal(str, int, int)    # tab_name, current, total
    tab_done     = Signal(str, int)         # tab_name, item_count
    all_done     = Signal(object)           # dict[str, list[VideoInfo]]
    error        = Signal(str)

    def __init__(
        self,
        channel_url:  str,
        selected_tabs: list[TabInfo],
        cookies_file: Optional[str] = None,
        proxy_url:    Optional[str] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._channel_url   = channel_url
        self._selected_tabs = selected_tabs
        self._cookies_file  = cookies_file
        self._proxy_url     = proxy_url
        self._cancelled     = False

    def cancel(self) -> None:
        self._cancelled = True

    # ── QThread.run ───────────────────────────────────────────────────────────

    def run(self) -> None:
        results: dict[str, list[VideoInfo]] = {}

        try:
            for tab in self._selected_tabs:
                if self._cancelled:
                    break

                self.tab_started.emit(tab.name)

                if tab.tab_type in ("playlists", "releases", "podcasts"):
                    # Releases (albums) and Podcasts (episode collections) are
                    # structured exactly like Playlists: yt-dlp's flat listing
                    # returns each item as its own sub-playlist (a real
                    # playlist ID under "url"/"id", not a video), which needs
                    # expanding. Routing these through _scrape_flat_tab instead
                    # (as before) treated each playlist ID as if it were a
                    # video ID, fabricating broken /watch?v=<playlist-id> URLs
                    # rather than the real videos inside each release/episode.
                    videos = self._scrape_playlists_tab(tab)
                else:
                    videos = self._scrape_flat_tab(tab)

                results[tab.name] = videos
                self.tab_done.emit(tab.name, len(videos))

            if not self._cancelled:
                self.all_done.emit(results)

        except Exception as exc:  # noqa: BLE001
            logger.error("[ChannelScrapeWorker] Fatal error: %s", exc)
            self.error.emit(str(exc))

    # ── Regular tab scraping (videos / shorts / streams / releases) ───────────

    def _scrape_flat_tab(self, tab: TabInfo) -> list[VideoInfo]:
        """Scrape a non-playlist tab using yt-dlp flat-playlist."""
        import yt_dlp
        from utils.yt_dlp_opts import build_parse_ydl_opts, private_cookie_ydl_opts

        opts = build_parse_ydl_opts(
            cookies_file=self._cookies_file,
            proxy=self._proxy_url,
        )
        opts["quiet"] = True

        videos: list[VideoInfo] = []

        try:
            with private_cookie_ydl_opts(opts) as private_opts:
                with yt_dlp.YoutubeDL(private_opts) as ydl:
                    info = ydl.extract_info(tab.url, download=False)

            if not info:
                return videos

            entries = info.get("entries") or []
            for entry in entries:
                if self._cancelled:
                    break
                v = self._entry_to_video(entry, tab.name, tab.tab_type)
                if v:
                    videos.append(v)

        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChannelScrapeWorker] Tab %s failed: %s", tab.name, exc)

        return videos

    # ── Playlists tab scraping (list + expand each playlist) ─────────────────

    def _scrape_playlists_tab(self, tab: TabInfo) -> list[VideoInfo]:
        """
        1. Fetch the list of playlists from the /playlists tab.
        2. For each playlist, expand it to get individual video IDs.
        3. Return all videos with playlist_name / playlist_index set.
        """
        import yt_dlp
        from utils.yt_dlp_opts import build_parse_ydl_opts, private_cookie_ydl_opts

        opts = build_parse_ydl_opts(
            cookies_file=self._cookies_file,
            proxy=self._proxy_url,
        )
        opts["quiet"] = True

        # ── Step 1: Get playlist list ──────────────────────────────────────────
        playlist_entries: list[dict] = []
        try:
            with private_cookie_ydl_opts(opts) as private_opts:
                with yt_dlp.YoutubeDL(private_opts) as ydl:
                    info = ydl.extract_info(tab.url, download=False)
            if info:
                playlist_entries = [e for e in (info.get("entries") or []) if e]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChannelScrapeWorker] Playlists tab list failed: %s", exc)
            return []

        total_playlists = len(playlist_entries)
        all_videos: list[VideoInfo] = []

        # ── Step 2: Expand each playlist ──────────────────────────────────────
        for pl_idx, pl_entry in enumerate(playlist_entries, start=1):
            if self._cancelled:
                break

            self.tab_progress.emit(tab.name, pl_idx, total_playlists)

            pl_id    = pl_entry.get("id") or ""
            pl_title = pl_entry.get("title") or f"Playlist {pl_idx}"
            pl_url   = (
                pl_entry.get("url")
                or pl_entry.get("webpage_url")
                or (f"https://www.youtube.com/playlist?list={pl_id}" if pl_id else "")
            )
            if not pl_url:
                continue

            try:
                with private_cookie_ydl_opts(opts) as private_opts:
                    with yt_dlp.YoutubeDL(private_opts) as ydl:
                        pl_info = ydl.extract_info(pl_url, download=False)

                if not pl_info:
                    continue

                entries = [e for e in (pl_info.get("entries") or []) if e]
                for pos, entry in enumerate(entries, start=1):
                    if self._cancelled:
                        break
                    v = self._entry_to_video(
                        entry,
                        tab_name=tab.name,
                        tab_type="playlist_item",
                        playlist_name=pl_title,
                        playlist_url=pl_url,
                        playlist_index=pos,
                    )
                    if v:
                        all_videos.append(v)

            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ChannelScrapeWorker] Playlist '%s' expand failed: %s", pl_title, exc
                )

        return all_videos

    # ── VideoInfo factory ─────────────────────────────────────────────────────

    @staticmethod
    def _entry_to_video(
        entry: dict,
        tab_name: str,
        tab_type: str,
        playlist_name: str = "",
        playlist_url:  str = "",
        playlist_index: int = 0,
    ) -> Optional[VideoInfo]:
        vid = entry.get("id") or entry.get("video_id") or ""
        if not vid:
            return None

        title = entry.get("title") or entry.get("fulltitle") or vid

        # Thumbnail: prefer maxresdefault, fall back to what yt-dlp provides
        thumb = entry.get("thumbnail") or ""
        if not thumb and vid:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        return VideoInfo(
            video_id=vid,
            title=title,
            url=f"https://www.youtube.com/watch?v={vid}",
            thumbnail_url=thumb,
            duration_sec=entry.get("duration"),
            tab_name=tab_name,
            tab_type=tab_type,
            playlist_name=playlist_name,
            playlist_url=playlist_url,
            playlist_index=playlist_index,
        )
