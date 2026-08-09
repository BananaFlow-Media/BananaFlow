"""
core/download_request_codec.py  –  Persist a resumable DownloadRequest
=========================================================================
A ``DownloadRequest`` carries live, non-serialisable state (a threading
cancel Event, per-run callbacks, a lazy URL resolver). To persist a paused
job across an application restart we keep only the JSON-safe fields needed
to rebuild an equivalent, resumable request — enums are stored by value and
rebuilt on load; the transient run state resets to its defaults.

Zero GUI imports.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.downloader import DownloadRequest, MediaType
from core.playlist_parser import SourcePlatform
from core.quality_presets import (
    AudioQuality,
    VideoQuality,
    audio_quality_from_id,
    video_quality_from_id,
)

logger = logging.getLogger(__name__)

# Plain (already JSON-safe) fields copied verbatim in both directions.
_PLAIN_FIELDS = (
    "url", "output_dir", "workspace_dir",
    "audio_format", "video_format",
    "embed_thumbnail", "embed_metadata", "write_subtitles",
    "playlist_start", "playlist_end",
    "cookies_file", "cookies_browser", "proxy_url",
    "forced_title", "forced_artist", "forced_album",
    "forced_index", "forced_disc", "forced_total", "forced_duration",
    "playlist_name", "thumbnail_url",
    "sponsorblock", "sponsorblock_categories",
    "embed_lyrics", "replay_gain", "musicbrainz",
    "square_thumbnails", "expand_thumbnails",
    "clean_filename", "is_solo",
    "youtube_reliability_mode", "stream_type", "category",
    "source_kind", "source_url",
    "spotify_match_identity",
    # Post-download resume checkpoint (core.downloader.DownloadRequest).
    # A job paused during post-processing or publishing has already
    # finished downloading; without these the restored request re-runs
    # yt-dlp against an already-complete file, no postprocessor hook fires,
    # and the resume dies with "output file is missing". The referenced
    # file lives in the persisted workspace_dir, so it survives the restart
    # alongside it -- and DownloadEngine re-validates that it still exists
    # before honouring the checkpoint.
    "resume_phase", "resume_final_path",
)


def request_to_dict(req: DownloadRequest) -> dict[str, Any]:
    """Serialise the resumable subset of a DownloadRequest to a JSON-safe
    dict. Live/transient fields (cancel_event, callbacks, url_resolver) are
    deliberately excluded — a restored request re-acquires them fresh.

    ``final_output_path`` is the one ``init=False`` field that IS persisted,
    and it has to be. There is a boundary — between yt-dlp producing the
    final workspace file and the post-download checkpoint being written —
    where a pause snapshot carries that tracker and nothing else: no
    ``resume_phase``, no ``resume_final_path``. Leaving it in memory only
    meant such a job resumed correctly inside the running process but came
    back from a restart with nothing to resume from, so it re-ran yt-dlp
    against an already-complete file, fired no postprocessor hook and died
    with "output file is missing". A captured paused job must stay
    resumable across a restart, so the tracker travels with the record. It
    is only ever a hint: the engine re-validates the file before using it,
    and re-runs the download if it has gone.

    ``had_pending_resolver`` records whether ``req.url_resolver`` was still
    set (a Spotify two-stage match that never ran before this request was
    paused/persisted) — the resolver itself is a live closure and cannot be
    serialised, but the caller needs to know whether ``url`` here is a real,
    downloadable URL or still just a placeholder waiting on that match, so
    it knows to rebuild an equivalent resolver on restore (see
    core.spotify_request_builder.build_spotify_resolver). A job can
    only ever reach this function fully resolved OR never-started — never
    mid-resolve — since DownloadOrchestrator.live_request_snapshot refuses
    to hand out a snapshot while a resolve is actually in flight."""
    data: dict[str, Any] = {name: getattr(req, name) for name in _PLAIN_FIELDS}
    data["media_type"] = req.media_type.value
    data["audio_quality"] = req.audio_quality.value
    data["video_quality"] = req.video_quality.value
    data["platform"] = req.platform.value if isinstance(req.platform, SourcePlatform) else None
    data["had_pending_resolver"] = req.url_resolver is not None
    # init=False, so it cannot ride along in _PLAIN_FIELDS and cannot be
    # passed to the constructor on the way back — see request_from_dict.
    data["final_output_path"] = req._final_output_path  # noqa: SLF001
    # A restored job always continues from its partial download.
    data["resumable"] = True
    return data


def request_from_dict(data: dict[str, Any]) -> DownloadRequest:
    """Rebuild a resumable DownloadRequest from :func:`request_to_dict`
    output. Tolerant of missing keys (falls back to DownloadRequest's own
    defaults) and of unknown enum values (falls back to safe defaults) so a
    partially-written or version-skewed record never raises."""
    kwargs: dict[str, Any] = {}
    for name in _PLAIN_FIELDS:
        if name in data:
            kwargs[name] = data[name]
    # url and output_dir are required positionals — default them so a
    # truncated/partial record can never raise on construction (the caller
    # validates usability, e.g. that the workspace still exists).
    kwargs.setdefault("url", data.get("url", "") or "")
    kwargs.setdefault("output_dir", data.get("output_dir", "") or "")

    try:
        kwargs["media_type"] = MediaType(data.get("media_type", "audio"))
    except ValueError:
        kwargs["media_type"] = MediaType.AUDIO

    audio_codec = data.get("audio_format") or "mp3"
    aq = audio_quality_from_id(data.get("audio_quality", ""), audio_codec)
    kwargs["audio_quality"] = aq or AudioQuality.MP3_320

    vq = video_quality_from_id(data.get("video_quality", ""))
    kwargs["video_quality"] = vq or VideoQuality.P1080

    platform_val = data.get("platform")
    if platform_val:
        try:
            kwargs["platform"] = SourcePlatform(platform_val)
        except ValueError:
            kwargs["platform"] = None

    kwargs["resumable"] = True
    req = DownloadRequest(**kwargs)

    # Assigned after construction: _final_output_path is init=False, so the
    # constructor will not take it. Restoring it is what keeps a job paused
    # in the post-download boundary resumable across a restart — see
    # request_to_dict. A record that predates this field, or one that was
    # paused before yt-dlp produced anything, simply keeps the default "".
    final_output_path = data.get("final_output_path")
    if isinstance(final_output_path, str) and final_output_path:
        req._final_output_path = final_output_path  # noqa: SLF001

    return req
