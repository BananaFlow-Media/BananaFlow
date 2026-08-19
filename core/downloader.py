"""
downloader.py  –  Core download engine for BananaFlow  (v3)
===================================================================
Changelog v3
------------
* SponsorBlock integration: when request.sponsorblock is True, yt-dlp
  removes non-music segments (music_offtopic, sponsor, intro, outro).
* Playlist subfolder + index prefix: request.playlist_name creates
  output_dir/<name>/ and forced_index is always zero-padded.
* Pause & Resume: cancel + continuedl flag.  DownloadRequest.resumable
  controls whether yt-dlp picks up the .part file on retry.
* Post-processing pipeline after FINISHED: lyrics embed, ReplayGain,
  MusicBrainz enrichment, square thumbnail crop.  Each step is guarded
  by the corresponding request flag so it is zero-cost when disabled.
* Backward compatible: all existing DownloadRequest callers work unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp
try:
    import yt_dlp_ejs  # noqa: F401
except ImportError:
    pass

from utils.cookie_validator import check_cookies_valid
from utils.paths import _set_hidden_attribute, get_app_cookies_path, get_bundled_ffmpeg_dir
from utils.yt_dlp_opts import build_base_ydl_opts as _build_base_opts, temp_cookies_copy
from core.media_formats import DEFAULT_AUDIO_FORMAT, DEFAULT_VIDEO_FORMAT
from core.playlist_parser import SourcePlatform
from core.quality_presets import (
    AudioQuality,
    VideoQuality,
    audio_preset_for_quality,
    video_preset_for_quality,
)
from core.warning_classifier import (
    BROWSER_COOKIE_ACCESS_BLOCKED,
    COOKIES_EXPIRED_OR_INVALID,
    classify_warning,
)
from core.youtube_reliability import CONSERVATIVE_FRAGMENT_CONCURRENCY, is_youtube_url
from ui.i18n import t


logger = logging.getLogger(__name__)


class SilentLogger:
    """Captures yt-dlp output and avoids polluting the console.

    Only genuinely redundant chatter is filtered (see the lists below).
    Warnings that explain *why* a download is about to fail — PO Token,
    invalid cookies, missing JS runtime, 403/bot/rate-limit — are never
    suppressed. They are tagged with a stable category from
    ``core.warning_classifier`` so the log line is actionable instead of
    just repeating yt-dlp's prose.
    """
    def __init__(self) -> None:
        self._failure_evidence: list[str] = []

    def _remember_failure_evidence(self, msg: str) -> None:
        category = classify_warning(msg)
        if category and msg not in self._failure_evidence:
            self._failure_evidence.append(msg)

    @property
    def failure_evidence(self) -> str:
        """Earlier extractor evidence retained for final error precedence."""
        return " | ".join(self._failure_evidence[-3:])

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        logger.debug(f"[yt-dlp] {msg}")

    def info(self, msg: str) -> None:
        pass

    @staticmethod
    def _is_po_token_diagnostic(msg: str) -> bool:
        lower = (msg or "").lower()
        return any(token in lower for token in (
            "potokenprovidererror", "failed while generating pot",
            "failed to generate an integrity token", "unable to fetch gvs po token",
            "po_token_missing", "po token",
        ))

    @staticmethod
    def _is_coalesced_cookie_diagnostic(msg: str) -> bool:
        return classify_warning(msg) in {
            COOKIES_EXPIRED_OR_INVALID,
            BROWSER_COOKIE_ACCESS_BLOCKED,
        }

    def warning(self, msg: str) -> None:
        self._remember_failure_evidence(msg)
        from utils.yt_dlp_opts import note_po_token_provider_diagnostic
        if self._is_po_token_diagnostic(msg):
            if note_po_token_provider_diagnostic(msg):
                logger.warning(
                    "[yt-dlp][po_token] provider-related warning observed; "
                    "further related messages will be summarized: %s", msg,
                )
            else:
                logger.debug("[yt-dlp][po_token] coalesced provider diagnostic: %s", msg)
            return
        if self._is_coalesced_cookie_diagnostic(msg):
            from utils.yt_dlp_opts import note_cookie_diagnostic
            note_cookie_diagnostic(msg)
            logger.debug("[yt-dlp][cookies] coalesced invalid-cookie diagnostic: %s", msg)
            return
        # Filter technical noise that clutters the console
        if any(x in msg for x in [
            "Signature solving failed",
            "n challenge solving failed",
            "Incomplete data received",
            "re-fetching using API",
            "Some formats may be missing",
            # Routine per-client fallback chatter — yt-dlp always tries
            # several player clients internally; these fire on nearly every
            # track regardless of whether the download ultimately succeeds.
            # The actual outcome is already surfaced via error()/the final
            # per-track status, so repeating every intermediate attempt here
            # is pure noise.
            "unable to extract yt initial data",
            "initial player response",
        ]):
            return

        category = classify_warning(msg)
        if category:
            logger.warning(f"[yt-dlp][{category}] {msg}")
        else:
            logger.warning(f"[yt-dlp] {msg}")

    def error(self, msg: str) -> None:
        self._remember_failure_evidence(msg)
        from utils.yt_dlp_opts import note_po_token_provider_diagnostic
        if self._is_po_token_diagnostic(msg):
            if note_po_token_provider_diagnostic(msg):
                logger.error(
                    "[yt-dlp][po_token] provider-related error observed; "
                    "further related messages will be summarized: %s", msg,
                )
            else:
                logger.debug("[yt-dlp][po_token] coalesced provider diagnostic: %s", msg)
            return
        if self._is_coalesced_cookie_diagnostic(msg):
            from utils.yt_dlp_opts import note_cookie_diagnostic
            note_cookie_diagnostic(msg)
            logger.debug("[yt-dlp][cookies] coalesced invalid-cookie diagnostic: %s", msg)
            return
        # Filter some redundancy in error messages
        if "Signature solving failed" in msg and "EJS" in msg:
            return
        category = classify_warning(msg)
        if category:
            logger.error(f"[yt-dlp][{category}] {msg}")
        else:
            logger.error(f"[yt-dlp] {msg}")

# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class PublishError(Exception):
    """A fully-downloaded file could not be atomically published from the
    batch workspace into the user's output directory. Raised by
    DownloadEngine._publish_to_final_location so the download is reported as
    a per-track error instead of a false success (the file is still in the
    workspace, which is about to be cleaned up)."""


class PublishCancelled(Exception):
    """A cancel/pause arrived while the finished file was being published,
    and the publish was abandoned before the destination was ever touched.

    Deliberately NOT a PublishError: nothing failed, the user stopped it.
    The caller reports CANCELLED (not an error, and definitely not a
    success), and the file stays in the workspace where a resume can pick
    it up — the publish is the last thing that happens, and it is not
    instantaneous across volumes, so a cancel landing inside it must not be
    silently overtaken by a completed, visible download."""


# ──────────────────────────────────────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────────────────────────────────────

class MediaType(Enum):
    AUDIO = "audio"
    VIDEO = "video"


class DownloadStatus(Enum):
    QUEUED      = auto()
    EXTRACTING  = auto()
    DOWNLOADING = auto()
    PROCESSING  = auto()
    FINISHED    = auto()
    ERROR       = auto()
    CANCELLED   = auto()
    PAUSED      = auto()    # NEW – user paused this item


# ──────────────────────────────────────────────────────────────────────────────
# Data-classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DownloadProgress:
    status:            DownloadStatus
    url:               str               = ""
    title:             str               = ""
    playlist_index:    Optional[int]     = None
    playlist_count:    Optional[int]     = None
    downloaded_bytes:  int               = 0
    total_bytes:       Optional[int]     = None
    total_bytes_estimate: Optional[int]  = None
    speed_bps:         Optional[float]   = None
    eta_seconds:       Optional[float]   = None
    fraction:          float             = 0.0
    error_message:     str               = ""
    warning_message:   str               = ""   # non-fatal post-processing failures
    output_path:       str               = ""
    thumbnail_url:     Optional[str]     = None


@dataclass
class DownloadRequest:
    """One complete download job.  All new fields have safe defaults."""

    url:         str
    output_dir:  str
    media_type:  MediaType       = MediaType.AUDIO
    video_quality: VideoQuality  = VideoQuality.P1080
    audio_quality: AudioQuality  = AudioQuality.MP3_320
    audio_format:  str           = DEFAULT_AUDIO_FORMAT
    video_format:  str           = DEFAULT_VIDEO_FORMAT
    embed_thumbnail: bool        = True
    embed_metadata:  bool        = True
    write_subtitles: bool        = False
    playlist_start:  int         = 1
    playlist_end:    Optional[int] = None
    cookies_file:    Optional[str] = None
    cookies_browser: Optional[str] = None

    # Forced metadata
    forced_title:    Optional[str] = None
    forced_artist:   Optional[str] = None
    forced_album:    Optional[str] = None
    forced_index:    Optional[int] = None
    forced_disc:     Optional[int] = None    # 1-based disc number (multi-disc releases)
    forced_total:    Optional[int] = None    # tracks in the release (single-disc only)
    forced_duration: Optional[int] = None    # seconds, for duplicate check

    # Playlist sub-folder routing
    playlist_name:   Optional[str] = None

    # Custom Thumbnail Overrides
    thumbnail_url:   Optional[str] = None

    # Proxy (passed to yt-dlp; empty/None = direct connection)
    proxy_url: Optional[str] = None

    # NEW v3 feature flags (all default to off for backward compat)
    sponsorblock:               bool              = False   # cut non-music segments
    sponsorblock_categories:    Optional[list[str]] = None  # None = use default set
    resumable:                  bool              = False   # pick up .part file if present
    embed_lyrics:           bool = False   # fetch + embed lyrics after download
    replay_gain:            bool = False   # ReplayGain analysis after download
    musicbrainz:            bool = False   # MusicBrainz tag enrichment after download
    square_thumbnails:      bool = False   # crop embedded art to 1:1 square
    expand_thumbnails:      bool = False   # pad 1:1 art to 16:9 for video
    clean_filename:         bool = False   # use minimal filename (Title only)
    is_solo:                bool = False   # single track download flag (no folder, no index, no artist name)

    # YouTube-only conservative reliability mode: "conservative" (default)
    # or "fast" (opt-in). Only affects requests whose url is a YouTube URL
    # — see core.youtube_reliability.is_youtube_url.
    youtube_reliability_mode: str = "conservative"

    # Universal / HLS / DASH stream (set when URL came from universal_extractor)
    # Values: "hls" | "dash" | "mp4" | "webm" | "ts" | None (= use yt-dlp)
    stream_type: Optional[str] = None
    platform: Optional[SourcePlatform] = None

    # Original queue-source identity.  Output routing uses this instead of
    # inferring collection semantics from the current selection size.
    source_kind: Optional[str] = None
    source_url:  Optional[str] = None

    # Category tag forwarded from TrackMeta (e.g. "stream_intercept", "stream:hls")
    category: Optional[str] = None

    # Batch workspace (utils.paths.make_batch_workspace). When set, the
    # download, conversion and all post-processing (thumbnail, MusicBrainz,
    # lyrics, ReplayGain) write here instead of directly into output_dir —
    # the finished file is atomically moved (os.replace) into output_dir
    # only once it is completely ready (see DownloadEngine.
    # _publish_to_final_location). None (default) preserves the old
    # direct-write behavior, so existing callers (tests, CLI) work
    # unchanged. Set by DownloadOrchestrator.run_batch for real batches.
    workspace_dir: Optional[str] = None

    # Post-download resume checkpoint. yt-dlp's own .part continuation only
    # covers the DOWNLOAD; a job paused after the bytes were all fetched is
    # somewhere in post-processing or publishing, and re-running yt-dlp on
    # resume would find nothing to do — no postprocessor hook fires, so
    # _final_output_path stays empty and the resumed job reports "output
    # file is missing". These two fields record where the job actually got
    # to, so a resume picks up at that phase instead of from scratch:
    #   resume_phase      None | "postprocess" | "publish"
    #   resume_final_path the workspace path of the fully-downloaded file
    # Ordinary dataclass fields (unlike the init=False trackers below) so
    # dataclasses.replace — how a pause snapshot is taken — preserves them,
    # and so they can be persisted across an application restart.
    resume_phase:      Optional[str] = None
    resume_final_path: Optional[str] = None

    # Per-request cancellation (parallel downloads)
    cancel_event: Optional[threading.Event] = field(default=None, repr=False)

    # Asked once, immediately before the finished file is made visible in
    # the user's output directory. Returning False means an outside owner (a
    # Global Pause) claimed this job first: the publish is abandoned and
    # PublishCancelled is raised, so "paused" and "published" can never both
    # be true for one job. None (default) always commits.
    publish_gate: Optional[Callable[[], bool]] = field(default=None, repr=False)

    # The counterpart to publish_gate: called when a gated attempt turns out
    # NOT to commit after all — a same-volume rename that reports the
    # destination is on another volume, or a locked-target retry. Handing
    # the claim back keeps the job pausable through the long cross-volume
    # copy, or the retry wait, that follows.
    publish_release: Optional[Callable[[], None]] = field(default=None, repr=False)

    # Lazy URL resolver (Spotify two-stage import). When set, ``url`` is a
    # placeholder and the real target URL is produced by calling this the
    # instant before the download starts — so a large catalog's YouTube
    # matching is pipelined with downloading instead of blocking it up front.
    # Receives the per-request cancel Event so a cancel stops an in-flight
    # match. Returns the resolved URL (or a ``ytsearch*`` last-resort string).
    url_resolver: Optional[Callable[[Optional[threading.Event]], str]] = field(
        default=None, repr=False
    )

    # Serializable Spotify identity retained after the lazy resolver has been
    # consumed. If that exact upload later proves private/deleted, the
    # orchestrator can invalidate only its cache row and resolve once more.
    spotify_match_identity: Optional[dict] = None

    # Callbacks
    on_progress: Optional[Callable[[DownloadProgress], None]] = field(
        default=None, repr=False
    )
    on_finished: Optional[Callable[[DownloadProgress], None]] = field(
        default=None, repr=False
    )
    on_error:    Optional[Callable[[DownloadProgress], None]] = field(
        default=None, repr=False
    )

    # Internal: set by yt-dlp post-processor hook to record the final output path.
    # Declared here (not set dynamically) so type-checkers and frozen-dataclass
    # tools can see it.
    _final_output_path: str = field(default="", init=False, repr=False)
    _thumb_sent: bool = field(default=False, init=False, repr=False)

    def snapshot_copy(self) -> "DownloadRequest":
        """A defensive copy of this request, for a pause snapshot.

        ``dataclasses.replace`` rebuilds the object through ``__init__``,
        which silently resets every ``init=False`` field — including
        ``_final_output_path``, the only in-memory record of where yt-dlp
        put the finished file. Carried across explicitly so a snapshot
        taken in the instant between yt-dlp returning and the post-download
        checkpoint being written still knows what was produced, and the
        resumed request can publish it instead of re-running a download
        that has nothing left to do.
        """
        import dataclasses

        copy = dataclasses.replace(self)
        copy._final_output_path = self._final_output_path
        copy._thumb_sent = self._thumb_sent
        return copy


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _strip_ansi_codes(text: str) -> str:
    """Remove [0;31m style escape codes from strings."""
    if not text: return ""
    import re as _re
    return _re.sub(r'\x1b\[[0-9;]*[mK]', '', text)


def _safe_file_size(path: str) -> Optional[int]:
    """Real on-disk size of a completed download, or None if unavailable.

    Used to populate DownloadProgress.downloaded_bytes/total_bytes on the
    FINISHED event so the batch aggregator (core.batch_progress) always has
    a real byte count for a completed job — even one that finished so fast
    (tiny file, cache hit) that no "downloading" progress hook ever fired.
    """
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        return size if size > 0 else None
    except OSError:
        return None


def _get_friendly_error(raw_err: str) -> str:
    """Analyze a technical yt-dlp error and append a localized tip."""
    clean = _strip_ansi_codes(raw_err)

    if "Sign in to confirm you’re not a bot" in clean or "Sign in to confirm your age" in clean or "Confirm you're not a bot" in clean:
        return f"{clean}\n\n" + t("downloader_auth_required_hint")

    if "Could not copy Chrome cookie database" in clean or "Failed to decrypt with DPAPI" in clean:
        return f"{clean}\n\n" + t("downloader_chrome_locked_hint")

    if "Signature solving failed" in clean or "n challenge solving failed" in clean:
        return (
            f"{clean}\n\n"
            + t("downloader_node_missing_hint")
            + "1. pip install quickjs\n"
            "2. pip install -U yt-dlp"
        )

    if "Requested format is not available" in clean or "Please sign in" in clean:
        return f"{clean}\n\n" + t("downloader_po_token_hint")

    if "HTTP Error 403" in clean or "Forbidden" in clean:
        return f"{clean}\n\n" + t("downloader_403_hint")

    return clean


def _sanitize_filename(name: str) -> str:
    """Sanitise a string for use as a filename stem on Windows + POSIX.

    Single source of truth: imported by ``core.duplicate_checker`` so the
    pre-download duplicate check builds the exact same stem the downloader
    writes to disk. Any change here must preserve byte equality with the
    on-disk filename.

    Truncates to 200 chars to stay under the Windows MAX_PATH=260 limit
    once a typical playlist subfolder and extension are added.
    """
    if not name:
        return "Unknown"
    # Replace restricted Windows characters with safer alternatives
    # Use two single quotes for double quotes (common practice for "שיר לממ''ד")
    # Replace colon with hyphen space for better flow
    name = name.replace('"', "''").replace(":", " - ").replace("/", "-").replace("\\", "-").replace("|", "-")
    # Remove remaining truly forbidden characters
    name = re.sub(r'[*?<>:]', " ", name)
    name = re.sub(r'\s+', " ", name)  # Collapse multiple spaces
    name = re.sub(r'[\x00-\x1f]', "", name)
    return name.strip(". ")[:200]


def _sanitize_folder_name(name: str) -> str:
    if not name:
        return "Playlist"
    # Replace colon with hyphen for safe path
    # Split by forward slash to handle hierarchical subfolders (e.g. Artist/Album)
    path_parts = name.replace("\\", "/").split("/")
    clean: list[str] = []
    for part in path_parts:
        if not part: continue
        # Sanitize individual segment
        p = part.replace('"', "''").replace(":", " - ").replace("|", "-")
        # Remove truly forbidden chars and control chars
        p = re.sub(r'[*?<> ]', " ", p)
        p = re.sub(r'\s+', " ", p)
        p = re.sub(r'[\x00-\x1f]', "", p)
        p = p.strip(". ")
        if p and p != "..":
            clean.append(p[:100])
    return "/".join(clean) if clean else "Playlist"


def _bytes_to_mb(b: Optional[int]) -> Optional[float]:
    if b is None:
        return None
    return round(b / (1024 * 1024), 2)


def _forced_metadata_args(
    req: DownloadRequest, pp_keys: tuple[str, ...],
) -> dict[str, list[str]]:
    """Build yt-dlp ``postprocessor_args`` carrying our forced metadata.

    The dict keys MUST be yt-dlp's ``PostProcessor.pp_key()`` lowercased,
    which is the class name with both the ``FFmpeg`` prefix and the ``PP``
    suffix stripped: FFmpegMetadataPP -> "metadata", FFmpegExtractAudioPP ->
    "extractaudio", FFmpegVideoConvertorPP -> "videoconvertor". Keying this
    dict by the class name instead ("FFmpegMetadata") makes yt-dlp's
    utils.cli_configuration_args lookup miss silently, so every argument here
    is dropped without a warning — that is what left album downloads with no
    ID3 TRCK frame (issue #65).

    A field we have no authoritative value for is OMITTED, never emitted as
    an empty ``-metadata key=``: an empty assignment tells ffmpeg to erase
    whatever the source already provided, and losing a correct value is worse
    than not improving it.
    """
    args: list[str] = []
    if req.forced_title:
        args += ["-metadata", f"title={req.forced_title}"]
    if req.forced_artist:
        args += ["-metadata", f"artist={req.forced_artist}"]
    if req.forced_album:
        args += ["-metadata", f"album={req.forced_album}"]
    if req.forced_index:
        track = str(req.forced_index)
        # "n/total" only where the total is unambiguous — see
        # _stamp_authoritative_position for why a multi-disc release omits it.
        if req.forced_total and not req.forced_disc:
            track = f"{track}/{req.forced_total}"
        args += ["-metadata", f"track={track}"]
    if req.forced_disc:
        args += ["-metadata", f"disc={req.forced_disc}"]

    return {key: list(args) for key in pp_keys}


def _stamp_authoritative_position(req: DownloadRequest, path: str) -> None:
    """Write the collection-authoritative track/disc position into the file.

    yt-dlp's own metadata pass only writes a track number when the *source*
    (YouTube) happens to expose one, which for the album journeys that matter
    here it does not — the position comes from the Spotify/catalog listing and
    is carried on the request. This runs at the very end of the post-download
    pipeline, after artwork embedding, MusicBrainz enrichment, lyrics and
    ReplayGain, so nothing downstream can drop the frame again, and it goes
    through the same canonical backend the Tag Editor uses, so every supported
    container gets its native field (ID3 TRCK/TPOS, Vorbis
    tracknumber/discnumber, MP4 trkn/disk).

    A request with no forced position (a single, or a bare URL) writes
    nothing at all, so independent downloads stay unnumbered.
    """
    if not req.forced_index and not req.forced_disc:
        return

    from core.metadata_backend import FORMAT_CAPABILITIES, CapabilityLevel
    from core.metadata_models import ProposedTags
    from core.metadata_processor import read_tags, write_tags

    target = Path(path)

    # Containers with no writable tag layer (webm, mkv, aac …) have nowhere to
    # put a track number. That is a property of the chosen output format, not
    # a failure of this download, so it must not raise a partial-failure
    # warning on every such file.
    level = FORMAT_CAPABILITIES.by_extension(target.suffix).level
    if level not in (CapabilityLevel.FULL, CapabilityLevel.LIMITED):
        logger.debug(
            "[Downloader] %s has no writable tag layer — skipping track position",
            target.suffix or target.name,
        )
        return
    original = read_tags(target)
    proposed = ProposedTags(
        track_num=req.forced_index or None,
        disc_num=req.forced_disc or None,
        # A release spanning discs numbers its tracks per disc, but the only
        # total we are given is for the whole release — writing that as the
        # per-disc total would be wrong, so a multi-disc release gets a bare
        # track number and no total.
        track_total=(req.forced_total or None) if not req.forced_disc else None,
    )
    if not write_tags(target, proposed, original):
        raise RuntimeError(f"could not write track position to {target.name}")


# ──────────────────────────────────────────────────────────────────────────────
# DownloadEngine
# ──────────────────────────────────────────────────────────────────────────────

class DownloadEngine:
    """
    Stateless download engine.  One instance per application lifetime.

    Create a DownloadRequest and call download() (blocking) — callers
    run it from a background thread (DownloadOrchestrator's pool / the
    CLI); the engine itself never spawns threads.
    """

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    def cancel_all(self) -> None:
        self._cancel_event.set()

    # ── Public API ─────────────────────────────────────────────────────────────

    def download(self, request: DownloadRequest) -> None:
        """Blocking download.  Safe to call from any background thread."""
        url = (request.url or "").strip()
        if not url:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=request.url,
                error_message="This unresolved track has no downloadable media target.",
            ), error=True)
            return

        if "spotify" in url.lower():
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=request.url,
                error_message="❌ Spotify URLs are not directly downloadable.",
            ), error=True)
            return

        # HLS / DASH / direct stream: bypass yt-dlp and use ffmpeg directly
        if request.stream_type in ("hls", "dash", "mp4", "webm", "ts"):
            self._download_hls_stream(request)
            return

        # Generic video page (any site): intercept HLS/DASH stream, then download via ffmpeg
        if request.category == "stream_intercept":
            self._download_with_stream_intercept(request)
            return

        cancel_ev    = request.cancel_event or self._cancel_event
        global_cancel = self._cancel_event

        self._fire(request, DownloadProgress(
            status=DownloadStatus.EXTRACTING,
            url=url,
            title=request.forced_title or "",
        ))

        try:
            resumed_phase = self._resume_checkpoint(request)
            if resumed_phase is not None:
                # This request was paused AFTER its bytes were all fetched —
                # somewhere in post-processing or publishing. Re-running
                # yt-dlp here would find the output already present, fire no
                # postprocessor hook, leave _final_output_path empty and then
                # fail with "output file is missing"; the user's paused track
                # would be unresumable. Pick up at the recorded phase instead.
                final_path = request.resume_final_path or ""
                pp_failures: list[str] = []
                logger.info(
                    "[Downloader] Resuming at the '%s' phase: %s",
                    resumed_phase, final_path,
                )
                if resumed_phase != "publish":
                    self._fire(request, DownloadProgress(
                        status=DownloadStatus.PROCESSING,
                        url=url,
                        title=request.forced_title or "",
                        output_path=final_path,
                    ))
                    pp_failures = self._run_final_pipeline(request, final_path)
                    request.resume_phase = "publish"
                return self._finalize_download(
                    request, url, final_path, pp_failures, cancel_ev, global_cancel,
                )

            # Not a post-download resume: any stale checkpoint is from a
            # workspace that no longer holds the file, so start clean.
            request.resume_phase = None
            request.resume_final_path = None

            opts = self._build_ydl_opts(request)

            def _abort_hook(_info: dict) -> None:  # noqa: ANN001
                if cancel_ev.is_set() or global_cancel.is_set():
                    raise yt_dlp.utils.DownloadCancelled()

            opts.setdefault("progress_hooks", []).append(_abort_hook)

            if request.media_type == MediaType.AUDIO:
                opts.update(self._audio_opts(request))
            else:
                opts.update(self._video_opts(request))

            # SponsorBlock (categories configurable per-request)
            if request.sponsorblock:
                sb_cats = request.sponsorblock_categories or [
                    "music_offtopic", "sponsor", "intro", "outro", "selfpromo"
                ]
                opts.setdefault("postprocessors", [])
                opts["postprocessors"].insert(0, {"key": "SponsorBlock", "categories": sb_cats})
                opts["postprocessors"].insert(1, {
                    "key": "ModifyChapters",
                    "remove_sponsor_segments": sb_cats,
                })

            # Resume / continuedl
            if request.resumable:
                opts["continuedl"] = True

            # Downloads run several-at-once (see download_orchestrator's
            # ThreadPoolExecutor) — a private copy of cookiefile keeps
            # concurrent yt-dlp instances from racing on the same shared,
            # yt-dlp-rewritten file.
            with temp_cookies_copy(opts.get("cookiefile")) as cf:
                opts["cookiefile"] = cf

                max_retries = 3
                with yt_dlp.YoutubeDL(opts) as ydl:
                    for attempt in range(max_retries):
                        try:
                            ydl.download([url])
                            # Checkpoint the INSTANT yt-dlp hands control
                            # back — not after the two context-manager
                            # exits (which do real file I/O), the
                            # cancellation check and the existence probe
                            # that used to sit between here and the
                            # assignment. A pause landing anywhere in that
                            # gap took a snapshot with no checkpoint and —
                            # because _final_output_path is init=False, so
                            # dataclasses.replace resets it — no output path
                            # either. The resumed job then re-ran yt-dlp
                            # against a file that was already complete, so
                            # no postprocessor hook fired and it died with
                            # "output file is missing".
                            self._record_post_download_checkpoint(request)
                            break
                        except Exception as exc:
                            # Check for Windows file-lock errors by winerror code (locale-safe)
                            # winerror 5 = ACCESS_DENIED, winerror 32 = SHARING_VIOLATION
                            winerror = getattr(exc, "winerror", None)
                            is_locked = winerror in (5, 32)
                            if is_locked and attempt < max_retries - 1:
                                logger.warning("[Downloader] File locked, retrying in 2s... (Attempt %d/%d)", attempt + 1, max_retries)
                                time.sleep(2)
                                continue
                            raise

            # ── Finalized: Run custom pipeline before emitting FINISHED ──────────────
            # yt-dlp's own abort_hook only fires DURING its own download loop
            # — it cannot see a cancel that arrives after yt-dlp returns but
            # before post-processing/publish run. Both of those still write
            # real work (ffmpeg conversions, network lookups, a file move
            # into the user's visible output directory), so a pause/cancel
            # requested in this narrow window must still be honoured, or a
            # cancelled job can be reported as a completed, published
            # download. Checked once before the pipeline starts and once
            # more before publish, since the pipeline itself can take a
            # while (MusicBrainz/lyrics network calls, ReplayGain analysis).
            if cancel_ev.is_set() or global_cancel.is_set():
                raise yt_dlp.utils.DownloadCancelled()

            final_path = request._final_output_path  # noqa: SLF001
            if final_path and os.path.exists(final_path):
                # The "postprocess" checkpoint was already written the
                # moment yt-dlp returned (see above), so a pause taken
                # anywhere from here on already carries the phase and the
                # workspace file identity it needs to resume.

                # Notify UI we are processing
                self._fire(request, DownloadProgress(
                    status=DownloadStatus.PROCESSING,
                    url=url,
                    title=request.forced_title or "",
                    output_path=final_path,
                ))

                # Execute steps; collect non-fatal failures for the UI
                pp_failures = self._run_final_pipeline(request, final_path)
                request.resume_phase = "publish"
            else:
                # If the file wasn't created, yt-dlp failed silently (e.g., ytsearch found nothing)
                raise Exception("Download completed but output file is missing. (Search may have yielded no results)")

            return self._finalize_download(
                request, url, final_path, pp_failures, cancel_ev, global_cancel,
            )

        except (yt_dlp.utils.DownloadCancelled, PublishCancelled):
            # PublishCancelled: the user stopped the job while the finished
            # file was being published. Nothing was committed to the output
            # directory — this is a cancellation, never an error.
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED,
                url=url,
                title=request.forced_title or "",
            ))
        except PublishError as exc:
            # A real failure, but a specific and self-explanatory one — the
            # generic handler below would relabel it "Unexpected error".
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=url,
                title=request.forced_title or "",
                error_message=str(exc),
            ), error=True)
        except yt_dlp.utils.DownloadError as exc:
            evidence = ""
            if isinstance(locals().get("opts"), dict):
                candidate_logger = opts.get("logger")
                evidence = getattr(candidate_logger, "failure_evidence", "")
            combined = f"{evidence} | {exc}" if evidence else str(exc)
            err_msg = _get_friendly_error(combined)
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=url,
                title=request.forced_title or "",
                error_message=err_msg,
            ), error=True)
        except Exception as exc:
            err_msg = _get_friendly_error(f"Unexpected error: {exc}")
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=url,
                title=request.forced_title or "",
                error_message=err_msg,
            ), error=True)

    def cancel(self) -> None:
        self._cancel_event.set()

    # ── Post-download resume checkpoint ───────────────────────────────────────

    @staticmethod
    def _record_post_download_checkpoint(req: DownloadRequest) -> None:
        """Mark the download half finished: yt-dlp has produced its output.

        Called as the very next operation after ``ydl.download()`` returns,
        and deliberately does no I/O of its own — the point is that nothing
        can run between yt-dlp finishing and this being written. Whether the
        recorded file actually still exists is validated later, by
        _resume_checkpoint, at the moment a resume would rely on it.

        A request that produced nothing (a ytsearch that matched no video)
        gets no checkpoint, so it falls through to the normal
        "output file is missing" error rather than claiming a resume point
        it does not have.
        """
        path = req._final_output_path  # noqa: SLF001
        if not path:
            return
        req.resume_final_path = path
        req.resume_phase = "postprocess"

    @staticmethod
    def _resume_checkpoint(req: DownloadRequest) -> Optional[str]:
        """The phase this request should resume at, or None to download.

        A pause taken after yt-dlp finished (during post-processing, or
        during the publish) records where the job got to on the request
        itself, and the fully-downloaded file stays in the job's workspace.
        Both have to still be true for a resume to short-circuit the
        download: a checkpoint whose file is gone (the workspace was swept,
        or the user deleted it) is stale and the job downloads again from
        scratch.
        """
        phase = req.resume_phase
        if phase not in ("postprocess", "publish"):
            return None
        path = req.resume_final_path or ""
        if not path or not os.path.exists(path):
            return None
        return phase

    def _finalize_download(
        self,
        request:       DownloadRequest,
        url:           str,
        final_path:    str,
        pp_failures:   list[str],
        cancel_ev:     threading.Event,
        global_cancel: threading.Event,
    ) -> None:
        """Publish the finished file and emit FINISHED.

        Shared by the ordinary path and the post-download resume path so
        both honour the same last cancellation check, the same atomic
        publish and the same completion reporting.
        """
        if cancel_ev.is_set() or global_cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled()

        warning_msg = ""
        if pp_failures:
            warning_msg = "Post-processing partial failure: " + "; ".join(pp_failures)
            logger.warning(f"[Downloader] {warning_msg}")

        # Atomic publish: only now — after conversion AND every
        # post-processing step succeeded or failed — is the file moved
        # out of the hidden batch workspace into the user's real output
        # directory. A no-op when request.workspace_dir isn't set.
        final_path = self._publish_to_final_location(request, final_path)

        # Published: the checkpoint has nothing left to protect, and leaving
        # it set would make a re-submitted request skip its own download.
        request.resume_phase = None
        request.resume_final_path = None

        # Report the real on-disk size on completion. A fast/tiny/cached
        # download can finish before yt-dlp ever fires a "downloading"
        # progress hook, in which case the batch aggregator would
        # otherwise never learn this job's true byte size and would fall
        # back to estimating it from other jobs (core.batch_progress's
        # unknown-size path) — accurate but avoidable when the real
        # number is sitting right there on disk.
        final_bytes = _safe_file_size(final_path)

        self._fire(request, DownloadProgress(
            status=DownloadStatus.FINISHED,
            url=url,
            title=request.forced_title or "",
            fraction=1.0,
            downloaded_bytes=final_bytes or 0,
            total_bytes=final_bytes,
            warning_message=warning_msg,
            output_path=final_path,
        ))

    # ── HLS / DASH stream download via ffmpeg ─────────────────────────────────

    def _download_hls_stream(self, request: DownloadRequest) -> None:
        """Download a raw HLS/DASH/direct stream URL using ffmpeg (not yt-dlp)."""
        from core.hls_downloader import download_hls, HlsCancelled

        cancel_ev = request.cancel_event or self._cancel_event
        url       = request.url
        if request.media_type == MediaType.AUDIO:
            ext = request.audio_format or DEFAULT_AUDIO_FORMAT
        else:
            ext = request.video_format or DEFAULT_VIDEO_FORMAT

        out_dir   = Path(request.workspace_dir or request.output_dir).expanduser().resolve()
        if request.playlist_name:
            out_dir = out_dir / request.playlist_name
        out_dir.mkdir(parents=True, exist_ok=True)

        # Build filename
        title  = request.forced_title or "stream"
        stem   = title
        # Sanitize
        stem = re.sub(r'[\\/*?:"<>|]', "_", stem)
        if request.forced_index:
            # "NN - " is the one numbering convention in this app: the yt-dlp
            # output templates build it and core.duplicate_checker.expected_stem
            # looks for it. This path used to omit the separator, so its files
            # sorted alongside everything else but were invisible to the
            # duplicate check.
            stem = f"{request.forced_index:02d} - {stem}"

        output_path = str(out_dir / f"{stem}.{ext}")

        self._fire(request, DownloadProgress(
            status=DownloadStatus.DOWNLOADING,
            url=url,
            title=title,
        ))

        try:
            download_hls(
                url=url,
                output_path=output_path,
                cookies_file=request.cookies_file,
                cancel_event=cancel_ev,
            )
        except HlsCancelled:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED, url=url, title=title,
            ))
            return
        except Exception as exc:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=url,
                title=title,
                error_message=str(exc),
            ), error=True)
            return

        # ffmpeg ran to completion, but a cancel may have arrived in the
        # instant right after — checked before publish for the same reason
        # as the main yt-dlp path (see download()).
        if cancel_ev.is_set() or self._cancel_event.is_set():
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED, url=url, title=title,
            ))
            return

        # This path builds the numbered filename itself and never went
        # through yt-dlp's metadata postprocessor, so the collection position
        # would otherwise reach the filename and nothing else. Only the
        # position is stamped here — the rest of _run_final_pipeline
        # (artwork, MusicBrainz, lyrics, ReplayGain) has never run for raw
        # stream downloads and enabling it is a separate decision.
        hls_warning = ""
        try:
            _stamp_authoritative_position(request, output_path)
        except Exception as exc:
            logger.error("[Downloader] Track position error (HLS): %s", exc, exc_info=True)
            hls_warning = f"Post-processing partial failure: track number: {exc}"

        try:
            output_path = self._publish_to_final_location(request, output_path)
        except PublishCancelled:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED, url=url, title=title,
            ))
            return
        except PublishError as exc:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR, url=url, title=title,
                error_message=str(exc),
            ), error=True)
            return
        hls_bytes = _safe_file_size(output_path)
        self._fire(request, DownloadProgress(
            status=DownloadStatus.FINISHED,
            url=url,
            title=title,
            fraction=1.0,
            downloaded_bytes=hls_bytes or 0,
            total_bytes=hls_bytes,
            warning_message=hls_warning,
            output_path=output_path,
        ))

    # ── Generic stream-intercept download (any video page) ───────────────────

    def _download_with_stream_intercept(self, request: DownloadRequest) -> None:
        """
        Universal video page downloader — works for any site whose video pages
        set category='stream_intercept'.

        Steps:
          1. Open the video page with Playwright (headless)
          2. Intercept the best HLS/DASH stream URL
          3. Download via ffmpeg (same mechanism as mpmux.com staticdownloader)

        Falls back to yt-dlp if stream interception fails.
        """
        page_url = request.url

        self._fire(request, DownloadProgress(
            status=DownloadStatus.EXTRACTING,
            url=page_url,
            title=request.forced_title or "",
        ))

        logger.debug("[Downloader] Intercepting stream from %s", page_url)

        try:
            from core.universal_extractor import find_best_stream_with_title
            stream_url, stream_type, page_title = find_best_stream_with_title(
                page_url, timeout_ms=35_000
            )
        except Exception as exc:
            logger.warning("[Downloader] Stream interception failed: %s — falling back to yt-dlp", exc)
            stream_url = ""
            stream_type = "unknown"
            page_title = ""

        if not stream_url or stream_type == "unknown":
            logger.info("[Downloader] No stream intercepted — trying yt-dlp for %s", page_url)
            # We do NOT return early here; we call the yt-dlp path below
            try:
                opts = self._build_ydl_opts(request)
                cancel_ev = request.cancel_event or self._cancel_event
                if request.media_type == MediaType.AUDIO:
                    opts.update(self._audio_opts(request))
                else:
                    opts.update(self._video_opts(request))

                def _abort_hook(_info: dict) -> None:
                    if cancel_ev.is_set() or self._cancel_event.is_set():
                        raise yt_dlp.utils.DownloadCancelled()

                opts.setdefault("progress_hooks", []).append(_abort_hook)

                with temp_cookies_copy(opts.get("cookiefile")) as cf:
                    opts["cookiefile"] = cf
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([page_url])

                final_path = request._final_output_path  # noqa: SLF001
                if final_path and not os.path.exists(final_path):
                    raise Exception("Download completed but output file is missing. (Search may have yielded no results)")

                # See download()'s matching check: a cancel can arrive after
                # yt-dlp returns but before publish.
                if cancel_ev.is_set() or self._cancel_event.is_set():
                    raise yt_dlp.utils.DownloadCancelled()

                final_path = self._publish_to_final_location(request, final_path)
                generic_bytes = _safe_file_size(final_path)
                self._fire(request, DownloadProgress(
                    status=DownloadStatus.FINISHED,
                    url=page_url,
                    title=request.forced_title or "",
                    fraction=1.0,
                    downloaded_bytes=generic_bytes or 0,
                    total_bytes=generic_bytes,
                    output_path=final_path,
                ))
            except (yt_dlp.utils.DownloadCancelled, PublishCancelled):
                self._fire(request, DownloadProgress(
                    status=DownloadStatus.CANCELLED,
                    url=page_url,
                    title=request.forced_title or "",
                ))
            except PublishError as exc:
                self._fire(request, DownloadProgress(
                    status=DownloadStatus.ERROR,
                    url=page_url,
                    title=request.forced_title or "",
                    error_message=str(exc),
                ), error=True)
            except Exception as exc:
                err_msg = _get_friendly_error(str(exc))
                self._fire(request, DownloadProgress(
                    status=DownloadStatus.ERROR,
                    url=page_url,
                    title=request.forced_title or "",
                    error_message=err_msg,
                ), error=True)
            return

        # Use page_title if our forced_title is a generic placeholder
        title = request.forced_title
        if (not title or title in ("Unknown Title", "stream")) and page_title:
            title = page_title
        if not title:
            title = page_url.rstrip("/").split("/")[-1].replace("-", " ") or "stream"

        # Update the request so the filename builder uses the real title
        request.forced_title = title

        logger.info(
            "[Downloader] Intercepted %s stream for '%s'",
            stream_type.upper(), title,
        )

        # Build output path
        if request.media_type == MediaType.AUDIO:
            ext = request.audio_format or DEFAULT_AUDIO_FORMAT
        else:
            ext = request.video_format or DEFAULT_VIDEO_FORMAT

        out_dir = Path(request.workspace_dir or request.output_dir).expanduser().resolve()
        if request.playlist_name:
            out_dir = out_dir / _sanitize_folder_name(request.playlist_name)
        out_dir.mkdir(parents=True, exist_ok=True)

        stem = _sanitize_filename(title)
        if request.forced_index:
            stem = f"{request.forced_index:02d} - {stem}"

        output_path = str(out_dir / f"{stem}.{ext}")
        cancel_ev = request.cancel_event or self._cancel_event

        self._fire(request, DownloadProgress(
            status=DownloadStatus.DOWNLOADING,
            url=page_url,
            title=title,
        ))

        try:
            from core.hls_downloader import download_hls, HlsCancelled
            download_hls(
                url=stream_url,
                output_path=output_path,
                cookies_file=request.cookies_file,
                cancel_event=cancel_ev,
            )
        except HlsCancelled:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED, url=page_url, title=title,
            ))
            return
        except Exception as exc:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR,
                url=page_url,
                title=title,
                error_message=str(exc),
            ), error=True)
            return

        if cancel_ev.is_set() or self._cancel_event.is_set():
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED, url=page_url, title=title,
            ))
            return

        # Same reason as the HLS path: ffmpeg wrote the container directly, so
        # no metadata postprocessor ever ran over it.
        stream_warning = ""
        try:
            _stamp_authoritative_position(request, output_path)
        except Exception as exc:
            logger.error("[Downloader] Track position error (stream): %s", exc, exc_info=True)
            stream_warning = f"Post-processing partial failure: track number: {exc}"

        try:
            output_path = self._publish_to_final_location(request, output_path)
        except PublishCancelled:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.CANCELLED, url=page_url, title=title,
            ))
            return
        except PublishError as exc:
            self._fire(request, DownloadProgress(
                status=DownloadStatus.ERROR, url=page_url, title=title,
                error_message=str(exc),
            ), error=True)
            return
        stream_bytes = _safe_file_size(output_path)
        self._fire(request, DownloadProgress(
            status=DownloadStatus.FINISHED,
            url=page_url,
            title=title,
            fraction=1.0,
            downloaded_bytes=stream_bytes or 0,
            total_bytes=stream_bytes,
            warning_message=stream_warning,
            output_path=output_path,
        ))

    # ── yt-dlp options builder ─────────────────────────────────────────────────

    def _build_ydl_opts(self, req: DownloadRequest) -> dict[str, Any]:
        # Write into the batch workspace when one is set (see
        # DownloadRequest.workspace_dir) — _publish_to_final_location moves
        # the finished file into req.output_dir once it's fully ready.
        out_dir = Path(req.workspace_dir or req.output_dir).expanduser().resolve()

        # Playlist subfolder
        if req.playlist_name:
            sub = _sanitize_folder_name(req.playlist_name)
            out_dir = out_dir / sub

        out_dir.mkdir(parents=True, exist_ok=True)

        # Output template
        raw_title = req.forced_title if (req.forced_title and req.forced_title != "Unknown Title") else None
        use_ydlp_title = raw_title is None

        if use_ydlp_title:
            raw_title = "%(title)s"

        # Comprehensive clean: strip common parenthetical labels and promotional suffixes
        # WE EXCLUDE 'Remix', 'Edit', 'Acoustic', 'Live' to prevent collisions in EPs
        clean_title = re.sub(r'\s*[([].*?(Official|Video|Clip|Audio|Prod|By|Remaster|Lyrics|HD|4K|Direct|Studio).*?[)\]]', '', raw_title, flags=re.IGNORECASE)
        # Strip anything in parens at the end
        clean_title = re.sub(r'\s*\([^)]*\)\s*$', '', clean_title).strip()
        # Strip trailing hyphens or dashes followed by common tags
        clean_title = re.sub(r'\s*-\s*(Club Edit|Official|Prod|Original).*$', '', clean_title, flags=re.IGNORECASE).strip()
        
        if not clean_title:
            clean_title = raw_title

        title = _sanitize_filename(clean_title)

        if use_ydlp_title:
            # No forced title — let yt-dlp determine the title
            if req.is_solo:
                outtmpl = str(out_dir / "%(title)s.%(ext)s")
            elif req.clean_filename:
                idx_prefix = f"{req.forced_index:02d} - " if (req.forced_index is not None and req.forced_index > 0) else ""
                outtmpl = str(out_dir / f"{idx_prefix}%(title)s.%(ext)s")
            else:
                outtmpl = str(out_dir / "%(playlist_index)s%(title)s.%(ext)s")
        elif req.is_solo:
            # Solo download: No artist, no index, just the clean title.
            outtmpl = str(out_dir / f"{title}.%(ext)s")
        elif req.clean_filename:
            # IMPORTANT: For clean_filename, we ONLY use the title, NO artist.
            idx_prefix = f"{req.forced_index:02d} - " if (req.forced_index is not None and req.forced_index > 0) else ""
            outtmpl = str(out_dir / f"{idx_prefix}{title}.%(ext)s")
        elif req.forced_title or req.forced_artist:
            idx_prefix = f"{req.forced_index:02d} - " if (req.forced_index is not None and req.forced_index > 0) else ""
            artist     = _sanitize_filename(req.forced_artist or "Unknown Artist")
            # In the 'Artist - Title' format, we still use the cleaned title
            outtmpl    = str(out_dir / f"{idx_prefix}{artist} - {title}.%(ext)s")
        else:
            outtmpl = str(out_dir / "%(playlist_index)s%(title)s.%(ext)s")

        # Automatic pickup of wizard cookies
        cookies_file = req.cookies_file
        if not cookies_file and not req.cookies_browser:
            wizard_cookies = get_app_cookies_path()
            if wizard_cookies.exists():
                cookies_file = str(wizard_cookies)

        # Warn if cookies are expired (non-blocking)
        if cookies_file:
            valid, warn_msg = check_cookies_valid(cookies_file)
            if not valid:
                from utils.yt_dlp_opts import note_cookie_diagnostic
                note_cookie_diagnostic(warn_msg)
                logger.debug("[Downloader][cookies] preflight diagnostic coalesced: %s", warn_msg)

        ytdlp_logger = SilentLogger()
        opts: dict[str, Any] = _build_base_opts(
            cookies_file=cookies_file or None,
            cookies_browser=req.cookies_browser or None,
            logger=ytdlp_logger,
            quiet=True,
            retries=10,
            proxy=req.proxy_url or None,
        )

        opts["outtmpl"]           = outtmpl
        opts["restrictfilenames"] = False
        opts["windowsfilenames"]  = True
        opts["ignoreerrors"]      = False
        opts["playliststart"]     = req.playlist_start
        if req.playlist_end:
            opts["playlistend"]   = req.playlist_end

        # Point yt-dlp at the bundled LGPL FFmpeg when the app is
        # installed as a frozen EXE; otherwise yt-dlp uses PATH.
        ffmpeg_dir = get_bundled_ffmpeg_dir()
        if ffmpeg_dir is not None:
            opts["ffmpeg_location"] = str(ffmpeg_dir)

        opts["progress_hooks"]      = [self._make_progress_hook(req)]
        opts["postprocessor_hooks"] = [self._make_pp_hook(req)]
        opts["no_warnings"]         = True

        # YouTube-only conservative reliability mode: single-fragment
        # concurrency for this URL. This is the only thing this method
        # controls — cross-job parallelism and the inter-job cooldown are
        # decided and logged by DownloadOrchestrator (only when a batch
        # actually has more than one YouTube job to serialize), so this
        # log line must not claim parallel=/delay= behavior that may not
        # happen at all for this request.
        if req.youtube_reliability_mode == "conservative" and is_youtube_url(req.url):
            opts["concurrent_fragment_downloads"] = CONSERVATIVE_FRAGMENT_CONCURRENCY
            logger.info(
                "[yt-dlp][youtube_conservative] fragment_concurrency=%d",
                CONSERVATIVE_FRAGMENT_CONCURRENCY,
            )

        # Reliable YouTube player client selection:
        # For audio-only downloads, android/ios player clients provide rapid, reliable streams without 403 blocks.
        # For video downloads, web/web_embedded player clients are used so full 4K / 1440p / 1080p Full HD
        # streams are extracted and merged with high-bitrate audio, rather than being restricted to 360p mobile formats.
        if is_youtube_url(req.url) or (req.url and req.url.startswith(("ytsearch:", "ytsearchdate:"))):
            if req.media_type == MediaType.AUDIO:
                opts.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = ["android", "ios"]
            else:
                opts.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = ["web", "web_embedded"]

        return opts

    # ── Format-specific option builders ───────────────────────────────────────

    @staticmethod
    def _audio_opts(req: DownloadRequest) -> dict[str, Any]:
        preset = audio_preset_for_quality(req.audio_quality)
        if preset.codec != req.audio_format:
            raise ValueError(
                f"Audio quality preset {preset.id!r} is for {preset.codec}, "
                f"not {req.audio_format!r}"
            )

        extractor = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": req.audio_format,
        }
        if preset.preferredquality is not None:
            extractor["preferredquality"] = preset.preferredquality

        postprocessors: list[dict] = [
            extractor,
        ]
        if req.embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        use_ytdlp_thumb = req.embed_thumbnail and not req.thumbnail_url
        if use_ytdlp_thumb:
            postprocessors.append({"key": "EmbedThumbnail"})

        opts: dict[str, Any] = {
            "format":         "bestaudio/best",
            "postprocessors": postprocessors,
            "writethumbnail": use_ytdlp_thumb,
        }

        opts["postprocessor_args"] = _forced_metadata_args(
            req, ("metadata", "extractaudio"),
        )

        return opts

    @staticmethod
    def _video_opts(req: DownloadRequest) -> dict[str, Any]:
        preset = video_preset_for_quality(req.video_quality)
        video_format = req.video_format or DEFAULT_VIDEO_FORMAT
        postprocessors: list[dict] = [
            {"key": "FFmpegVideoConvertor", "preferedformat": video_format},
        ]
        if req.embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        use_ytdlp_thumb = req.embed_thumbnail and not req.thumbnail_url
        if use_ytdlp_thumb:
            postprocessors.append({"key": "EmbedThumbnail"})
        if req.write_subtitles:
            postprocessors.append({
                "key": "FFmpegEmbedSubtitle",
                "already_have_subtitle": False,
            })

        opts: dict[str, Any] = {
            "format":              preset.format_selector,
            "postprocessors":      postprocessors,
            "merge_output_format": video_format,
            "writethumbnail":      use_ytdlp_thumb,
        }
        if preset.format_sort:
            opts["format_sort"] = list(preset.format_sort)
        if req.write_subtitles:
            opts["writesubtitles"]  = True
            opts["subtitleslangs"]  = ["en"]
            opts["subtitlesformat"] = "vtt"

        opts["postprocessor_args"] = _forced_metadata_args(
            req, ("metadata", "videoconvertor"),
        )

        return opts

    # ── Hook factories ─────────────────────────────────────────────────────────

    def _make_progress_hook(self, req: DownloadRequest) -> Callable[[dict], None]:
        def hook(d: dict) -> None:
            ydl_status = d.get("status", "")
            info       = d.get("info_dict", {})
            title      = info.get("title", d.get("filename", ""))
            pl_idx     = info.get("playlist_index")
            pl_count   = info.get("n_entries")
            dl_bytes   = d.get("downloaded_bytes", 0)
            total_real = d.get("total_bytes")
            total_est  = d.get("total_bytes_estimate")
            total      = total_real or total_est
            speed      = d.get("speed")
            eta        = d.get("eta")
            thumb      = info.get("thumbnail")

            fraction: float = 0.0
            if total and total > 0:
                fraction = min(dl_bytes / total, 1.0)

            if ydl_status == "downloading":
                self._fire(req, DownloadProgress(
                    status=DownloadStatus.DOWNLOADING,
                    url=req.url,
                    title=title,
                    playlist_index=pl_idx,
                    playlist_count=pl_count,
                    downloaded_bytes=dl_bytes,
                    total_bytes=total_real,
                    total_bytes_estimate=total_est,
                    speed_bps=speed,
                    eta_seconds=eta,
                    fraction=fraction,
                    thumbnail_url=thumb,
                ))

            elif ydl_status == "finished":
                self._fire(req, DownloadProgress(
                    status=DownloadStatus.PROCESSING,
                    url=req.url,
                    title=title,
                    downloaded_bytes=dl_bytes,
                    total_bytes=total_real,
                    total_bytes_estimate=total_est,
                    fraction=0.95,
                ))

            elif ydl_status == "error":
                self._fire(req, DownloadProgress(
                    status=DownloadStatus.ERROR,
                    url=req.url,
                    title=title,
                    error_message=d.get("error", "Unknown yt-dlp error"),
                ), error=True)

        return hook

    def _make_pp_hook(self, req: DownloadRequest) -> Callable[[dict], None]:
        """Post-processor hook fires after every FFmpeg stage."""
        def hook(d: dict) -> None:
            if d.get("status") != "finished":
                return

            pp_key = (d.get("postprocessor", "") or "").lower()
            output_path: str = d.get("info_dict", {}).get("filepath", "") or ""

            logger.debug("[Downloader] PP Hook: status=finished, pp=%s, path=%s", pp_key, output_path)

            if output_path:
                output_path = os.path.abspath(output_path)
                # Capture the most recent valid file path
                if not req._final_output_path or os.path.exists(output_path):  # noqa: SLF001
                    req._final_output_path = output_path  # noqa: SLF001

        return hook

    # ── Atomic publish (batch workspace -> real output directory) ────────────

    def _cancel_check(self, req: DownloadRequest) -> Callable[[], bool]:
        """A cheap "should this job stop right now?" predicate covering both
        the per-request cancel Event and the engine-wide one. Handed to the
        publish step so a cancel arriving *during* publication is honoured
        instead of being noticed only after the file has already appeared in
        the user's output directory."""
        per_request = req.cancel_event
        global_cancel = self._cancel_event

        def _cancelled() -> bool:
            return bool(
                (per_request is not None and per_request.is_set())
                or global_cancel.is_set()
            )

        return _cancelled

    def _publish_to_final_location(self, req: DownloadRequest, workspace_path: str) -> str:
        """Atomically move a fully-ready file out of the batch workspace and
        into the user's real output directory. Returns the final published
        path on success.

        Called only after every post-processing step (conversion, thumbnail,
        MusicBrainz, lyrics, ReplayGain) has already finished — the user must
        never see a half-built file appear in their output folder. A no-op
        that returns ``workspace_path`` unchanged when ``req.workspace_dir``
        is not set, preserving the old direct-write behavior for callers
        that don't opt into a workspace (tests, CLI).

        Raises ``PublishError`` when the file cannot be published. A publish
        failure must NEVER be reported as a completed download: the file is
        still sitting in the (about-to-be-cleaned) workspace, so silently
        returning that path would surface a "done" card pointing at a file
        that is about to vanish. The caller turns the raise into a normal
        per-track error instead. On failure the existing destination file,
        if any, is left untouched (os.replace is atomic; the fallback path
        only os.replace's an already-complete temp copy).

        Same-volume (the normal case — the workspace is nested under
        output_dir) is a pure atomic ``os.replace``. A cross-volume
        workspace (the app-data fallback in make_batch_workspace) is
        published by copying to a hidden temp file adjacent to the
        destination and then os.replace'ing that into place — still atomic
        at the visible destination, still no half-built file ever seen.

        Raises ``PublishCancelled`` if the job is cancelled/paused before
        the destination is committed — including part-way through a
        cross-volume copy, which is the one step here that is not
        instantaneous. Without that, a cancel arriving after the caller's
        last pre-publish check would still produce a published file and a
        reported success.
        """
        cancelled = self._cancel_check(req)
        if cancelled():
            raise PublishCancelled("Cancelled before publish")

        if not req.workspace_dir:
            # No workspace: the file was written straight to its final
            # location, so this is the commit point.
            if req.publish_gate is not None and not req.publish_gate():
                raise PublishCancelled("Paused before publish")
            return workspace_path

        try:
            workspace_root = Path(req.workspace_dir).expanduser().resolve()
            src = Path(workspace_path).resolve()
            relative = src.relative_to(workspace_root)
        except (ValueError, OSError) as exc:
            # A path that isn't under the declared workspace must not be
            # accepted as a successfully published result — publishing it
            # would move an arbitrary file, or (returning it) claim success
            # for a file outside the isolation boundary.
            raise PublishError(
                f"Refusing to publish {workspace_path!r}: not inside workspace "
                f"{req.workspace_dir!r} ({exc})"
            ) from exc

        dest = Path(req.output_dir).expanduser().resolve() / relative
        dest.parent.mkdir(parents=True, exist_ok=True)

        last_exc: Optional[BaseException] = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self._atomic_place(
                    src, dest,
                    cancel_check=cancelled,
                    commit_gate=req.publish_gate,
                    commit_release=req.publish_release,
                )
                return str(dest)
            except OSError as exc:
                last_exc = exc
                # Windows file-lock errors, same codes/retry as the yt-dlp
                # download retry above: winerror 5 = ACCESS_DENIED,
                # winerror 32 = SHARING_VIOLATION (locale-safe).
                winerror = getattr(exc, "winerror", None)
                if winerror in (5, 32) and attempt < max_retries - 1:
                    logger.warning(
                        "[Downloader] Publish target locked, retrying in "
                        "2s... (Attempt %d/%d)", attempt + 1, max_retries,
                    )
                    time.sleep(2)
                    continue
                break

        logger.error("[Downloader] Failed to publish %s -> %s: %s", src, dest, last_exc)
        raise PublishError(f"Could not publish to {dest}: {last_exc}") from last_exc

    # Recognisable marker so a startup sweep (utils.paths) can find and
    # remove a stray cross-volume publish temp left behind by a crash —
    # see PUBLISH_TMP_SUFFIX / sweep_stale_publish_temp_files.
    PUBLISH_TMP_SUFFIX = ".bananaflow-publish-tmp"

    # Chunk size for the cross-volume staging copy. Small enough that a
    # cancel is honoured promptly on a slow volume, large enough that the
    # per-chunk check costs nothing measurable against real I/O.
    _PUBLISH_COPY_CHUNK = 1024 * 1024

    @classmethod
    def _atomic_place(
        cls,
        src: Path,
        dest: Path,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        commit_gate: Optional[Callable[[], bool]] = None,
        commit_release: Optional[Callable[[], None]] = None,
    ) -> None:
        """Put ``src`` at ``dest`` atomically at the visible destination.

        Fast path: same-volume ``os.replace`` (a pure rename). Cross-volume
        (raises OSError with errno EXDEV): copy to a hidden temp file next
        to dest, then os.replace the temp into place — that rename is
        same-volume and atomic, so the destination only ever flips from
        "old/absent" to "fully-copied new", never a partial. The temp is
        cleaned up on any failure so a cross-volume error can't strand a
        half-copy.

        The temp name is generated by ``tempfile.mkstemp`` (a securely
        unique name, not a pid-based one) so two concurrent publishes —
        including a retry racing an earlier attempt, or two jobs that
        legitimately target the same destination filename — can never
        collide on the same temp path.

        The temp is hidden BEFORE a single byte of content is written, and
        the content is then written through mkstemp's own already-open
        descriptor. That ordering matters on Windows: it is *re-opening* an
        already-hidden file for writing that fails (CreateFile without
        FILE_ATTRIBUTE_HIDDEN on an existing hidden file), which is what
        made an earlier version hide the temp only after copying — leaving
        a visible, growing partial file sitting in the user's output folder
        for the whole duration of the copy. Writing through the existing
        handle never re-opens anything, so the file can be hidden from
        birth and the user never sees a partial. The attribute is cleared
        again immediately before the rename, because os.replace carries the
        source's attributes across and would otherwise publish the finished
        file as a hidden file.

        Neither attribute operation is best-effort, and both results are
        checked. If the temp cannot be hidden, the only alternative is to
        write a visible growing partial into the user's output folder —
        precisely what this staging step exists to prevent — so the publish
        is refused instead (the same stance make_batch_workspace takes
        toward a location it cannot hide). If the attribute cannot be
        cleared again, the rename is refused rather than publishing the
        user's finished track as a file they cannot see. Both leave the
        destination untouched and the file safely in the workspace.

        ``cancel_check`` is polled between copy chunks and once more
        immediately before the final rename; when it reports cancellation
        the staging temp is removed and ``PublishCancelled`` is raised, so a
        cancel during a long cross-volume copy can never be overtaken by a
        published file and a reported success. A crash before the final
        rename can still leave the (hidden) temp behind, which is why its
        name carries PUBLISH_TMP_SUFFIX — a recognisable marker a startup
        sweep can find and remove (see
        utils.paths.sweep_stale_publish_temp_files).

        ``commit_gate`` gets the last word before the rename that makes the
        file visible: an outside owner (a Global Pause) that has claimed
        this job returns False and the publish is abandoned. It is asked
        here, not by the caller, precisely because here is the only place
        genuinely adjacent to the commit.

        ``commit_release`` undoes a gate that did not lead to a commit. The
        same-volume attempt has to be gated before it runs — the gate and
        the rename must be inseparable — but that attempt may report the
        destination is on another volume, or fail on a locked target and be
        retried seconds later. Handing the claim back in those cases is what
        keeps the job pausable across the long cross-volume copy, and across
        the caller's retry wait, instead of freezing it as "terminal" for
        the whole operation while nothing has actually been published.
        """
        import errno
        import tempfile

        def _cancelled() -> bool:
            return bool(cancel_check is not None and cancel_check())

        def _may_commit() -> bool:
            return commit_gate is None or bool(commit_gate())

        def _undo_commit_claim() -> None:
            if commit_release is not None:
                commit_release()

        if _cancelled():
            raise PublishCancelled("Cancelled before publish")
        if not _may_commit():
            raise PublishCancelled("Paused before publish")
        try:
            os.replace(str(src), str(dest))
            return
        except OSError as exc:
            # Nothing was committed, so the claim taken above is given back
            # before either continuing to the (slow) cross-volume path or
            # handing a retryable error to the caller.
            _undo_commit_claim()
            if getattr(exc, "errno", None) != errno.EXDEV:
                raise

        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=f".{dest.name}.", suffix=cls.PUBLISH_TMP_SUFFIX,
        )
        tmp = Path(tmp_name)
        # Hide first, write second — see the docstring. NOT best-effort:
        # if the attribute cannot be applied, the alternative is writing a
        # visible, growing partial into the user's output folder, which is
        # the exact thing this staging step exists to prevent. Refuse
        # instead, exactly as make_batch_workspace refuses a location it
        # cannot hide; the finished file stays in the workspace and the job
        # is reported as a publish error rather than silently exposing a
        # half-written file.
        if not _set_hidden_attribute(tmp):
            os.close(fd)
            try:
                tmp.unlink()
            except OSError:
                pass
            raise PublishError(
                f"Refusing to stage a cross-volume publish next to {dest}: the "
                f"staging file could not be hidden, and a visible partial must "
                f"never appear in the output directory"
            )
        try:
            with os.fdopen(fd, "wb") as out_fh, open(str(src), "rb") as in_fh:
                while True:
                    if _cancelled():
                        raise PublishCancelled("Cancelled during cross-volume publish")
                    chunk = in_fh.read(cls._PUBLISH_COPY_CHUNK)
                    if not chunk:
                        break
                    out_fh.write(chunk)
            shutil.copystat(str(src), str(tmp))
            if _cancelled():
                raise PublishCancelled("Cancelled before publish")
            if not _may_commit():
                raise PublishCancelled("Paused before publish")
            # The claim is held from here on; anything that stops short of
            # the rename must hand it back, or the job is stuck as
            # "terminal" without ever having been published.
            try:
                # Unhide only now: the content is complete, and the very next
                # step renames it onto the visible destination name. Also NOT
                # best-effort: os.replace carries the source's attributes
                # across, so renaming a still-hidden temp publishes the
                # user's finished track as a hidden file they cannot find in
                # their own folder. Failing here leaves the destination
                # untouched and the file safely in the workspace, which is
                # recoverable; publishing an invisible file is not.
                if not _set_hidden_attribute(tmp, hidden=False):
                    raise PublishError(
                        f"Refusing to publish {dest}: the staging file's Hidden "
                        f"attribute could not be cleared, and os.replace would "
                        f"carry it onto the published file"
                    )
                os.replace(str(tmp), str(dest))
            except (OSError, PublishError):
                _undo_commit_claim()
                raise
        except (OSError, PublishCancelled, PublishError):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
        else:
            # Copy+rename succeeded — remove the now-published workspace source.
            try:
                src.unlink()
            except OSError:
                pass

    def _run_final_pipeline(self, req: DownloadRequest, final_path: str) -> list[str]:
        """
        Execute all custom post-processing steps sequentially.
        Called after yt-dlp has completely finished.
        Returns a list of non-fatal error messages.
        """
        # 0. Stability delay to ensure file system is ready (mitigates ffprobe locking)
        time.sleep(1.5)

        if not os.path.exists(final_path):
            logger.warning(f"[Downloader] Final path does not exist, skipping pipeline: {final_path}")
            return [f"Output file missing: {Path(final_path).name}"]

        logger.info(f"[Downloader] Starting final post-processing for: {Path(final_path).name}")
        failures: list[str] = []

        # 1. Custom Thumbnail Embedding & Cropping
        if req.thumbnail_url:
            should_crop = False
            should_pad = False
            is_audio = req.media_type == MediaType.AUDIO
            is_video = req.media_type == MediaType.VIDEO

            if req.square_thumbnails and is_audio:
                platform_needs_crop = req.platform in (SourcePlatform.YOUTUBE, SourcePlatform.GENERIC)
                if platform_needs_crop:
                    should_crop = True
                    
            if req.expand_thumbnails and is_video:
                should_pad = True

            logger.debug(f"[Downloader] Embedding custom thumbnail (crop={should_crop}, pad={should_pad})...")
            try:
                from core.thumbnail_cropper import embed_custom_thumbnail
                ok = embed_custom_thumbnail(final_path, req.thumbnail_url, crop=should_crop, pad=should_pad)
                if ok:
                    logger.debug(f"[Downloader] Custom thumbnail embedded successfully.")
                else:
                    logger.warning(f"[Downloader] Failed to embed custom thumbnail.")
                    failures.append("thumbnail embed")
            except Exception as exc:
                logger.error(f"[Downloader] Thumbnail error: {exc}", exc_info=True)
                failures.append(f"thumbnail: {exc}")
        elif (req.square_thumbnails and req.media_type == MediaType.AUDIO) or (req.expand_thumbnails and req.media_type == MediaType.VIDEO):
            should_pad = req.expand_thumbnails and req.media_type == MediaType.VIDEO
            try:
                from core.thumbnail_cropper import crop_embedded_thumbnail
                action = "Padding" if should_pad else "Cropping"
                logger.debug(f"[Downloader] {action} embedded yt-dlp thumbnail...")
                ok = crop_embedded_thumbnail(final_path, pad=should_pad)
                if ok:
                    logger.debug(f"[Downloader] Embedded thumbnail {action.lower()} successfully.")
                else:
                    logger.warning(f"[Downloader] Failed to process embedded thumbnail.")
                    failures.append("thumbnail process")
            except Exception as exc:
                logger.error(f"[Downloader] Thumbnail process error: {exc}", exc_info=True)
                failures.append(f"thumbnail process: {exc}")

        # 2. MusicBrainz enrichment
        if req.musicbrainz:
            try:
                logger.debug(f"[Downloader] Fetching MusicBrainz metadata...")
                from core.musicbrainz_enricher import enrich_file
                enrich_file(final_path, title=req.forced_title or "", artist=req.forced_artist or "",
                            album=req.forced_album or "", duration_s=req.forced_duration)
                logger.debug("[Downloader] MusicBrainz metadata enriched.")
            except Exception as exc:
                logger.error(f"[Downloader] MusicBrainz error: {exc}")
                failures.append(f"MusicBrainz: {exc}")

        # 3. Lyrics embedding
        if req.embed_lyrics:
            try:
                logger.debug(f"[Downloader] Fetching lyrics...")
                from core.lyrics_embedder import embed_lyrics
                embed_lyrics(final_path, title=req.forced_title, artist=req.forced_artist)
                logger.debug("[Downloader] Lyrics embedded.")
            except Exception as exc:
                logger.error(f"[Downloader] Lyrics error: {exc}")
                failures.append(f"lyrics: {exc}")

        # 4. ReplayGain analysis
        if req.replay_gain:
            try:
                logger.debug(f"[Downloader] Analyzing ReplayGain...")
                from core.replay_gain import analyse_and_embed
                analyse_and_embed(final_path)
                logger.debug("[Downloader] ReplayGain added.")
            except Exception as exc:
                logger.error(f"[Downloader] ReplayGain error: {exc}")
                failures.append(f"ReplayGain: {exc}")

        # 5. Authoritative track/disc position — LAST, so that no earlier step
        #    (artwork rewrite, MusicBrainz, lyrics, ReplayGain) can drop it.
        try:
            _stamp_authoritative_position(req, final_path)
        except Exception as exc:
            logger.error(f"[Downloader] Track position error: {exc}", exc_info=True)
            failures.append(f"track number: {exc}")

        logger.info(f"[Downloader] Post-processing finished for: {Path(final_path).name}")
        return failures


    # ── Signal dispatcher ──────────────────────────────────────────────────────

    @staticmethod
    def _fire(
        req:      DownloadRequest,
        progress: DownloadProgress,
        error:    bool = False,
    ) -> None:
        if error and req.on_error:
            try:
                req.on_error(progress)
            except Exception as exc:
                logger.warning("[Downloader] on_error callback raised: %s", exc, exc_info=True)
        elif progress.status == DownloadStatus.FINISHED and req.on_finished:
            try:
                req.on_finished(progress)
            except Exception as exc:
                logger.warning("[Downloader] on_finished callback raised: %s", exc, exc_info=True)
        elif req.on_progress:
            try:
                req.on_progress(progress)
            except Exception as exc:
                logger.warning("[Downloader] on_progress callback raised: %s", exc, exc_info=True)
