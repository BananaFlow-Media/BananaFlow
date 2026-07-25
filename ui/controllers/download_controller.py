"""
ui/controllers/download_controller.py
=======================================
Manages the full download lifecycle:
  * Building DownloadRequest objects from queue cards + config
  * Running DownloadWorker (batch) and per-track resume workers
  * Pause / resume per-track and global pause
  * Routing DownloadWorker signals to card state (set_progress / set_status)
  * Emitting higher-level signals that AppWindow connects to panels

AppWindow mediates cross-controller interactions and all widget-level UI
(InfoBar, MessageBox, tray notifications, queue state persistence).

Zero Qt widget imports in this file — self.parent() is used only for
MessageBox parent in the duplicate-check dialog (unavoidable Qt requirement).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from config import AppConfig
from core.batch_outcome import BatchOutcome
from core.downloader import (
    DownloadEngine,
    DownloadRequest,
    MediaType,
)
from core.history_db import HistoryDB
from core.playlist_parser import SourcePlatform, UrlKind
from core.quality_presets import (
    AudioQuality,
    VideoQuality,
    audio_quality_from_id,
    default_audio_quality_id_for_codec,
    video_quality_from_id,
)
from ui.i18n import localized_folder_name, t

logger = logging.getLogger(__name__)


_MULTI_KINDS = {UrlKind.PLAYLIST, UrlKind.ALBUM, UrlKind.ARTIST}


def _parse_stream_type(category: str) -> Optional[str]:
    """Extract stream_type from a category string like 'stream:hls'."""
    if category and category.startswith("stream:"):
        return category[len("stream:"):]
    return None


class DownloadController(QObject):
    """
    Owns all download logic extracted from AppWindow.

    Signals to AppWindow / panels
    ------------------------------
    batch_snapshot     : BatchSnapshot — AppWindow → StatusBar.show_batch_progress()
    downloading_changed: bool — → dl_bar.set_downloading()
    show_success_bar   : str output_path — AppWindow shows InfoBar
    show_error_dialog  : object ErrorInfo — AppWindow shows MessageBox
    batch_finished     : BatchOutcome — AppWindow maps outcome → footer + queue state
    batch_started      : () — AppWindow saves queue state + shows "preparing…"
    track_thumbnail    : (int, str) — AppWindow refreshes a card thumbnail

    Legacy signals (deprecated — audited 2026-07-12, zero connections found
    anywhere in ui/ or tests/): the footer is now driven entirely by
    ``batch_snapshot`` + ``batch_finished`` (see ui/panels/status_bar.py).
    These are kept rather than removed so this verification pass doesn't
    turn into an unscoped public-API change to DownloadController /
    DownloadWorker's signal forwarding; delete them together in a follow-up
    if no external/CLI consumer appears.
      * status_update    — no longer emitted anywhere (dead both ends).
      * metrics_update, overall_progress, job_count_changed — still forwarded
        live from DownloadWorker each tick, but nothing connects to them.
      * cancel_visible    — still emitted True/False around a batch, but
        nothing connects to it (AppWindow derives cancel visibility from
        StatusBar's own state machine now).
    """

    status_update       = Signal(str)            # DEPRECATED — see class docstring
    metrics_update      = Signal(str, str)        # DEPRECATED — see class docstring
    overall_progress    = Signal(float)           # DEPRECATED — see class docstring
    batch_snapshot      = Signal(object)         # core.batch_progress.BatchSnapshot
    cancel_visible      = Signal(bool)            # DEPRECATED — see class docstring
    downloading_changed = Signal(bool)
    job_count_changed   = Signal(int, int)        # DEPRECATED — see class docstring
    show_success_bar    = Signal(str)       # output_path
    show_error_dialog   = Signal(object, str)    # ErrorInfo, Failing URL
    batch_finished      = Signal(object)    # core.batch_outcome.BatchOutcome
    batch_started       = Signal()
    track_thumbnail     = Signal(int, str)

    def __init__(
        self,
        config:  AppConfig,
        engine:  DownloadEngine,
        db:      Optional[HistoryDB] = None,
        parent:  QObject = None,
    ) -> None:
        super().__init__(parent)
        self._cfg    = config
        self._engine = engine
        self._db     = db

        self._dl_worker:      Optional = None
        self._resume_workers: list     = []

        # key (str(id(card))) → TrackCard
        self._key_to_card:   dict = {}
        # key → last reported fraction (throttle UI updates)
        self._card_progress: dict = {}
        # key → DownloadRequest snapshot saved at pause time
        self._paused_requests: dict[str, DownloadRequest] = {}
        # The single authoritative on-disk store of paused download state
        # (replaces the old write-only config.paused_items). Persisted after
        # every pause change; loaded on startup to survive a restart.
        from core.paused_batch_store import PausedBatchStore
        self._paused_store = PausedBatchStore()
                
        self._fatal_error_triggered = False  # Track if a fatal dialog was already shown
        self._fatal_lock = threading.Lock()  # Synchronize fatal error reporting
        # Why the current batch is ending, if the user/an error forced it.
        # None means "let it run to natural completion". First writer wins
        # (a later cancel callback must not overwrite the real fatal cause),
        # so all writes go through _set_termination_intent().
        self._termination_intent: Optional[BatchOutcome] = None

        # Fast-start timing: wall-clock of the last Download click and one-shot
        # flags for the two headline latencies logged per batch — the engine
        # starting (status "downloading") and the first byte actually arriving
        # (first non-zero progress). Diagnostics only — never affects flow.
        self._batch_click_ts: Optional[float] = None
        self._engine_start_logged: bool = False
        self._first_byte_logged: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start_batch(
        self,
        selected:             list,             # list[TrackCard]
        opts:                 dict,             # from OptionsBar.get_options()
        last_url_kind:        Optional[UrlKind],
        last_playlist_title:  str,
    ) -> None:
        """
        Build DownloadRequest objects for every selected card and start a
        DownloadWorker.  All job-building logic that was in AppWindow._on_download
        lives here.
        """
        from ui.dialogs.styled_dialog import show_warning

        t_click = time.monotonic()

        if not selected:
            # "Nothing selected" is not a global-status condition \u2014 the
            # Download button is disabled when nothing is selected, so this
            # is only reachable as an edge case. Fail quietly.
            return

        # Reset the fatal error lock for the new batch
        with self._fatal_lock:
            self._fatal_error_triggered = False
        self._termination_intent = None
        
        # Let yt-dlp try browser-cookie extraction even when the browser is
        # open. Some systems can read the live profile; when they cannot,
        # yt-dlp returns a concrete cookie/DPAPI error and the UI explains
        # the export-file fallback. Blocking here made regular Chrome sign-in
        # unusable because opening Chrome immediately prevented downloads.
        media_type = MediaType(opts["media_type"])
        is_audio   = media_type == MediaType.AUDIO
        quality_id = str(opts["quality_label"])
        audio_codec = str(opts["audio_format"])
        video_format = str(opts["video_format"])
        audio_q = AudioQuality.MP3_320
        video_q = VideoQuality.P1080
        if is_audio:
            resolved_audio = audio_quality_from_id(quality_id, audio_codec)
            if resolved_audio is None:
                fallback_id = default_audio_quality_id_for_codec(audio_codec)
                logger.warning(
                    "[DownloadController] Invalid audio quality id %r for codec %r; using %s",
                    quality_id,
                    audio_codec,
                    fallback_id,
                )
                resolved_audio = audio_quality_from_id(fallback_id, audio_codec)
            audio_q = resolved_audio or AudioQuality.MP3_320
        else:
            resolved_video = video_quality_from_id(quality_id)
            if resolved_video is None:
                logger.warning(
                    "[DownloadController] Invalid video quality id %r; using %s",
                    quality_id,
                    VideoQuality.P1080.value,
                )
            video_q = resolved_video or VideoQuality.P1080
        # Verify the base output dir is writable (opts["output_dir"] from OptionsBar)
        base_output_dir = opts["output_dir"]
        try:
            Path(base_output_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError) as exc:
            show_warning(
                self.parent(),
                t("cannot_write_output_title"),
                t("cannot_write_output_detail", path=base_output_dir, exc=exc),
            )
            return

        is_multi = last_url_kind in _MULTI_KINDS
        self._key_to_card.clear()
        self._card_progress.clear()

        unique_artists  = {c.artist for c in selected if c.artist}
        is_multi_batch  = len(unique_artists) > 1
        is_solo         = len(selected) == 1

        jobs: list[tuple[str, DownloadRequest]] = []
        # Duplicate-skip resolutions (policy "skip", or "warn" -> skip all):
        # the file already exists on disk and no download will run for it.
        # These must still be registered with the orchestrator as terminal
        # successes — see _on_track_preexisting — so batch totals stay
        # correct (e.g. 40 already-existing + 19 downloaded == 59/59, not
        # 19/19).
        preexisting_jobs: list[tuple[str, str]] = []
        # Cards found duplicate under the "warn" policy: resolution is
        # deferred until every card has been scanned, so the whole batch's
        # duplicates can be presented in ONE dialog (see below) instead of
        # one confirm() popup per file.
        pending_warn: list[dict] = []

        def _build_request(
            card, output_dir: str, track_playlist_name: Optional[str],
            is_parent_discography: bool, track_index: Optional[int], is_clean: bool,
        ) -> DownloadRequest:
            # Map the card.platform string to a SourcePlatform enum so the
            # orchestrator can persist the correct platform on the history
            # record. Unknown / missing values pass through as None and the
            # orchestrator records them as "unknown".
            card_platform_str = (card.platform or "").lower()
            req_platform = {
                "youtube": SourcePlatform.YOUTUBE,
                "ytmusic": SourcePlatform.YOUTUBE_MUSIC,
                "spotify": SourcePlatform.SPOTIFY,
                "generic": SourcePlatform.GENERIC,
            }.get(card_platform_str)

            from utils.url_cleaner import clean_youtube_url

            req = DownloadRequest(
                url=clean_youtube_url(card.track_url),
                output_dir=output_dir,
                media_type=media_type,
                audio_quality=audio_q,
                video_quality=video_q,
                audio_format=audio_codec,
                video_format=video_format,
                embed_thumbnail=self._cfg.embed_thumbnail,
                embed_metadata=self._cfg.embed_metadata,
                forced_title=card.title,
                forced_artist=card.artist,
                forced_album=card.album,
                forced_duration=getattr(card, "duration_sec", None),
                forced_index=track_index,
                cookies_file=self._cfg.cookies_file or None,
                cookies_browser=self._cfg.cookies_browser or None,
                playlist_name=self._get_dynamic_folder(
                    card, track_playlist_name, is_parent_discography
                ),
                thumbnail_url=card.thumbnail_url,
                platform=req_platform,
                sponsorblock=self._cfg.sponsorblock_enabled,
                sponsorblock_categories=self._cfg.get("sponsorblock_categories") or None,
                embed_lyrics=self._cfg.lyrics_enabled,
                replay_gain=self._cfg.replay_gain_enabled,
                musicbrainz=self._cfg.musicbrainz_enabled,
                square_thumbnails=self._cfg.square_thumbnails,
                expand_thumbnails=self._cfg.expand_thumbnails,
                clean_filename=is_clean,
                proxy_url=self._cfg.get("youtube_proxy_url") or None,
                is_solo=is_solo,
                stream_type=_parse_stream_type(getattr(card, "category", "")),
                category=getattr(card, "category", "") or None,
                youtube_reliability_mode=self._cfg.youtube_reliability_mode,
            )

            # Two-stage Spotify import: a card whose YouTube match was deferred
            # carries match_status == "pending". Attach a lazy resolver so the
            # download pool matches it to YouTube the instant before it starts
            # downloading — pipelining the (possibly large) catalog's matching
            # with the downloads instead of blocking every download on it.
            if getattr(card, "match_status", "matched") == "pending":
                td = {
                    "spotify_id":       getattr(card, "spotify_id", ""),
                    "spotify_key_kind": getattr(card, "spotify_key_kind", "spotify_id"),
                    "title":            card.title,
                    "artist":           card.artist,
                    "duration_sec":     getattr(card, "duration_sec", None),
                }
                cookies = self._cfg.cookies_file or None

                def _resolve(ev, _td=td, _cookies=cookies):
                    from core.scraper import resolve_track_to_youtube
                    from utils.url_cleaner import clean_youtube_url as _clean
                    resolved = resolve_track_to_youtube(
                        _td, cookies_file=_cookies,
                        cancel_check=lambda: ev is not None and ev.is_set(),
                    )
                    return _clean(resolved)

                req.url_resolver = _resolve

            return req

        for card in selected:
            key = str(id(card))
            track_playlist_name:   Optional[str] = None
            is_parent_discography: bool          = False

            if self._cfg.playlist_subfolders:
                parent_artist = (card.parent_artist or "").strip()
                kind          = (card.release_type  or "").strip()
                album         = (card.album         or "").strip()
                platform      = (card.platform      or "").lower()
                category      = (card.category      or "").strip()

                if parent_artist:
                    is_live     = "live" in card.title.lower() or "הופעה" in card.title
                    is_spotify  = "spotify" in platform
                    
                    # ── CATEGORY MAPPING ──────────────────────────────────────
                    if kind == "album":
                         cat_name = "אלבומים"
                    elif (is_live or kind == "performance") and not is_spotify:
                        cat_name = "הופעות חיות"
                    elif category in ("סינגלים ו-EP", "סינגלים וגרסאות EP", "סינגלים ומיני אלבומים"):
                        cat_name = "סינגלים ומיני אלבומים"
                    elif category: # Scraper provided category (e.g. "שורטס")
                        cat_name = category
                    elif kind == "video":
                        cat_name = "סרטונים"
                    elif kind == "playlist":
                        cat_name = "פלייליסטים"
                    else:
                        cat_name = "סינגלים ומיני אלבומים"

                     # ── FOLDER DEPTH LOGIC ────────────────────────────────────
                    # User wants strict separation:
                    # 1. 'אלבומים' ALWAYS get a subfolder if an album name is known.
                    # 2. 'סינגלים ומיני אלבומים' only get a subfolder if they have multiple tracks (EP).
                    # 3. YTM/Other releases use the count heuristic.
                    
                    is_grouped = (
                        (kind == "album") or
                        (kind == "ep") or
                        (cat_name == "אלבומים") or
                        (cat_name in ("סינגלים ומיני אלבומים", "סינגלים ו-EP", "סינגלים וגרסאות EP") and (card.total_tracks > 1 or kind == "ep")) or
                        (card.total_tracks > 1 and album) or
                        (kind == "playlist" and card.total_tracks > 1)
                    )
                    
                    # Ensure 1-track playlists are NOT grouped into folders
                    if kind == "playlist" and card.total_tracks == 1:
                        is_grouped = False
                    
                    cat_folder = localized_folder_name(cat_name)
                    if cat_name == "סינגלים ומיני אלבומים" and not self._cfg.singles_subfolder:
                        if is_grouped and album:
                            track_playlist_name = f"{parent_artist}/{album}"
                        else:
                            track_playlist_name = parent_artist
                    else:
                        if is_grouped and album:
                            track_playlist_name = f"{parent_artist}/{cat_folder}/{album}"
                        else:
                            track_playlist_name = f"{parent_artist}/{cat_folder}"

                    is_parent_discography = True

                elif is_multi:
                    # Generic multi-item (Playlist/Album) logic
                    track_playlist_name = last_playlist_title or "Playlist"
                elif card.artist:
                    pass  # single track — no subfolder
            else:
                track_playlist_name = ""

            # User wants NO folders for solo downloads
            if is_solo:
                track_playlist_name = ""

            # Use the same path the writability check ran against. opts["output_dir"]
            # comes from OptionsBar and reflects either the user's typed path
            # (persisted to config on editingFinished) or the browse-button
            # selection. Falling back to cfg.output_dir would silently drop a
            # path the user typed but hasn't committed yet.
            output_dir = str(Path(base_output_dir).expanduser())

            # Clean filename (index + title only, never include artist name).
            # The duplicate checker must use the same convention or it will
            # never find an existing file on disk.
            is_clean = True

            # Calculate the index to use for filename prefixing and metadata tags
            track_index = None
            if not is_solo:
                if card.release_type in ("album", "ep") and card.album_index > 0:
                    track_index = card.album_index
                elif card.release_type == "playlist":
                    # User explicitly requested no numbering for playlists
                    track_index = None
                elif self._cfg.playlist_index_prefix:
                    track_index = card.queue_index

            # Duplicate detection — pass include_artist=False to match the
            # is_clean filename layout produced below.
            if self._cfg.duplicate_action != "overwrite":
                from core.duplicate_checker import find_duplicate
                dup = find_duplicate(
                    output_dir=output_dir,
                    title=card.title,
                    artist=card.artist,
                    index=track_index,
                    include_index=track_index is not None,
                    include_artist=not is_clean,
                    duration_s=None,
                    playlist_name=self._get_dynamic_folder(
                        card, track_playlist_name, is_parent_discography
                    ),
                )
                if dup is not None:
                    if self._cfg.duplicate_action == "skip":
                        card.set_status("done")
                        card.set_progress(1.0)
                        self._key_to_card[key] = card
                        preexisting_jobs.append((key, str(dup)))
                        continue
                    else:  # "warn" — collect for the one batched dialog below;
                        # do not build the request or decide skip/replace yet.
                        pending_warn.append({
                            "card": card, "key": key, "dup": str(dup),
                            "output_dir": output_dir,
                            "track_playlist_name": track_playlist_name,
                            "is_parent_discography": is_parent_discography,
                            "track_index": track_index,
                            "is_clean": is_clean,
                        })
                        continue

            req = _build_request(
                card, output_dir, track_playlist_name,
                is_parent_discography, track_index, is_clean,
            )
            self._key_to_card[key] = card
            jobs.append((key, req))

        # One consolidated dialog for every "warn"-policy duplicate found in
        # this batch — never one confirm() popup per file (see
        # ui.dialogs.batch_duplicate_dialog).
        if pending_warn:
            from ui.dialogs.batch_duplicate_dialog import ask_batch_duplicate_action
            skip_all = ask_batch_duplicate_action(
                self.parent(),
                [(p["card"].title, p["dup"]) for p in pending_warn],
            )
            for p in pending_warn:
                card, key = p["card"], p["key"]
                if skip_all:
                    card.set_status("done")
                    card.set_progress(1.0)
                    self._key_to_card[key] = card
                    preexisting_jobs.append((key, p["dup"]))
                else:
                    req = _build_request(
                        card, p["output_dir"], p["track_playlist_name"],
                        p["is_parent_discography"], p["track_index"], p["is_clean"],
                    )
                    self._key_to_card[key] = card
                    jobs.append((key, req))

        if not jobs and not preexisting_jobs:
            return

        self._engine._cancel_event.clear()  # noqa: SLF001

        # Fast-start diagnostics: how long from the click to a built job queue,
        # and (later, on the first "downloading" status) to the first download
        # actually starting. See _on_track_status.
        self._batch_click_ts = t_click
        self._engine_start_logged = False
        self._first_byte_logged = False
        logger.info(
            "[timing][click] queue built: %d job(s), %d preexisting in %.3fs",
            len(jobs), len(preexisting_jobs), time.monotonic() - t_click,
        )

        # Only reset cards that will actually download — a duplicate-skip
        # card was already set to "done" above and must not be clobbered
        # back to "queued" here.
        for key, _ in jobs:
            card = self._key_to_card[key]
            card.set_status("queued")
            card.set_progress(0.0)

        self.cancel_visible.emit(True)
        self.downloading_changed.emit(True)

        self._dl_worker = self._build_batch_worker(jobs, preexisting_jobs)
        self._dl_worker.start()

        self.batch_started.emit()

    def _build_batch_worker(self, jobs, preexisting_jobs):
        """Create and fully wire a batch DownloadWorker. Shared by
        start_batch() and resume_all() so a resumed batch is a first-class
        batch — same footer/progress/snapshot wiring as a fresh one — not a
        stripped-down side worker."""
        from ui.workers.download_worker import DownloadWorker
        worker = DownloadWorker(
            jobs=jobs,
            preexisting=preexisting_jobs,
            engine=self._engine,
            config=self._cfg,
            db=self._db,
            max_workers=self._cfg.max_parallel_downloads,
            parent=self,
        )
        worker.track_progress.connect(self._on_track_progress)
        worker.track_speed.connect(self._on_track_speed)
        worker.track_status.connect(self._on_track_status)
        worker.track_finished.connect(self._on_track_finished)
        worker.track_preexisting.connect(self._on_track_preexisting)
        worker.overall_progress.connect(self._on_worker_overall_progress)
        worker.metrics.connect(self._on_worker_metrics)
        worker.batch_snapshot.connect(self._on_worker_batch_snapshot)
        worker.job_count_changed.connect(self._on_worker_job_count_changed)
        worker.job_error.connect(self._on_track_error)
        worker.all_finished.connect(self._on_batch_done)
        worker.track_thumbnail.connect(self._on_track_thumbnail)
        return worker

    def is_downloading(self) -> bool:
        """True while a batch DownloadWorker is actively running."""
        return self._dl_worker is not None and self._dl_worker.isRunning()

    def _set_termination_intent(self, intent: BatchOutcome) -> None:
        """Record why the batch is ending.

        Fatal stops have highest priority. A deliberate user cancel can
        override a just-requested pause (rapid pause -> cancel), while a pause
        cannot override an existing cancel/fatal intent.
        """
        with self._fatal_lock:
            current = self._termination_intent
            if current is None:
                self._termination_intent = intent
            elif current == BatchOutcome.STOPPED_BY_FATAL_ERROR:
                return
            elif intent == BatchOutcome.STOPPED_BY_FATAL_ERROR:
                self._termination_intent = intent
            elif (
                current == BatchOutcome.PAUSED_BY_USER
                and intent == BatchOutcome.CANCELLED_BY_USER
            ):
                self._termination_intent = intent

    @staticmethod
    def _snapshot_for_pause(req: DownloadRequest) -> DownloadRequest:
        """A complete, standalone copy of a live request, prepared to resume.

        Uses dataclasses.replace so EVERY field survives (workspace_dir,
        thumbnail_url, forced_album, platform, category, cookies_browser,
        is_solo, url_resolver, …) — the old hand-written copy silently
        dropped several, so a resumed track lost its custom artwork/album/
        crop behaviour. ``resumable=True`` makes yt-dlp pick up the .part
        file; the per-run transients (cancel_event, callbacks) are cleared
        so the next orchestrator owns them cleanly, and the init=False
        output-path trackers reset to their defaults automatically."""
        import dataclasses
        return dataclasses.replace(
            req,
            resumable=True,
            cancel_event=None,
            on_progress=None,
            on_finished=None,
            on_error=None,
        )

    def global_pause(self) -> None:
        """Pause the running batch. Internally this cancels the in-flight
        downloads (leaving resumable .part files in each job's workspace),
        but the *outcome* is a pause, never a cancellation — the distinction
        is preserved for the UI so the user is offered Resume, not a fresh
        start.

        Every unfinished job is snapshotted into ``_paused_requests`` so
        Resume All continues the SAME jobs (same workspace, same partial
        downloads) rather than rebuilding a brand-new batch through
        start_batch() — which would re-run the duplicate policy and discard
        the partial state. Completed jobs (cards already "done", including
        duplicate-skips) are deliberately NOT captured, so they are never
        downloaded again."""
        self._set_termination_intent(BatchOutcome.PAUSED_BY_USER)
        if not (self._dl_worker and self._dl_worker.isRunning()):
            return
        for key, req in list(self._dl_worker._jobs):  # noqa: SLF001
            card = self._key_to_card.get(key)
            if card is not None and card.get_status() == "done":
                continue  # completed / preexisting — never resume it
            self._paused_requests[key] = self._snapshot_for_pause(req)
            if card is not None:
                card.set_status("paused")
        self._persist_paused_state()
        self._dl_worker.cancel()

    def cancel_all(self) -> None:
        """Cancel the engine (all in-flight yt-dlp downloads) and the worker."""
        self._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)
        self._engine.cancel_all()
        if self._dl_worker and self._dl_worker.isRunning():
            self._dl_worker.cancel()

    def pause_track(self, card) -> None:
        """
        Save the in-flight request for this card and cancel only that track.
        AppWindow looks up the card from _index_to_card and passes it here.
        """
        key = str(id(card))
        req = self._active_request_for_key(key)
        if req is not None:
            # Full-fidelity snapshot (every field, incl. workspace_dir) so
            # the resumed track continues from its .part file with all its
            # metadata/artwork behaviour intact — see _snapshot_for_pause.
            self._paused_requests[key] = self._snapshot_for_pause(req)

        if self._dl_worker:
            self._dl_worker.cancel_track(key)
        card.set_status("paused")
        self._persist_paused_state()

    # ── Paused-state persistence (single authoritative store) ─────────────────

    @staticmethod
    def _card_to_dict(card) -> dict:
        """Display metadata needed to rebuild a paused card after restart —
        the same shape AppWindow persists for the general queue."""
        return {
            "title":         getattr(card, "title", ""),
            "artist":        getattr(card, "artist", ""),
            "url":           getattr(card, "track_url", ""),
            "duration_str":  getattr(card, "duration", ""),
            "thumbnail_url": getattr(card, "thumbnail_url", ""),
            "platform":      getattr(card, "platform", "youtube"),
            "album":         getattr(card, "album", ""),
            "parent_artist": getattr(card, "parent_artist", ""),
            "release_type":  getattr(card, "release_type", ""),
            "category":      getattr(card, "category", ""),
            "album_index":   getattr(card, "album_index", 0),
            "total_tracks":  getattr(card, "total_tracks", 0),
        }

    def _persist_paused_state(self) -> None:
        """Write the current in-memory paused set to the authoritative store,
        or clear the store when nothing is paused. Best-effort — a persistence
        failure must never break pausing itself (the store swallows errors)."""
        from core.download_request_codec import request_to_dict
        from core.paused_batch_store import PausedJob

        jobs = []
        for key, req in self._paused_requests.items():
            card = self._key_to_card.get(key)
            jobs.append(PausedJob(
                key=key,
                request=request_to_dict(req),
                card=self._card_to_dict(card) if card is not None else {},
                workspace_dir=req.workspace_dir or "",
            ))
        if jobs:
            self._paused_store.save(jobs)
        else:
            self._paused_store.clear()

    def restore_paused_on_startup(self, card_factory) -> list:
        """Startup entry point: sweep abandoned workspaces and restore the
        valid persisted paused jobs.

        The sweep removes every batch workspace under the output directory
        that no valid paused job still needs — crashed runs and completed
        batches whose cleanup didn't finish — while keeping the workspaces of
        the jobs about to be restored. Runs even when there is nothing to
        restore, so stale workspaces are always reclaimed. Returns the
        restored cards (empty if none)."""
        store_jobs = self._paused_store.load()
        try:
            from utils.paths import sweep_stale_workspaces
            base = getattr(self._cfg, "output_dir", None)
            base_dirs = [base] if base else []
            removed = sweep_stale_workspaces(base_dirs, self._paused_store.workspace_dirs())
            if removed:
                logger.info("[DownloadController] Swept %d stale workspace(s)", len(removed))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DownloadController] Stale-workspace sweep failed: %s", exc)

        if not store_jobs:
            return []
        return self.restore_paused_jobs(store_jobs, card_factory)

    def restore_paused_jobs(self, store_jobs, card_factory) -> list:
        """Rebuild the in-memory paused set from persisted records after a
        restart. ``card_factory(card_dict) -> card`` creates and registers a
        (paused) queue card; returns None to skip. A record whose workspace
        no longer exists (swept as stale, or the user deleted it) is skipped
        — there is nothing left to resume from. Returns the list of restored
        cards so the caller can wire their pause/resume signals."""
        from core.download_request_codec import request_from_dict

        restored: list = []
        for pj in store_jobs:
            if pj.workspace_dir and not Path(pj.workspace_dir).exists():
                continue
            card = card_factory(pj.card)
            if card is None:
                continue
            key = str(id(card))
            self._paused_requests[key] = request_from_dict(pj.request)
            self._key_to_card[key] = card
            restored.append(card)
        # Re-persist so the store reflects exactly what was restorable
        # (drops records whose workspace had vanished).
        self._persist_paused_state()
        return restored

    def resume_track(self, card) -> None:
        """
        Re-submit the paused DownloadRequest with continuedl=True.
        AppWindow looks up the card from _index_to_card and passes it here.
        """
        from ui.workers.download_worker import DownloadWorker

        key = str(id(card))
        req = self._paused_requests.pop(key, None)
        if req is None:
            logger.warning("[DownloadController] No paused request for card %s", key)
            return

        self._persist_paused_state()  # this key is no longer paused

        card.set_status("queued")
        card.set_progress(0.0)

        self._key_to_card[key] = card
        resume_worker = DownloadWorker(
            jobs=[(key, req)],
            engine=self._engine,
            config=self._cfg,
            db=self._db,
            max_workers=1,
            parent=self,
        )
        self._resume_workers.append(resume_worker)
        resume_worker.track_progress.connect(self._on_track_progress)
        resume_worker.track_speed.connect(self._on_track_speed)
        resume_worker.track_status.connect(self._on_track_status)
        resume_worker.track_finished.connect(self._on_track_finished)
        resume_worker.job_error.connect(self._on_track_error)
        resume_worker.all_finished.connect(self._on_batch_done)
        resume_worker.track_thumbnail.connect(self._on_track_thumbnail)
        resume_worker.all_finished.connect(
            lambda w=resume_worker: (
                self._resume_workers.remove(w) if w in self._resume_workers else None
            )
        )
        resume_worker.start()

    def resume_all(self) -> None:
        """Continue a globally-paused batch: re-submit every snapshotted
        paused job as ONE batch, continuing from its .part file in its
        existing workspace.

        This is deliberately NOT start_batch(): it never rebuilds requests
        from cards, never runs duplicate detection, and never shows the
        duplicate dialog. The jobs, their identities (keys), their
        workspaces and their partial downloads are exactly the ones that
        were paused. Completed / duplicate-skip cards were never captured
        (see global_pause), so they are not re-run."""
        if not self._paused_requests:
            return
        if self._dl_worker is not None and self._dl_worker.isRunning():
            # A batch is already running — don't stack a second one.
            return

        jobs: list[tuple[str, DownloadRequest]] = []
        for key, req in self._paused_requests.items():
            card = self._key_to_card.get(key)
            if card is not None:
                card.set_status("queued")
                card.set_progress(0.0)
            jobs.append((key, req))

        self._paused_requests.clear()
        self._card_progress.clear()
        self._persist_paused_state()  # everything resumed — store now empty

        # A resume is a fresh run for termination-intent purposes — clear any
        # lingering pause intent so a clean resume finishes as COMPLETED.
        with self._fatal_lock:
            self._fatal_error_triggered = False
        self._termination_intent = None
        self._engine._cancel_event.clear()  # noqa: SLF001

        self._batch_click_ts = time.monotonic()
        self._engine_start_logged = False
        self._first_byte_logged = False

        self.cancel_visible.emit(True)
        self.downloading_changed.emit(True)

        self._dl_worker = self._build_batch_worker(jobs, preexisting_jobs=[])
        self._dl_worker.start()

        self.batch_started.emit()

    # ── Private slots (wired to DownloadWorker signals) ───────────────────────

    def _is_active_worker_signal(self) -> bool:
        sender = self.sender()
        return (
            sender is None
            or sender is self._dl_worker
            or sender in self._resume_workers
        )

    def _is_current_batch_worker_signal(self) -> bool:
        sender = self.sender()
        return (
            sender is None
            or sender is self._dl_worker
            or (self._dl_worker is None and sender in self._resume_workers)
        )

    def _on_worker_overall_progress(self, fraction: float) -> None:
        if self._is_current_batch_worker_signal():
            self.overall_progress.emit(fraction)

    def _on_worker_metrics(self, speed: str, eta: str) -> None:
        if self._is_current_batch_worker_signal():
            self.metrics_update.emit(speed, eta)

    def _on_worker_batch_snapshot(self, snapshot) -> None:
        if self._is_current_batch_worker_signal():
            self.batch_snapshot.emit(snapshot)

    def _on_worker_job_count_changed(self, completed: int, total: int) -> None:
        if self._is_current_batch_worker_signal():
            self.job_count_changed.emit(completed, total)

    def _on_track_progress(self, key: str, fraction: float) -> None:
        if not self._is_active_worker_signal():
            return
        # First non-zero progress anywhere in the batch = the first byte, the
        # honest "download actually started" moment. Checked BEFORE the UI
        # throttle below so a tiny first tick is never swallowed. This is the
        # headline click->first-download latency the fast-start work targets.
        if (
            fraction > 0.0
            and not self._first_byte_logged
            and self._batch_click_ts is not None
        ):
            self._first_byte_logged = True
            logger.info(
                "[timing][click] first byte (download started): %.3fs after click",
                time.monotonic() - self._batch_click_ts,
            )
        prev = self._card_progress.get(key, 0.0)
        if fraction - prev < 0.01 and fraction < 1.0:
            return
        self._card_progress[key] = fraction
        card = self._key_to_card.get(key)
        if card:
            card.set_progress(fraction)
            if card._status != "downloading":  # noqa: SLF001
                card.set_status("downloading")

    def _on_track_speed(self, key: str, speed_bps: float, eta_seconds: float) -> None:
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card and hasattr(card, "update_speed"):
            card.update_speed(speed_bps, eta_seconds)

    def _on_track_status(self, key: str, status: str) -> None:
        if not self._is_active_worker_signal():
            return
        # First track to reach "downloading" = the engine started (URL resolved,
        # gate acquired, about to hand off to yt-dlp) — but NOT yet the first
        # byte (extract_info still runs). Logged separately from first-byte so
        # the two costs are visible; the first-byte line is the true headline.
        if (
            status == "downloading"
            and not self._engine_start_logged
            and self._batch_click_ts is not None
        ):
            self._engine_start_logged = True
            logger.info(
                "[timing][click] first engine start (downloading): %.3fs after click",
                time.monotonic() - self._batch_click_ts,
            )
        card = self._key_to_card.get(key)
        if card:
            card.set_status(status)

    def _on_track_finished(self, key: str, output_path: str) -> None:
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card:
            card.set_status("done")
            card.set_progress(1.0)
        self.show_success_bar.emit(output_path)

    def _on_track_preexisting(self, key: str, output_path: str) -> None:
        """Duplicate-skip resolution: the file already existed, nothing was
        downloaded. The card was already set to "done" synchronously in
        start_batch() for instant feedback — this just confirms it once the
        orchestrator has formally registered the job. No success toast: the
        old "skip" behavior was silent, and 40 toasts for pre-existing files
        would be noise, not signal."""
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card:
            card.set_status("done")
            card.set_progress(1.0)

    def _on_track_error(self, key: str, err: object) -> None:
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card:
            card.set_status("error")

        err_msg = str(err)
        if hasattr(err, "error_message"):
            err_msg = err.error_message
            
        # Detect fatal errors that should stop the entire batch.
        #
        # Only markers that mean "the whole cookie/auth mechanism is broken"
        # belong here (can't read the cookie file, IP fully blocked, JS
        # challenge solver unavailable) — every remaining track would fail
        # the same way, so cancelling early saves time.
        #
        # "confirm you're not a bot" / "Please sign in" / "cookies are no
        # longer valid" / "Requested format is not available" are
        # per-VIDEO outcomes (that one video is gated, or lacks the
        # requested format) — plenty of other tracks in the same batch can
        # still succeed, so these must NOT cancel the whole batch. They still
        # mark that one card as errored and show once via the throttled
        # per-track error dialog.
        is_fatal = False
        fatal_markers = [
            "cookie database",
            "DPAPI",
            "HTTP Error 403",
            "Signature solving failed",
            "n challenge solving failed",
        ]
        if any(marker in err_msg for marker in fatal_markers):
            is_fatal = True
            
        # Get the failing URL to pass to the UI
        failing_url = ""
        track_req = self._active_request_for_key(key)
        if track_req:
            failing_url = track_req.url

        # Storm prevention: only emit the first fatal dialog
        emit_dialog = False
        with self._fatal_lock:
            if not is_fatal or not self._fatal_error_triggered:
                if is_fatal:
                    self._fatal_error_triggered = True
                emit_dialog = True
                
        if emit_dialog:
            self.show_error_dialog.emit(err, failing_url)

        if is_fatal:
            logger.warning("[DownloadController] Fatal error detected. Stopping batch.")
            # Record the fatal cause BEFORE cancelling so the outcome is
            # reported as a technical stop, not a user cancellation — and so
            # the first fatal cause is never overwritten by the cancellation
            # callback that cancel_all() triggers.
            self._set_termination_intent(BatchOutcome.STOPPED_BY_FATAL_ERROR)
            self.cancel_all()

    def _on_track_thumbnail(self, key: str, thumb_url: str) -> None:
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card:
            self.track_thumbnail.emit(card.queue_index, thumb_url)

    def _on_batch_done(self, orchestrator_outcome=None) -> None:
        """Resolve the final batch outcome and hand it to the UI.

        The orchestrator can only report COMPLETED / COMPLETED_WITH_ERRORS /
        CANCELLED_BY_USER — it cannot tell a UI *pause* or a *fatal stop* from
        a plain cancel. That intent lives here, so a recorded termination
        intent (pause / cancel / fatal) always wins over the orchestrator's
        best guess. Absent any intent, the orchestrator's outcome stands
        (clean completion vs completion-with-failures)."""
        if not self._is_current_batch_worker_signal():
            return
        # Capture the finished worker's jobs BEFORE nulling _dl_worker so a
        # cancellation can clean up exactly that batch's abandoned work.
        # Safe to read here: all_finished fires only after run_batch has
        # drained its pool, so no download thread is still writing.
        finished_worker = self.sender()
        worker_jobs = list(getattr(finished_worker, "_jobs", []) or [])

        self._dl_worker = None
        self.cancel_visible.emit(False)
        self.downloading_changed.emit(False)
        self.metrics_update.emit("", "")

        outcome = self._termination_intent
        if outcome is None:
            outcome = orchestrator_outcome or BatchOutcome.COMPLETED
        self._termination_intent = None

        # A deliberate cancel (never a pause) removes the batch's unfinished
        # partial/intermediate work. Fully-published final files already
        # live in the user's output directory and are never touched (cleanup
        # only removes the hidden workspace container). Pause preserves the
        # workspace instead, which is exactly what keeps the two operations
        # fundamentally different.
        if outcome == BatchOutcome.CANCELLED_BY_USER:
            self._cleanup_cancelled_batch(worker_jobs)

        self.batch_finished.emit(outcome)

    def _cleanup_cancelled_batch(self, jobs: list) -> None:
        """Remove the abandoned workspace of a cancelled batch and drop any
        paused snapshots for its jobs — a cancel abandons everything.

        Only touches the BananaFlow-owned hidden workspace container (via the
        filesystem-safe remove_workspace_tree); published final files in the
        output directory are never affected. Jobs that never started leave
        only empty subdirs, which go with the container. Sibling batches are
        untouched — cleanup is scoped to this batch's own container(s)."""
        if not jobs:
            return
        from utils.paths import remove_workspace_tree

        containers: set[Path] = set()
        dropped = False
        for key, req in jobs:
            if self._paused_requests.pop(key, None) is not None:
                dropped = True
            if req.workspace_dir:
                # req.workspace_dir is this job's per-job subdir
                # (<container>/<job_key>); its parent is the batch container.
                containers.add(Path(req.workspace_dir).parent)
        for container in containers:
            remove_workspace_tree(container)
        if dropped:
            self._persist_paused_state()  # cancelled work is no longer paused

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _active_request_for_key(self, key: str) -> Optional[DownloadRequest]:
        """Retrieve the live DownloadRequest for a card key from the active worker."""
        if self._dl_worker is None:
            return None
        for card_key, req in self._dl_worker._jobs:  # noqa: SLF001
            if card_key == key:
                return req
        return None

    def _get_dynamic_folder(
        self,
        card,
        fallback: Optional[str] = None,
        is_discography: bool = False,
    ) -> str:
        """
        Construct a folder path. If 'fallback' is provided (from the main loop), it takes priority
        as it was constructed with full context.
        """
        if fallback is not None:
            # Fallback already contains the logic-built path (e.g. "Playlist Name", "Artist/Category/Album", or "" for Solo)
            return fallback

        artist   = (card.parent_artist or card.artist or "").strip()
        album    = (card.album or "").strip()
        rel_type = (card.release_type or "album").lower()

        path_parts: list[str] = []
        if is_discography and artist:
            path_parts.append(artist)
            if rel_type == "album":
                path_parts.append(localized_folder_name("אלבומים"))
            elif self._cfg.singles_subfolder:
                path_parts.append(localized_folder_name("סינגלים ו-EP"))
    
        if album:
            path_parts.append(album.replace("Album - ", "").strip())
        elif artist:
            path_parts.append(artist)

        # De-duplication (case-insensitive)
        seen: list[str] = []
        for part in path_parts:
            if not part: continue
            if not seen or part.lower() != seen[-1].lower():
                seen.append(part)

        return "/".join(seen)
