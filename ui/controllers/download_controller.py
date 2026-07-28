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
import uuid
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
from core.youtube_reliability import is_youtube_url
from ui.i18n import localized_folder_name, t

logger = logging.getLogger(__name__)


_MULTI_KINDS = {UrlKind.PLAYLIST, UrlKind.ALBUM, UrlKind.ARTIST}

# Card statuses a job never comes back from — captured here (rather than
# just "done") because Global Pause must not snapshot-and-resume a job that
# has already reached one of these; a resume of an errored/cancelled card
# would re-run work the user never asked to continue.
_TERMINAL_CARD_STATUSES = frozenset({"done", "error", "cancelled"})


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
        # BatchSnapshot.batch_id of the batch the footer is currently showing.
        # None until the live batch worker emits its first snapshot; see
        # _on_worker_batch_snapshot.
        self._batch_snapshot_id: Optional[str] = None

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
        # starting (status "starting") and the first actual downloaded byte
        # arriving. Diagnostics only — never affects flow.
        self._batch_click_ts: Optional[float] = None
        self._engine_start_logged: bool = False
        self._first_byte_logged: bool = False
        # A single-track resume is a separate user action, not part of the
        # previous batch's click-to-first-byte measurement.  Keep its timing
        # keyed by request so a late byte cannot be attributed to that batch.
        self._resume_click_ts: dict[str, float] = {}
        self._resume_engine_started: set[str] = set()

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

        downloadable = []
        for card in selected:
            match_status = getattr(card, "match_status", "matched")
            pending = match_status == "pending"
            has_url = bool((getattr(card, "track_url", "") or "").strip())
            if match_status in ("metadata_invalid", "unresolved") or (not pending and not has_url):
                if hasattr(card, "mark_unresolved"):
                    message_key = (
                        "spotify_metadata_invalid_card"
                        if match_status == "metadata_invalid"
                        else "spotify_unresolved_card"
                    )
                    card.mark_unresolved(t(message_key))
                continue
            downloadable.append(card)
        selected = downloadable

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
                is_solo=(
                    str(getattr(card, "source_kind", "") or "").upper()
                    in {UrlKind.SINGLE_VIDEO.name, UrlKind.UNKNOWN.name}
                    if getattr(card, "source_kind", "")
                    else is_solo
                ),
                stream_type=_parse_stream_type(getattr(card, "category", "")),
                category=getattr(card, "category", "") or None,
                source_kind=getattr(card, "source_kind", "") or None,
                source_url=getattr(card, "source_url", "") or None,
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
                req.spotify_match_identity = dict(td)
                req.url_resolver = self._build_spotify_resolver(td, self._cfg.cookies_file or None)
            elif req_platform == SourcePlatform.SPOTIFY:
                req.spotify_match_identity = {
                    "spotify_id":       getattr(card, "spotify_id", ""),
                    "spotify_key_kind": getattr(card, "spotify_key_kind", "spotify_id"),
                    "title":            card.title,
                    "artist":           card.artist,
                    "duration_sec":     getattr(card, "duration_sec", None),
                }

            return req

        for card in selected:
            key = str(id(card))
            track_playlist_name:   Optional[str] = None
            is_parent_discography: bool          = False
            source_kind = str(getattr(card, "source_kind", "") or "").upper()
            has_source_context = bool(source_kind)
            independent_source = source_kind in {
                UrlKind.SINGLE_VIDEO.name, UrlKind.UNKNOWN.name,
            }
            grouped_source = source_kind in {
                UrlKind.PLAYLIST.name, UrlKind.ALBUM.name, UrlKind.ARTIST.name,
            }

            if self._cfg.playlist_subfolders:
                parent_artist = (card.parent_artist or "").strip()
                kind          = (card.release_type  or "").strip()
                album         = (card.album         or "").strip()
                platform      = (card.platform      or "").lower()
                category      = (card.category      or "").strip()

                if independent_source:
                    # Album/artist fields describe this track but do not turn
                    # a direct URL (or TXT list of direct URLs) into a collection.
                    track_playlist_name = ""
                elif parent_artist:
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

                elif grouped_source or (not has_source_context and is_multi):
                    # Generic multi-item (Playlist/Album) logic
                    track_playlist_name = (
                        (card.album or "").strip()
                        or last_playlist_title
                        or "Playlist"
                    )
                elif card.artist:
                    pass  # single track — no subfolder
            else:
                track_playlist_name = ""

            # User wants NO folders for solo downloads
            if is_solo and not grouped_source:
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
            if grouped_source or (not has_source_context and not is_solo):
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
        # and (later, on the first "starting" status) to the first download
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
        # Mint this batch's identity HERE, before the worker exists, and hand
        # the same value to it. The footer then knows which batch it is showing
        # from the outset instead of adopting whichever snapshot happens to
        # arrive first — an id learned that way can be captured by a stale or
        # foreign snapshot that beats the real one, which would then cause every
        # genuine snapshot to be rejected for the rest of the batch. See
        # _on_worker_batch_snapshot.
        self._batch_snapshot_id = uuid.uuid4().hex
        worker = DownloadWorker(
            jobs=jobs,
            preexisting=preexisting_jobs,
            engine=self._engine,
            config=self._cfg,
            db=self._db,
            max_workers=self._cfg.max_parallel_downloads,
            batch_id=self._batch_snapshot_id,
            parent=self,
        )
        worker.track_progress.connect(self._on_track_progress)
        if hasattr(worker, "track_first_byte"):
            worker.track_first_byte.connect(self._on_track_first_byte)
        worker.track_speed.connect(self._on_track_speed)
        worker.track_status.connect(self._on_track_status)
        worker.track_phase.connect(self._on_track_phase)
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
    def _build_spotify_resolver(td: dict, cookies: Optional[str]):
        """Build a lazy url_resolver closure for a Spotify two-stage match
        from the minimal identity dict it needs (spotify_id,
        spotify_key_kind, title, artist, duration_sec).

        Shared by start_batch (building a fresh request from a live queue
        card) and restore_paused_jobs (rebuilding an EQUIVALENT resolver
        for a paused job that was never resolved before the app
        restarted). The resolver itself is a live closure and cannot be
        persisted, but everything it needs to rebuild an equivalent one
        can be -- see core.download_request_codec's had_pending_resolver
        flag and _card_to_dict's spotify_id/spotify_key_kind/duration_sec
        fields."""
        from core.scraper import track_match_source_hint

        source_hint = track_match_source_hint(td)

        def _resolve(ev, _td=td, _cookies=cookies):
            from core.scraper import resolve_track_to_youtube
            from utils.url_cleaner import clean_youtube_url as _clean
            resolved = resolve_track_to_youtube(
                _td, cookies_file=_cookies,
                cancel_check=lambda: ev is not None and ev.is_set(),
            )
            _resolve.resolve_source = _td.get("_match_source", "live")
            return _clean(resolved)
        _resolve.resolve_source = source_hint
        return _resolve

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
        output-path trackers reset to their defaults automatically.

        ``resume_phase`` / ``resume_final_path`` are ordinary fields and
        deliberately DO survive: they are how a track paused during
        post-processing or publishing resumes at that phase instead of
        re-running yt-dlp against an already-complete file (which finds
        nothing to do, fires no postprocessor hook, and fails)."""
        import dataclasses
        snapshot = dataclasses.replace(
            req,
            resumable=True,
            cancel_event=None,
            on_progress=None,
            on_finished=None,
            on_error=None,
            publish_gate=None,
            publish_release=None,
        )
        # dataclasses.replace rebuilds through __init__ and resets the
        # init=False output-path tracker. Re-attached because it is the only
        # in-memory record of what yt-dlp produced for a job paused in the
        # instant before its post-download checkpoint was written (see
        # DownloadRequest.snapshot_copy).
        snapshot._final_output_path = req._final_output_path  # noqa: SLF001
        return snapshot

    def _worker_for_key(self, key: str):
        """The running worker that currently owns job ``key`` — the main
        batch worker, or the single-job worker a per-track resume started.

        Pause used to address only the batch worker, so a track that had
        already been resumed once (and is therefore running in its own
        resume worker) could not be paused again: the lookup found nothing,
        no snapshot was taken and no cancel was delivered, so the card was
        labelled "paused" while the download carried on to completion.
        """
        for worker in [self._dl_worker, *self._resume_workers]:
            if worker is None:
                continue
            for job_key, _req in getattr(worker, "_jobs", []) or []:
                if job_key == key:
                    return worker
        return None

    def _pause_and_snapshot(self, key: str) -> Optional[DownloadRequest]:
        """Claim job ``key`` for a pause and, if it was still safely
        resumable at that instant, return a resume-ready snapshot of it —
        else None (already terminal, or its own thread claimed it first).

        CLAIMS first, cancels second (DownloadOrchestrator.
        live_request_snapshot takes the job's own lock, reads its state and
        hands back a defensive copy as one atomic step). The claim — not the
        cancel — is what makes this safe: from that instant the job's pool
        thread cannot take it terminal, and the engine's publish gate will
        refuse to make its file visible, so a job captured as paused can
        never also complete for real. Cancelling first (the earlier order)
        left the opposite race open: the cancel could be observed and the
        job marked CANCELLED before the snapshot was taken, and the track
        was then dropped from the paused set entirely.

        Reading the UI card's status label instead of this would leave both
        windows open — the card is only updated once a Qt-queued signal from
        the worker thread is dispatched, one tick behind the orchestrator's
        own bookkeeping.
        """
        worker = self._worker_for_key(key)
        if worker is None:
            return None
        orch = worker._orch  # noqa: SLF001
        _state, live_req = orch.live_request_snapshot(key)
        if live_req is None:
            return None
        worker.cancel_track(key)
        # Full-fidelity snapshot (every field, incl. workspace_dir) so the
        # resumed track continues from its .part file with all its
        # metadata/artwork behaviour intact.
        return self._snapshot_for_pause(live_req)

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
        the partial state. Jobs whose card already reached a terminal state
        (done/preexisting, errored, or already cancelled) are deliberately
        NOT captured, so they are never re-run on the next Resume All. A job
        caught with nothing safe to snapshot (see _pause_and_snapshot) is
        cancelled outright rather than resumed from unsafe state.

        Covers every running worker, not just the main batch one: a track
        the user already resumed individually is running in its own
        single-job worker, and Global Pause must stop and capture that one
        too rather than leave it downloading."""
        self._set_termination_intent(BatchOutcome.PAUSED_BY_USER)
        workers = [
            w for w in [self._dl_worker, *self._resume_workers]
            if w is not None and w.isRunning()
        ]
        if not workers:
            return
        for worker in workers:
            for key, _req in list(getattr(worker, "_jobs", []) or []):
                card = self._key_to_card.get(key)
                if card is not None and card.get_status() in _TERMINAL_CARD_STATUSES:
                    continue  # already done/errored/cancelled — never resume it
                snapshot = self._pause_and_snapshot(key)
                if snapshot is None:
                    continue
                self._paused_requests[key] = snapshot
                if card is not None:
                    card.set_status("paused")
        self._persist_paused_state()
        for worker in workers:
            worker.cancel()

    def cancel_all(self) -> None:
        """Cancel everything in flight: the engine, the batch worker, and
        every per-track resume worker.

        The engine-wide event is NOT sufficient on its own, which is why
        each worker is cancelled explicitly. A job that reaches ffmpeg —
        an HLS/DASH stream, or the generic stream-intercept path — polls
        exactly ONE event while the child process runs, and that is the
        job's own per-request event (core.hls_downloader.download_hls's
        ``cancel_event`` parameter, which core.downloader passes as
        ``request.cancel_event or self._cancel_event``). With a per-request
        event present, as every batched job has, the engine-wide flag is
        never looked at, so the remux ran to completion and only then
        noticed it had been cancelled. Only the worker's own cancel()
        reaches DownloadOrchestrator.cancel(), which sets those per-job
        events and shuts the pool down.

        Symmetric with global_pause, which already covers every running
        worker: a track the user resumed individually lives in its own
        single-job worker and was previously left running by Cancel All."""
        self._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)
        self._engine.cancel_all()
        for worker in [self._dl_worker, *self._resume_workers]:
            if worker is not None and worker.isRunning():
                worker.cancel()

    def pause_track(self, card) -> bool:
        """Save the in-flight request for this card and cancel only that
        track. Returns whether the track was actually paused.

        The card is relabelled "paused" ONLY when a snapshot was genuinely
        taken — which is also exactly when the cancel was delivered (see
        _pause_and_snapshot). Setting it unconditionally was a lie in every
        case where the snapshot failed: no worker owns the key, or the job
        had already finished, errored or been claimed by its own thread. A
        card reading "paused" over a download that is still running, or
        over one that already completed, is worse than no feedback at all —
        the user is offered a Resume that has nothing to resume.

        AppWindow looks up the card from _index_to_card and passes it here.
        """
        key = str(id(card))
        snapshot = self._pause_and_snapshot(key)
        if snapshot is None:
            logger.info(
                "[DownloadController] Nothing to pause for card %s — leaving "
                "its status as %r", key, card.get_status(),
            )
            return False
        self._paused_requests[key] = snapshot
        card.set_status("paused")
        self._persist_paused_state()
        return True

    # ── Paused-state persistence (single authoritative store) ─────────────────

    @staticmethod
    def _card_to_dict(card) -> dict:
        """Display metadata needed to rebuild a paused card after restart.

        NOT the same key spelling as AppWindow._save_queue_state's general-
        queue dict (which uses "url"/"duration_str" and is read back through
        an intermediate object wrapper). This dict is instead passed
        STRAIGHT to AppWindow._add_track_to_queue as `data`, which hits its
        isinstance(data, dict) branch and reads "track_url"/"duration"
        directly -- using the general-queue spelling here silently restored
        every paused card with an empty URL and duration.

        Also carries the Spotify two-stage identity fields
        (spotify_id/spotify_key_kind/duration_sec/match_status) a pending
        (unresolved) card needs -- restore_paused_jobs uses these to rebuild
        an equivalent url_resolver, since the live resolver closure itself
        cannot be persisted (see _build_spotify_resolver)."""
        return {
            "title":            getattr(card, "title", ""),
            "artist":           getattr(card, "artist", ""),
            "track_url":        getattr(card, "track_url", ""),
            "duration":         getattr(card, "duration", ""),
            "thumbnail_url":    getattr(card, "thumbnail_url", ""),
            "platform":         getattr(card, "platform", "youtube"),
            "album":            getattr(card, "album", ""),
            "parent_artist":    getattr(card, "parent_artist", ""),
            "release_type":     getattr(card, "release_type", ""),
            "category":         getattr(card, "category", ""),
            "album_index":      getattr(card, "album_index", 0),
            "total_tracks":     getattr(card, "total_tracks", 0),
            "duration_sec":     getattr(card, "duration_sec", None),
            "spotify_id":       getattr(card, "spotify_id", ""),
            "spotify_key_kind": getattr(card, "spotify_key_kind", "spotify_id"),
            "match_status":     getattr(card, "match_status", "matched"),
            "source_kind":      getattr(card, "source_kind", ""),
            "source_url":       getattr(card, "source_url", ""),
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
            keep = self._paused_store.workspace_dirs_or_none()
            if keep is None:
                # The persisted state could not be read in full -- bad JSON,
                # an unexpected shape, or even one unreadable record. Its
                # keep-set is unknowable, NOT empty, and a PARTIAL keep-set
                # is just as dangerous as an empty one: the entries missing
                # from it are precisely the workspaces that would be swept.
                # Skip the sweep rather than delete resumable work.
                logger.warning(
                    "[DownloadController] Paused-state file could not be read "
                    "in full — skipping the stale-workspace sweep this run "
                    "rather than risk deleting resumable work"
                )
            else:
                # Every output dir a currently-loaded paused job actually
                # points at, PLUS the current config's output dir.
                # sweep_stale_workspaces additionally searches every RECORDED
                # output root (utils.paths.known_output_roots), which is what
                # finds an abandoned workspace under a directory the user has
                # since changed away from — nothing here still references
                # such a path once its paused job is gone.
                base_dirs: list[str] = []
                current = getattr(self._cfg, "output_dir", None)
                if current:
                    base_dirs.append(current)
                for pj in store_jobs:
                    job_output_dir = pj.request.get("output_dir")
                    if job_output_dir and job_output_dir not in base_dirs:
                        base_dirs.append(job_output_dir)
                removed = sweep_stale_workspaces(base_dirs, keep)
                if removed:
                    logger.info("[DownloadController] Swept %d stale workspace(s)", len(removed))
                # Cross-volume publish temps stranded by a crash between the
                # staging copy and the final rename. Same discovery rule, and
                # nothing else ever removes them.
                from utils.paths import sweep_stale_publish_temp_files
                temps = sweep_stale_publish_temp_files(base_dirs)
                if temps:
                    logger.info(
                        "[DownloadController] Removed %d stale publish temp file(s)",
                        len(temps),
                    )
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
            req = request_from_dict(pj.request)
            # A job paused before its Spotify two-stage match ever ran has
            # no resolved URL AND (per request_to_dict) no persisted
            # resolver -- a live closure can't be serialised. Rebuild an
            # EQUIVALENT one from the identity fields _card_to_dict saved,
            # so it re-matches to YouTube instead of trying to download a
            # placeholder URL.
            if pj.request.get("had_pending_resolver") and pj.card.get("spotify_id"):
                td = {
                    "spotify_id":       pj.card.get("spotify_id", ""),
                    "spotify_key_kind": pj.card.get("spotify_key_kind", "spotify_id"),
                    "title":            pj.card.get("title", ""),
                    "artist":           pj.card.get("artist", ""),
                    "duration_sec":     pj.card.get("duration_sec"),
                }
                req.url_resolver = self._build_spotify_resolver(td, self._cfg.cookies_file or None)
                req.spotify_match_identity = dict(td)
            self._paused_requests[key] = req
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

        card.set_status("queued")
        card.set_progress(0.0)
        self._resume_click_ts[key] = time.monotonic()
        self._resume_engine_started.discard(key)

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
        if hasattr(resume_worker, "track_first_byte"):
            resume_worker.track_first_byte.connect(self._on_track_first_byte)
        resume_worker.track_speed.connect(self._on_track_speed)
        resume_worker.track_status.connect(self._on_track_status)
        resume_worker.track_phase.connect(self._on_track_phase)
        resume_worker.track_finished.connect(self._on_track_finished)
        resume_worker.job_error.connect(self._on_track_error)
        resume_worker.all_finished.connect(self._on_batch_done)
        resume_worker.track_thumbnail.connect(self._on_track_thumbnail)
        resume_worker.all_finished.connect(
            # all_finished carries the BatchOutcome as its one argument, so
            # the lambda must accept (and ignore) it as its FIRST positional
            # parameter -- a lambda with only a defaulted `w=resume_worker`
            # parameter would have that default silently overridden by the
            # emitted outcome instead, so `w` would never actually be the
            # worker and this would never remove anything: _resume_workers
            # would grow forever, one stale entry per resumed track, and
            # _on_batch_done's "is this the last active worker" check (see
            # its docstring) would see those stale entries as still active
            # and stop reporting completion for every resume after the
            # first one.
            lambda _outcome=None, w=resume_worker, k=key: self._finish_resume_worker(k, w)
        )
        # Persist only after the worker has been submitted.  A crash before
        # this point must leave the paused record and its workspace restorable.
        resume_worker.start()
        self._release_paused_record(resume_worker)

    def _finish_resume_worker(self, key: str, worker) -> None:
        """Release one resume worker and any unfinished timing state."""
        if worker in self._resume_workers:
            self._resume_workers.remove(worker)
        self._resume_click_ts.pop(key, None)
        self._resume_engine_started.discard(key)

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
        self._release_paused_record(self._dl_worker)

        self.batch_started.emit()

    def _release_paused_record(self, worker) -> None:
        """Drop the on-disk paused record for the jobs ``worker`` has just
        taken over — but only once it has genuinely entered its run
        lifecycle.

        QThread.start() only guarantees the thread was SCHEDULED; run() may
        not have executed a line yet. Rewriting the persisted state on the
        strength of start() alone leaves a real window in which a crash or a
        kill loses the jobs outright: the record that said "these are
        paused, keep their workspaces" is already gone, and the worker that
        was supposed to assume ownership never did — so the next startup
        sweeps those workspaces as abandoned. Waiting for the worker to
        actually enter run() means the record only disappears once something
        is running that can re-create it.

        A worker that does not report started within the timeout keeps its
        record: an extra, stale paused entry is harmless (it is re-persisted
        from live state on the next pause, and its workspace is protected
        meanwhile), whereas dropping it prematurely is not."""
        try:
            started = worker.wait_until_running(timeout_ms=3000)
        except Exception:  # noqa: BLE001 — a fake/stub worker in tests
            started = True
        if not started:
            logger.warning(
                "[DownloadController] Resume worker did not report started — "
                "keeping the persisted paused record so its workspace stays "
                "protected"
            )
            return
        self._persist_paused_state()

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
        """Forward a whole-batch snapshot to the footer, if it belongs here.

        The sender check alone is not enough. It has to stay permissive —
        ``sender() is None`` for direct programmatic calls, and a resume worker
        is accepted while ``_dl_worker is None`` — so nothing structural stops a
        single-track resume, which runs its own orchestrator with its own 1-job
        aggregator, from repainting the whole-batch footer as "0 of 1". That
        only fails to happen today because resume_track() does not connect this
        signal, which is a convention rather than a guarantee.

        So the batch is identified explicitly. _build_batch_worker mints an id
        and hands the same value to the worker, which passes it down to the
        aggregator that stamps every snapshot with it. The comparison is
        therefore against an identity this controller chose in advance, not one
        inferred from traffic: a stale or foreign snapshot cannot define what
        "the current batch" means simply by arriving first.

        No live batch means no id, and nothing may repaint the footer.
        """
        if not self._is_current_batch_worker_signal():
            return
        if self._batch_snapshot_id is None:
            return
        if (getattr(snapshot, "batch_id", "") or "") != self._batch_snapshot_id:
            return
        self.batch_snapshot.emit(snapshot)

    def _on_worker_job_count_changed(self, completed: int, total: int) -> None:
        if self._is_current_batch_worker_signal():
            self.job_count_changed.emit(completed, total)

    def _on_track_phase(self, key: str, phase: str, remaining_seconds) -> None:
        """Tell one card which stage it is in and how long that leaves.

        The card used to be handed the byte transfer and nothing else, so it
        showed a dead bar through matching and the gate wait, raced to 95%, and
        then sat at 95% through post-processing. Both stretches are now named
        and both advance.
        """
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card is None:
            return
        card.set_phase(phase, remaining_seconds)

    def _on_track_progress(self, key: str, fraction: float) -> None:
        if not self._is_active_worker_signal():
            return
        prev = self._card_progress.get(key, 0.0)
        if fraction - prev < 0.01 and fraction < 1.0:
            return
        self._card_progress[key] = fraction
        card = self._key_to_card.get(key)
        if card:
            card.set_progress(fraction)

    def _on_track_first_byte(self, key: str) -> None:
        """Record the first *real* transfer byte once per clicked batch."""
        if not self._is_active_worker_signal():
            return
        resume_click = getattr(self, "_resume_click_ts", {}).pop(key, None)
        if resume_click is not None:
            logger.info(
                "[timing][resume] first real byte (downloaded_bytes > 0): %.3fs after resume",
                time.monotonic() - resume_click,
            )
            return
        if self._first_byte_logged or self._batch_click_ts is None:
            return
        self._first_byte_logged = True
        logger.info(
            "[timing][click] first real byte (downloaded_bytes > 0): %.3fs after click",
            time.monotonic() - self._batch_click_ts,
        )

    def _on_track_speed(self, key: str, speed_bps: float, eta_seconds: float) -> None:
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card and hasattr(card, "update_speed"):
            card.update_speed(speed_bps, eta_seconds)

    def _on_track_status(self, key: str, status: str) -> None:
        if not self._is_active_worker_signal():
            return
        # "starting" means the URL was resolved and the orchestrator is about
        # to hand the job to yt-dlp. It intentionally precedes the first byte.
        resume_click = getattr(self, "_resume_click_ts", {}).get(key)
        if (
            status == "starting"
            and resume_click is not None
            and key not in getattr(self, "_resume_engine_started", set())
        ):
            self._resume_engine_started.add(key)
            logger.info(
                "[timing][resume] first engine start (starting): %.3fs after resume",
                time.monotonic() - resume_click,
            )
        elif (
            status == "starting"
            and not self._engine_start_logged
            and self._batch_click_ts is not None
        ):
            self._engine_start_logged = True
            logger.info(
                "[timing][click] first engine start (starting): %.3fs after click",
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
        track_req = self._active_request_for_key(key)
        message_key = getattr(err, "message_key", "")
        resolver_failed = bool(
            track_req
            and track_req.spotify_match_identity
            and not is_youtube_url(track_req.url)
        )
        if card:
            if message_key == "err_safe_match_not_found" or resolver_failed:
                card.mark_unresolved(t("spotify_unresolved_card"))
            else:
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
        (clean completion vs completion-with-failures).

        Multiple per-track resume workers can be running at once (each an
        independent single-job DownloadWorker started by resume_track), and
        every one of them connects its all_finished signal to this same
        slot. One of them finishing must not tell the UI "no longer
        downloading" — and must not consume the shared termination intent —
        while a sibling resume is still active; only the truly LAST active
        worker (main batch or resume) does that. The finishing sender is
        still present in _resume_workers at this point (its own removal is a
        separately-connected slot that has not necessarily run yet), so it
        is excluded explicitly rather than relying on that removal.

        Per-worker workspace cleanup (see _cleanup_cancelled_batch), by
        contrast, runs for THIS finishing worker regardless of whether it's
        the last one — an early-finishing resume that was itself cancelled
        or caught in a fatal stop must have its own abandoned workspace
        swept immediately, not held hostage until some sibling resume also
        finishes."""
        if not self._is_current_batch_worker_signal():
            return
        # Capture the finishing worker's jobs BEFORE nulling _dl_worker so a
        # cancellation/fatal-stop can clean up exactly that worker's
        # abandoned work. Safe to read here: all_finished fires only after
        # run_batch has drained its pool, so no download thread is still
        # writing.
        sender = self.sender()
        worker_jobs = list(getattr(sender, "_jobs", []) or [])

        if sender is self._dl_worker:
            self._dl_worker = None

        # A deliberate cancel OR a fatal stop (never a pause) removes this
        # worker's unfinished partial/intermediate work. Fully-published
        # final files already live in the user's output directory and are
        # never touched (cleanup only removes the hidden workspace
        # container). Pause preserves the workspace instead, which is
        # exactly what keeps the two operations fundamentally different.
        # Read independent of the last-active-worker check below: this is
        # this worker's own outcome, not a shared UI-facing signal.
        worker_outcome = self._termination_intent or orchestrator_outcome or BatchOutcome.COMPLETED
        if worker_outcome in (BatchOutcome.CANCELLED_BY_USER, BatchOutcome.STOPPED_BY_FATAL_ERROR):
            self._cleanup_cancelled_batch(worker_jobs)

        other_active_workers = [w for w in self._resume_workers if w is not sender]
        if self._dl_worker is not None or other_active_workers:
            return

        self.cancel_visible.emit(False)
        self.downloading_changed.emit(False)
        self.metrics_update.emit("", "")

        outcome = self._termination_intent
        if outcome is None:
            outcome = orchestrator_outcome or BatchOutcome.COMPLETED
        self._termination_intent = None

        self.batch_finished.emit(outcome)

    def _cleanup_cancelled_batch(self, jobs: list) -> None:
        """Remove the abandoned workspace of a cancelled OR fatally-stopped
        batch and drop any paused snapshots for its jobs — both abandon
        everything, unlike a pause. A fatal stop (e.g. a broken cookie jar
        that would fail every remaining job the same way) is just as much
        an abandonment as a deliberate user cancel; leaving it out would
        strand the batch's workspace on disk forever since nothing else
        ever sweeps it.

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
        """Retrieve the live DownloadRequest for a card key from whichever
        worker owns it — the batch worker or a per-track resume worker."""
        worker = self._worker_for_key(key)
        if worker is None:
            return None
        for card_key, req in getattr(worker, "_jobs", []) or []:
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
