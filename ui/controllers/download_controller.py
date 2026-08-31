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
from core.download_recovery import (
    DownloadFailureIncident,
    FailureIncidentItem,
    RecoveryDecision,
    UserActionRequest,
    shared_download_recovery_coordinator,
    systemic_recovery_policy,
)
from core.filename_numbering import decide_numbering
from core.output_layout import decide_output_layout
from core.playlist_parser import SourcePlatform, UrlKind
from core.quality_presets import (
    AudioQuality,
    VideoQuality,
    audio_quality_from_id,
    default_audio_quality_id_for_codec,
    video_quality_from_id,
)
from core.spotify_request_builder import (
    attach_spotify_matching,
    build_spotify_resolver,
    effective_match_status,
    is_downloadable,
    spotify_identity,
)
from ui.i18n import localized_folder_name, t

logger = logging.getLogger(__name__)


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
    user_action_required: UserActionRequest — one serialized pre-terminal choice
    rate_limit_waiting : float seconds — shared YouTube cooldown countdown
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
    user_action_required = Signal(object)         # UserActionRequest
    failure_incident_updated = Signal(object)     # DownloadFailureIncident
    rate_limit_waiting  = Signal(float)          # shared cooldown seconds
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
                
        self._termination_lock = threading.Lock()
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
        self._rate_limit_until: float = 0.0
        self._failure_incidents: dict[str, DownloadFailureIncident] = {}
        self._failed_requests: dict[str, DownloadRequest] = {}

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

        # Admission rule shared with the CLI (core.spotify_request_builder) so
        # both front-ends agree on which tracks are downloadable at all.
        downloadable = []
        for card in selected:
            match_status = effective_match_status(card)
            if match_status == "pending" and getattr(card, "match_status", "") != "pending":
                # A previously "unresolved" card is being retried: clear the
                # stale error so the row stops rendering as blocked.
                card.match_status = "pending"
                card.resolution_error = ""
            if not is_downloadable(card, match_status):
                if hasattr(card, "mark_metadata_invalid"):
                    card.mark_metadata_invalid(t("spotify_metadata_invalid_card"))
                continue
            downloadable.append(card)
        selected = downloadable

        if not selected:
            # "Nothing selected" is not a global-status condition \u2014 the
            # Download button is disabled when nothing is selected, so this
            # is only reachable as an edge case. Fail quietly.
            return

        self._termination_intent = None
        self._rate_limit_until = 0.0
        
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

        self._key_to_card.clear()
        self._card_progress.clear()

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
            track_index: Optional[int], filename_index: Optional[int],
            include_artist_filename: bool, is_clean: bool,
            disc_number: Optional[int] = None, track_total: Optional[int] = None,
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
                filename_index=filename_index,
                filename_include_artist=include_artist_filename,
                forced_disc=disc_number,
                forced_total=track_total,
                cookies_file=self._cfg.cookies_file or None,
                cookies_browser=self._cfg.cookies_browser or None,
                playlist_name=track_playlist_name or "",
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
            # carries match_status == "pending", and gets a lazy resolver so the
            # download pool matches it to YouTube the instant before it starts
            # downloading — pipelining the (possibly large) catalog's matching
            # with the downloads instead of blocking every download on it.
            # Shared with the CLI so both build the same request (issue #59).
            attach_spotify_matching(req, card, self._cfg.cookies_file or None)

            return req

        # Which releases in this batch actually span more than one disc.
        # A disc number is only meaningful in that context: on a single-disc
        # album every track reports disc 1, and stamping TPOS=1 on all of them
        # adds noise. It also cannot be decided per card — disc 1 of a 2-disc
        # set looks exactly like a whole single-disc album until you have seen
        # its sibling — so the whole selection is scanned once up front.
        discs_per_release: dict[tuple[str, str], set[int]] = {}
        for card in selected:
            disc = getattr(card, "disc_number", 0) or 0
            if disc > 0:
                release_key = (
                    (card.parent_artist or card.artist or "").strip(),
                    (card.album or "").strip(),
                )
                discs_per_release.setdefault(release_key, set()).add(disc)
        multi_disc_releases = {
            release for release, discs in discs_per_release.items() if len(discs) > 1
        }

        for card in selected:
            key = str(id(card))
            track_playlist_name:   Optional[str] = None
            source_kind = str(getattr(card, "source_kind", "") or "").upper()
            collection_title = (
                str(getattr(card, "collection_title", "") or "").strip()
                or (
                    (last_playlist_title or (card.album or "").strip())
                    if source_kind == UrlKind.PLAYLIST.name
                    else (
                        (card.album or "").strip() or last_playlist_title
                        if source_kind == UrlKind.ALBUM.name else ""
                    )
                )
            )
            release_key = (
                (card.parent_artist or card.artist or "").strip(),
                (card.album or "").strip(),
            )
            try:
                reported_disc_total = int(getattr(card, "disc_total", 0) or 0)
            except (TypeError, ValueError):
                reported_disc_total = 0
            known_multi_disc = (
                reported_disc_total > 1
                or release_key in multi_disc_releases
            )
            layout = decide_output_layout(
                source_kind=source_kind,
                release_type=card.release_type,
                collection_title=collection_title,
                album=card.album,
                parent_artist=card.parent_artist,
                artist=card.artist,
                category=card.category,
                total_tracks=card.total_tracks,
                platform=card.platform,
                track_title=card.title,
                playlist_subfolders=self._cfg.playlist_subfolders,
                singles_subfolder=self._cfg.singles_subfolder,
                multi_disc=known_multi_disc,
                disc_number=getattr(card, "disc_number", 0),
            )
            track_playlist_name = layout.render_folder(
                localize_category=localized_folder_name,
                disc_label=lambda number: t("disc_folder", number=number),
            )
            include_artist_filename = layout.include_artist_in_filename

            # Use the same path the writability check ran against. opts["output_dir"]
            # comes from OptionsBar and reflects either the user's typed path
            # (persisted to config on editingFinished) or the browse-button
            # selection. Falling back to cfg.output_dir would silently drop a
            # path the user typed but hasn't committed yet.
            output_dir = str(Path(base_output_dir).expanduser())

            # Direct files and compilations include the artist to avoid
            # title-only collisions. Ordered releases/playlists remain concise.
            is_clean = not include_artist_filename

            # Use only the provider's original collection position. queue_index
            # is a stable UI identity and must never leak into filenames.
            numbering = decide_numbering(
                source_kind=source_kind,
                release_type=card.release_type,
                collection_index=card.album_index,
                total_tracks=card.total_tracks,
                number_playlists=self._cfg.playlist_index_prefix,
            )
            track_index = numbering.metadata_track_index
            filename_index = numbering.filename_index
            is_release_position = numbering.is_release_position

            # Disc and total only describe a real album/EP/compilation position.
            disc_number = None
            track_total = None
            if is_release_position:
                release_key = (
                    (card.parent_artist or card.artist or "").strip(),
                    (card.album or "").strip(),
                )
                if known_multi_disc:
                    disc_number = getattr(card, "disc_number", 0) or None
                if card.total_tracks > 0:
                    track_total = card.total_tracks

            # Duplicate detection must use the exact filename body chosen by
            # the shared layout policy.
            if self._cfg.duplicate_action != "overwrite":
                from core.duplicate_checker import find_duplicate
                dup = find_duplicate(
                    output_dir=output_dir,
                    title=card.title,
                    artist=card.artist,
                    index=filename_index,
                    include_index=filename_index is not None,
                    include_artist=include_artist_filename,
                    duration_s=None,
                    playlist_name=track_playlist_name or "",
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
                            "track_index": track_index,
                            "filename_index": filename_index,
                            "include_artist_filename": include_artist_filename,
                            "is_clean": is_clean,
                            "disc_number": disc_number,
                            "track_total": track_total,
                        })
                        continue

            req = _build_request(
                card, output_dir, track_playlist_name,
                track_index, filename_index, include_artist_filename, is_clean,
                disc_number, track_total,
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
                        p["track_index"], p["filename_index"],
                        p["include_artist_filename"], p["is_clean"],
                        p["disc_number"], p["track_total"],
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
        if hasattr(worker, "user_action_required"):
            worker.user_action_required.connect(self._on_user_action_required)
        if hasattr(worker, "rate_limit_wait"):
            worker.rate_limit_wait.connect(self._on_rate_limit_wait)
        worker.all_finished.connect(self._on_batch_done)
        worker.track_thumbnail.connect(self._on_track_thumbnail)
        return worker

    def is_downloading(self) -> bool:
        """True while the main batch or any per-track resume is running."""
        return any(
            worker is not None and worker.isRunning()
            for worker in [self._dl_worker, *self._resume_workers]
        )

    def request_shutdown(self) -> bool:
        """Cooperatively cancel every download worker; report when all stopped."""
        self.cancel_all()
        return not any(
            worker is not None and worker.isRunning()
            for worker in [self._dl_worker, *self._resume_workers]
        )

    def _set_termination_intent(self, intent: BatchOutcome) -> None:
        """Record why the batch is ending.

        Fatal stops have highest priority. A deliberate user cancel can
        override a just-requested pause (rapid pause -> cancel), while a pause
        cannot override an existing cancel/fatal intent.
        """
        with self._termination_lock:
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
        from the minimal identity dict it needs.

        Thin delegate to core.spotify_request_builder, which owns the
        matching contract for BOTH front-ends (issue #59). Kept as a method
        so restore_paused_jobs -- which rebuilds an EQUIVALENT resolver for a
        paused job that was never resolved before the app restarted -- reads
        the same way as it always did. The resolver itself is a live closure
        and cannot be persisted, but everything needed to rebuild an
        equivalent one can be: see core.download_request_codec's
        had_pending_resolver flag and _card_to_dict's identity fields."""
        return build_spotify_resolver(td, cookies)

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
        single-job worker and was previously left running by Cancel All.

        Paused work is abandoned too, BEFORE the workers are cancelled --
        see _discard_paused_requests. Cancel All means "throw this batch
        away"; leaving the paused snapshots behind kept offering Resume for
        tracks the user had just cancelled, and after a Global Pause it left
        them behind completely untouched, since by then no worker is running
        for this to reach at all."""
        self._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)
        self._discard_paused_requests()
        self._engine.cancel_all()
        for worker in [self._dl_worker, *self._resume_workers]:
            if worker is not None and worker.isRunning():
                worker.cancel()

    def _discard_paused_requests(self) -> None:
        """Cancel All's half of the pause/cancel split: throw every paused
        snapshot away, mark its card cancelled, and release what is holding
        its partial download on disk.

        Runs BEFORE the workers are cancelled, while the orchestrator that
        owns each job is still alive. cancel_paused_job takes the pause's
        outcome claim back and moves the job to CANCELLED in the batch
        aggregator; done afterwards, the batch would already have finalised
        and the job would stay PAUSED in a snapshot nobody can correct.

        Workspaces are removed here ONLY for jobs no running worker owns any
        more -- the Global-Pause-then-Cancel-All case, where nothing else
        will ever clean up after them. For a job whose worker is still
        winding down, removal is left to _cleanup_cancelled_batch once that
        worker has actually finished: deleting a .part file out from under a
        job that is still shutting down is the one thing this must not do.
        """
        if not self._paused_requests:
            return
        from utils.paths import remove_workspace_tree

        abandoned_containers: set[Path] = set()
        for key, req in list(self._paused_requests.items()):
            worker = self._worker_for_key(key)
            orch = getattr(worker, "_orch", None) if worker is not None else None
            if orch is not None:
                orch.cancel_paused_job(key)
            card = self._key_to_card.get(key)
            if card is not None:
                # Set directly rather than relying on the orchestrator's
                # "cancelled" callback: that arrives over a queued signal
                # from a worker that is about to be torn down, and for a
                # batch that already finished there is no worker to send it.
                card.set_status("cancelled")
            still_owned = worker is not None and worker.isRunning()
            if not still_owned and req.workspace_dir:
                # req.workspace_dir is this job's per-job subdir
                # (<container>/<job_key>); its parent is the batch container.
                abandoned_containers.add(Path(req.workspace_dir).parent)
        self._paused_requests.clear()
        self._persist_paused_state()  # nothing paused left -> clears the store
        for container in abandoned_containers:
            remove_workspace_tree(container)

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
            "disc_number":      getattr(card, "disc_number", 0),
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
                # spotify_identity() reads the persisted card dict directly, so
                # a restored resolver is built from the SAME six fields as a
                # live one -- including "album", which the hand-written copy
                # this replaced dropped even though _card_to_dict saves it.
                td = spotify_identity(pj.card)
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
        if hasattr(resume_worker, "user_action_required"):
            resume_worker.user_action_required.connect(self._on_user_action_required)
        if hasattr(resume_worker, "rate_limit_wait"):
            resume_worker.rate_limit_wait.connect(self._on_rate_limit_wait)
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

    def _on_user_action_required(self, request: UserActionRequest) -> None:
        """Forward one pre-terminal request, never a stale worker's request."""
        if not self._is_active_worker_signal():
            # The producer is waiting. A stale queued signal must still be
            # resolved so its old thread cannot remain stranded during teardown.
            request.resolve(RecoveryDecision.CANCEL)
            return
        self.user_action_required.emit(request)

    def resolve_user_action(
        self,
        request: UserActionRequest,
        decision: RecoveryDecision | str,
    ) -> bool:
        """Apply refreshed authentication before releasing a retry."""
        resolved = RecoveryDecision(decision)
        if resolved == RecoveryDecision.RETRY:
            cookies_file = self._cfg.cookies_file or ""
            cookies_browser = self._cfg.cookies_browser or ""
            for worker in [self._dl_worker, *self._resume_workers]:
                if worker is None:
                    continue
                for _key, req in getattr(worker, "_jobs", []) or []:
                    req.cookies_file = cookies_file
                    req.cookies_browser = cookies_browser
        return request.resolve(resolved)

    def _on_rate_limit_wait(self, key: str, remaining_seconds: float) -> None:
        if not self._is_active_worker_signal():
            return
        remaining = max(0.0, float(remaining_seconds))
        self._rate_limit_until = (
            time.monotonic() + remaining if remaining > 0 else 0.0
        )
        card = self._key_to_card.get(key)
        if card and remaining > 0 and hasattr(card, "set_phase"):
            card.set_phase("rate_limited", remaining)
        elif card and remaining <= 0 and hasattr(card, "set_phase"):
            card.set_phase("waiting", None)
        self.rate_limit_waiting.emit(remaining)

    def rate_limit_remaining(self) -> float:
        coordinator = shared_download_recovery_coordinator()
        remaining = max(
            0.0,
            self._rate_limit_until - time.monotonic(),
            coordinator.rate_remaining(),
        )
        # The advertised countdown reaching zero admits only the same-track
        # canary. Keep unrelated fetch/search actions closed until it settles.
        if coordinator.rate_incident_active():
            return max(1.0, remaining)
        return remaining

    def _on_track_error(self, key: str, err: object) -> None:
        if not self._is_active_worker_signal():
            return
        card = self._key_to_card.get(key)
        if card:
            # Resolver misses are repaired before engine submission. An error
            # reaching this callback is an ordinary transfer/service failure,
            # not a permanently invalid Spotify identity.
            card.set_status("waiting")

        req = self._request_for_worker_key(key)
        if req is not None:
            self._failed_requests[key] = self._fresh_retry_request(req)

        policy = systemic_recovery_policy(err)
        scope = policy.scope if policy else self._local_incident_scope(err)
        coordinator = shared_download_recovery_coordinator()
        streak, stopped_all = (
            coordinator.systemic_state(policy.scope) if policy else (0, False)
        )
        incident = self._failure_incidents.get(scope)
        if incident is None:
            incident = DownloadFailureIncident(
                scope=scope,
                error=err,
                systemic_policy=policy,
            )
            self._failure_incidents[scope] = incident
        title = str(
            getattr(card, "title", "")
            or getattr(req, "forced_title", "")
            or key
        )
        artist = str(
            getattr(card, "artist", "")
            or getattr(req, "forced_artist", "")
            or ""
        )
        album = str(
            getattr(card, "album", "")
            or getattr(req, "forced_album", "")
            or ""
        )
        duration_sec = getattr(card, "duration_sec", None)
        if duration_sec is None:
            duration_sec = getattr(req, "forced_duration", None)
        url = str(getattr(req, "url", "") or "")
        incident.add(
            FailureIncidentItem(
                key=key,
                title=title,
                url=url,
                raw=str(getattr(err, "raw", "") or ""),
                artist=artist,
                album=album,
                duration_sec=duration_sec,
            ),
            stopped_all=stopped_all,
            streak=streak,
        )
        self.failure_incident_updated.emit(incident)

        logger.info(
            "[DownloadController] Track added to incident %s: %s "
            "category=%s count=%d stopped_all=%s",
            incident.incident_id,
            key,
            getattr(err, "message_key", "unknown"),
            incident.count,
            incident.stopped_all,
        )

    @staticmethod
    def _local_incident_scope(err: object) -> str:
        key = str(getattr(err, "message_key", "") or type(err).__name__)
        if key == "err_generic":
            raw = " ".join(
                str(getattr(err, "raw", "") or "").casefold().split()
            )
            return f"error:{key}:{raw[:160]}"
        return f"error:{key}"

    def _request_for_worker_key(self, key: str) -> Optional[DownloadRequest]:
        sender = self.sender()
        workers = [sender, self._dl_worker, *self._resume_workers]
        seen: set[int] = set()
        for worker in workers:
            if worker is None or id(worker) in seen:
                continue
            seen.add(id(worker))
            for job_key, req in list(getattr(worker, "_jobs", []) or []):
                if job_key == key:
                    return req
        return None

    def _fresh_retry_request(self, req: DownloadRequest) -> DownloadRequest:
        """Copy a failed request at a clean boundary for an explicit retry."""
        fresh = req.snapshot_copy()
        fresh.workspace_dir = None
        fresh.resume_phase = None
        fresh.resume_final_path = None
        fresh.cancel_event = None
        fresh.publish_gate = None
        fresh.publish_release = None
        fresh.on_progress = None
        fresh.on_finished = None
        fresh.on_error = None
        fresh._final_output_path = ""  # noqa: SLF001
        fresh._thumb_sent = False  # noqa: SLF001
        fresh.cookies_file = self._cfg.cookies_file or None
        fresh.cookies_browser = self._cfg.cookies_browser or None
        if fresh.spotify_match_identity:
            fresh.url_resolver = build_spotify_resolver(
                dict(fresh.spotify_match_identity),
                fresh.cookies_file,
            )
        return fresh

    def resolve_failure_incident(
        self,
        incident_id: str,
        decision: RecoveryDecision | str,
    ) -> bool:
        """Retry or skip every track represented by one aggregate incident."""
        incident = next(
            (
                current
                for current in self._failure_incidents.values()
                if current.incident_id == incident_id
            ),
            None,
        )
        if incident is None:
            return False

        resolved = RecoveryDecision(decision)
        self._failure_incidents.pop(incident.scope, None)
        coordinator = shared_download_recovery_coordinator()
        if incident.systemic_policy is not None:
            coordinator.reset_systemic(incident.systemic_policy.scope)

        keys = [item.key for item in incident.items]
        if resolved == RecoveryDecision.RETRY:
            jobs: list[tuple[str, DownloadRequest]] = []
            for key in keys:
                req = self._failed_requests.pop(key, None)
                card = self._key_to_card.get(key)
                if req is None:
                    if card:
                        card.set_status("error")
                    continue
                jobs.append((key, self._fresh_retry_request(req)))
                if card:
                    card.set_status("queued")
                    card.set_progress(0.0)
            if jobs:
                self._start_failure_retry_jobs(jobs)
        else:
            for key in keys:
                self._failed_requests.pop(key, None)
                card = self._key_to_card.get(key)
                if card:
                    card.set_status("error")
        return True

    def replace_failure_source(
        self,
        incident_id: str,
        key: str,
        url: str,
    ) -> bool:
        """Select and immediately retry one explicit YouTube source."""
        job = self.prepare_failure_source(incident_id, key, url)
        if job is None:
            return False
        self.start_failure_source_jobs([job])
        return True

    def prepare_failure_source(
        self,
        incident_id: str,
        key: str,
        url: str,
    ) -> Optional[tuple[str, DownloadRequest]]:
        """Prepare one explicit source without starting an extra worker yet.

        The selected media URL replaces only the transport source.  The
        original Spotify title, artist, album, numbering and output-routing
        metadata remain on the request and card.  Clearing the lazy resolver
        is essential: the user's explicit choice must not immediately be
        replaced by the automatic matcher on the retry. The UI collects these
        jobs and starts one bounded worker after the selection pass, avoiding
        one independent worker per click.
        """
        from core.playlist_parser import classify_url
        from utils.url_cleaner import clean_youtube_url

        clean_url = clean_youtube_url(str(url or "").strip())
        platform, kind = classify_url(clean_url)
        if platform not in {
            SourcePlatform.YOUTUBE,
            SourcePlatform.YOUTUBE_MUSIC,
        } or kind != UrlKind.SINGLE_VIDEO:
            return None

        incident = next(
            (
                current
                for current in self._failure_incidents.values()
                if current.incident_id == incident_id
            ),
            None,
        )
        if incident is None or not any(item.key == key for item in incident.items):
            return None

        req = self._failed_requests.pop(key, None)
        if req is None:
            return None

        fresh = self._fresh_retry_request(req)
        fresh.url = clean_url
        fresh.url_resolver = None
        fresh.spotify_match_identity = None

        card = self._key_to_card.get(key)
        if card is not None:
            card.track_url = clean_url
            card.match_status = "matched"
            card.resolution_error = ""
            card.set_status("queued")
            card.set_progress(0.0)

        incident.items = [item for item in incident.items if item.key != key]
        incident.revision += 1
        if not incident.items:
            self._failure_incidents.pop(incident.scope, None)

        return key, fresh

    def start_failure_source_jobs(
        self,
        jobs: list[tuple[str, DownloadRequest]],
    ) -> None:
        """Start one bounded retry worker for manually selected sources."""
        if jobs:
            self._start_failure_retry_jobs(jobs)

    def discard_failure_source_jobs(
        self,
        jobs: list[tuple[str, DownloadRequest]],
    ) -> None:
        """Settle prepared-but-not-started rows during an explicit cancel."""
        for key, _req in jobs:
            card = self._key_to_card.get(key)
            if card is not None:
                card.set_status("cancelled")

    def _start_failure_retry_jobs(
        self,
        jobs: list[tuple[str, DownloadRequest]],
    ) -> None:
        """Retry an incident without interrupting the still-live main batch."""
        from ui.workers.download_worker import DownloadWorker

        self._engine._cancel_event.clear()  # noqa: SLF001
        if self._dl_worker is None:
            # The original batch already ended (the common overnight case).
            # Make the collected retry a first-class batch so the footer and
            # cancel controls represent all 50 retries, not a hidden side job.
            self.cancel_visible.emit(True)
            self.downloading_changed.emit(True)
            self._dl_worker = self._build_batch_worker(jobs, [])
            self._dl_worker.start()
            self.batch_started.emit()
            return

        worker = DownloadWorker(
            jobs=jobs,
            engine=self._engine,
            config=self._cfg,
            db=self._db,
            max_workers=min(max(1, self._cfg.max_parallel_downloads), len(jobs)),
            parent=self,
        )
        self._resume_workers.append(worker)
        worker.track_progress.connect(self._on_track_progress)
        if hasattr(worker, "track_first_byte"):
            worker.track_first_byte.connect(self._on_track_first_byte)
        worker.track_speed.connect(self._on_track_speed)
        worker.track_status.connect(self._on_track_status)
        worker.track_phase.connect(self._on_track_phase)
        worker.track_finished.connect(self._on_track_finished)
        worker.job_error.connect(self._on_track_error)
        if hasattr(worker, "rate_limit_wait"):
            worker.rate_limit_wait.connect(self._on_rate_limit_wait)
        worker.all_finished.connect(self._on_batch_done)
        worker.track_thumbnail.connect(self._on_track_thumbnail)
        worker.all_finished.connect(
            lambda _outcome=None, w=worker: self._finish_incident_retry_worker(w)
        )
        self.downloading_changed.emit(True)
        worker.start()

    def _finish_incident_retry_worker(self, worker) -> None:
        if worker in self._resume_workers:
            self._resume_workers.remove(worker)

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
        reported_outcome = (
            orchestrator_outcome
            if isinstance(orchestrator_outcome, BatchOutcome)
            else BatchOutcome.STOPPED_BY_FATAL_ERROR
        )
        worker_outcome = self._termination_intent or reported_outcome
        if worker_outcome in (BatchOutcome.CANCELLED_BY_USER, BatchOutcome.STOPPED_BY_FATAL_ERROR):
            self._cleanup_cancelled_batch(worker_jobs)

        other_active_workers = [w for w in self._resume_workers if w is not sender]
        if self._dl_worker is not None or other_active_workers:
            return

        self.cancel_visible.emit(False)
        self.downloading_changed.emit(False)
        self.metrics_update.emit("", "")
        if not shared_download_recovery_coordinator().rate_incident_active():
            self._rate_limit_until = 0.0
            self.rate_limit_waiting.emit(0.0)

        outcome = self._termination_intent
        if outcome is None:
            outcome = reported_outcome
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
