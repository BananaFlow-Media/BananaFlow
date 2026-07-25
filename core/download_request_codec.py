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
    "forced_index", "forced_duration",
    "playlist_name", "thumbnail_url",
    "sponsorblock", "sponsorblock_categories",
    "embed_lyrics", "replay_gain", "musicbrainz",
    "square_thumbnails", "expand_thumbnails",
    "clean_filename", "is_solo",
    "youtube_reliability_mode", "stream_type", "category",
)


def request_to_dict(req: DownloadRequest) -> dict[str, Any]:
    """Serialise the resumable subset of a DownloadRequest to a JSON-safe
    dict. Live/transient fields (cancel_event, callbacks, url_resolver, the
    output-path trackers) are deliberately excluded — a restored request
    resumes from its .part file and re-acquires them fresh.

    ``had_pending_resolver`` records whether ``req.url_resolver`` was still
    set (a Spotify two-stage match that never ran before this request was
    paused/persisted) — the resolver itself is a live closure and cannot be
    serialised, but the caller needs to know whether ``url`` here is a real,
    downloadable URL or still just a placeholder waiting on that match, so
    it knows to rebuild an equivalent resolver on restore (see
    ui.controllers.download_controller._build_spotify_resolver). A job can
    only ever reach this function fully resolved OR never-started — never
    mid-resolve — since DownloadOrchestrator.live_request_snapshot refuses
    to hand out a snapshot while a resolve is actually in flight."""
    data: dict[str, Any] = {name: getattr(req, name) for name in _PLAIN_FIELDS}
    data["media_type"] = req.media_type.value
    data["audio_quality"] = req.audio_quality.value
    data["video_quality"] = req.video_quality.value
    data["platform"] = req.platform.value if isinstance(req.platform, SourcePlatform) else None
    data["had_pending_resolver"] = req.url_resolver is not None
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
    return DownloadRequest(**kwargs)
