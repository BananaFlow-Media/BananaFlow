"""
core/media_formats.py – Supported audio/video output formats
================================================================
Single source of truth for which file formats this app can currently
produce. Consumed by config.py, config_migrate.py, core/downloader.py,
core/quality_presets.py, ui/panels/options_bar.py, and cli.py.

Adding a new video format later starts here, but a tuple entry alone does
not make it real: it still needs yt-dlp/ffmpeg postprocessor wiring in
core/downloader.py, a decision on how quality presets apply to it, UI
exposure, and test coverage before it's actually supported.
"""

from __future__ import annotations

AUDIO_FORMATS: tuple[str, ...] = ("mp3", "m4a", "flac", "opus")
VIDEO_FORMATS: tuple[str, ...] = ("mp4",)

DEFAULT_AUDIO_FORMAT: str = "mp3"
DEFAULT_VIDEO_FORMAT: str = "mp4"


def is_valid_audio_format(fmt: str) -> bool:
    return str(fmt or "").strip().lower() in AUDIO_FORMATS


def is_valid_video_format(fmt: str) -> bool:
    return str(fmt or "").strip().lower() in VIDEO_FORMATS
