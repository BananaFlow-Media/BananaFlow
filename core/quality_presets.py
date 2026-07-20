"""
core/quality_presets.py - Single source of truth for download quality choices.

The desktop UI, CLI, config migration, and downloader all consume these
presets.  Stable IDs are persisted; translated display text is never used as
logic or storage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from core.media_formats import AUDIO_FORMATS, DEFAULT_AUDIO_FORMAT

logger = logging.getLogger(__name__)


class AudioQuality(Enum):
    MP3_320 = "audio_mp3_320"
    MP3_256 = "audio_mp3_256"
    MP3_192 = "audio_mp3_192"
    MP3_128 = "audio_mp3_128"
    M4A_BEST = "audio_m4a_best"
    M4A_256 = "audio_m4a_256"
    M4A_192 = "audio_m4a_192"
    M4A_128 = "audio_m4a_128"
    OPUS_BEST = "audio_opus_best"
    OPUS_192 = "audio_opus_192"
    OPUS_160 = "audio_opus_160"
    OPUS_128 = "audio_opus_128"
    OPUS_96 = "audio_opus_96"
    FLAC_SOURCE = "audio_flac_source"


class VideoQuality(Enum):
    BEST = "video_best"
    P2160 = "video_2160"
    P1440 = "video_1440"
    P1080 = "video_1080"
    P720 = "video_720"
    P480 = "video_480"
    P360 = "video_360"
    SMALLEST = "video_smallest"


@dataclass(frozen=True)
class AudioQualityPreset:
    id: str
    quality: AudioQuality
    codec: str
    label_key: str
    detail_key: str | None
    preferredquality: str | None
    tooltip_key: str = "quality_tooltip_audio_bitrate"


@dataclass(frozen=True)
class VideoQualityPreset:
    id: str
    quality: VideoQuality
    label_key: str
    detail_key: str | None
    format_selector: str
    format_sort: tuple[str, ...] = ()
    tooltip_key: str | None = None


DEFAULT_AUDIO_QUALITY_ID = AudioQuality.MP3_320.value
DEFAULT_VIDEO_QUALITY_ID = VideoQuality.P1080.value

_DEFAULT_AUDIO_BY_CODEC: dict[str, str] = {
    "mp3": AudioQuality.MP3_320.value,
    "m4a": AudioQuality.M4A_BEST.value,
    "opus": AudioQuality.OPUS_BEST.value,
    "flac": AudioQuality.FLAC_SOURCE.value,
}


AUDIO_QUALITY_PRESETS: tuple[AudioQualityPreset, ...] = (
    AudioQualityPreset(
        id=AudioQuality.MP3_320.value,
        quality=AudioQuality.MP3_320,
        codec="mp3",
        label_key="quality_best",
        detail_key="quality_audio_320",
        preferredquality="320",
    ),
    AudioQualityPreset(
        id=AudioQuality.MP3_256.value,
        quality=AudioQuality.MP3_256,
        codec="mp3",
        label_key="quality_high",
        detail_key="quality_audio_256",
        preferredquality="256",
    ),
    AudioQualityPreset(
        id=AudioQuality.MP3_192.value,
        quality=AudioQuality.MP3_192,
        codec="mp3",
        label_key="quality_balanced",
        detail_key="quality_audio_192",
        preferredquality="192",
    ),
    AudioQualityPreset(
        id=AudioQuality.MP3_128.value,
        quality=AudioQuality.MP3_128,
        codec="mp3",
        label_key="quality_economical",
        detail_key="quality_audio_128",
        preferredquality="128",
    ),
    AudioQualityPreset(
        id=AudioQuality.M4A_BEST.value,
        quality=AudioQuality.M4A_BEST,
        codec="m4a",
        label_key="quality_best_available",
        detail_key=None,
        preferredquality="0",
    ),
    AudioQualityPreset(
        id=AudioQuality.M4A_256.value,
        quality=AudioQuality.M4A_256,
        codec="m4a",
        label_key="quality_high",
        detail_key="quality_audio_256",
        preferredquality="256",
    ),
    AudioQualityPreset(
        id=AudioQuality.M4A_192.value,
        quality=AudioQuality.M4A_192,
        codec="m4a",
        label_key="quality_balanced",
        detail_key="quality_audio_192",
        preferredquality="192",
    ),
    AudioQualityPreset(
        id=AudioQuality.M4A_128.value,
        quality=AudioQuality.M4A_128,
        codec="m4a",
        label_key="quality_economical",
        detail_key="quality_audio_128",
        preferredquality="128",
    ),
    AudioQualityPreset(
        id=AudioQuality.OPUS_BEST.value,
        quality=AudioQuality.OPUS_BEST,
        codec="opus",
        label_key="quality_best_available",
        detail_key=None,
        preferredquality="256",
    ),
    AudioQualityPreset(
        id=AudioQuality.OPUS_192.value,
        quality=AudioQuality.OPUS_192,
        codec="opus",
        label_key="quality_high",
        detail_key="quality_audio_192",
        preferredquality="192",
    ),
    AudioQualityPreset(
        id=AudioQuality.OPUS_160.value,
        quality=AudioQuality.OPUS_160,
        codec="opus",
        label_key="quality_balanced",
        detail_key="quality_audio_160",
        preferredquality="160",
    ),
    AudioQualityPreset(
        id=AudioQuality.OPUS_128.value,
        quality=AudioQuality.OPUS_128,
        codec="opus",
        label_key="quality_economical",
        detail_key="quality_audio_128",
        preferredquality="128",
    ),
    AudioQualityPreset(
        id=AudioQuality.OPUS_96.value,
        quality=AudioQuality.OPUS_96,
        codec="opus",
        label_key="quality_small_file",
        detail_key="quality_audio_96",
        preferredquality="96",
    ),
    AudioQualityPreset(
        id=AudioQuality.FLAC_SOURCE.value,
        quality=AudioQuality.FLAC_SOURCE,
        codec="flac",
        label_key="quality_source_quality",
        detail_key="quality_no_additional_lossy_compression",
        preferredquality=None,
        tooltip_key="quality_tooltip_flac",
    ),
)


VIDEO_QUALITY_PRESETS: tuple[VideoQualityPreset, ...] = (
    VideoQualityPreset(
        id=VideoQuality.BEST.value,
        quality=VideoQuality.BEST,
        label_key="quality_auto",
        detail_key="quality_best_available",
        format_selector="bestvideo+bestaudio/best",
        tooltip_key="quality_tooltip_video_auto",
    ),
    VideoQualityPreset(
        id=VideoQuality.P2160.value,
        quality=VideoQuality.P2160,
        label_key="quality_video_4k",
        detail_key="quality_video_2160",
        format_selector="bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    ),
    VideoQualityPreset(
        id=VideoQuality.P1440.value,
        quality=VideoQuality.P1440,
        label_key="quality_video_2k",
        detail_key="quality_video_1440",
        format_selector="bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    ),
    VideoQualityPreset(
        id=VideoQuality.P1080.value,
        quality=VideoQuality.P1080,
        label_key="quality_video_full_hd",
        detail_key="quality_video_1080",
        format_selector="bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    ),
    VideoQualityPreset(
        id=VideoQuality.P720.value,
        quality=VideoQuality.P720,
        label_key="quality_video_hd",
        detail_key="quality_video_720",
        format_selector="bestvideo[height<=720]+bestaudio/best[height<=720]",
    ),
    VideoQualityPreset(
        id=VideoQuality.P480.value,
        quality=VideoQuality.P480,
        label_key="quality_video_sd",
        detail_key="quality_video_480",
        format_selector="bestvideo[height<=480]+bestaudio/best[height<=480]",
    ),
    VideoQualityPreset(
        id=VideoQuality.P360.value,
        quality=VideoQuality.P360,
        label_key="quality_economical",
        detail_key="quality_video_360",
        format_selector="bestvideo[height<=360]+bestaudio/best[height<=360]",
    ),
    VideoQualityPreset(
        id=VideoQuality.SMALLEST.value,
        quality=VideoQuality.SMALLEST,
        label_key="quality_smallest_file",
        detail_key=None,
        format_selector="best",
        format_sort=("+size", "+br", "+res", "+fps"),
    ),
)


AUDIO_PRESETS_BY_ID = {preset.id: preset for preset in AUDIO_QUALITY_PRESETS}
AUDIO_PRESETS_BY_QUALITY = {preset.quality: preset for preset in AUDIO_QUALITY_PRESETS}
VIDEO_PRESETS_BY_ID = {preset.id: preset for preset in VIDEO_QUALITY_PRESETS}
VIDEO_PRESETS_BY_QUALITY = {preset.quality: preset for preset in VIDEO_QUALITY_PRESETS}


def audio_presets_for_codec(codec: str) -> tuple[AudioQualityPreset, ...]:
    codec = _normalise_codec(codec)
    return tuple(preset for preset in AUDIO_QUALITY_PRESETS if preset.codec == codec)


def default_audio_quality_id_for_codec(codec: str) -> str:
    return _DEFAULT_AUDIO_BY_CODEC.get(_normalise_codec(codec), DEFAULT_AUDIO_QUALITY_ID)


def audio_preset_from_id(preset_id: str, codec: str | None = None) -> AudioQualityPreset | None:
    preset = AUDIO_PRESETS_BY_ID.get(str(preset_id))
    if preset is None:
        return None
    if codec is not None and preset.codec != _normalise_codec(codec):
        return None
    return preset


def video_preset_from_id(preset_id: str) -> VideoQualityPreset | None:
    return VIDEO_PRESETS_BY_ID.get(str(preset_id))


def audio_preset_for_quality(quality: AudioQuality) -> AudioQualityPreset:
    return AUDIO_PRESETS_BY_QUALITY[quality]


def video_preset_for_quality(quality: VideoQuality) -> VideoQualityPreset:
    return VIDEO_PRESETS_BY_QUALITY[quality]


def audio_quality_from_id(preset_id: str, codec: str) -> AudioQuality | None:
    preset = audio_preset_from_id(preset_id, codec)
    return preset.quality if preset else None


def video_quality_from_id(preset_id: str) -> VideoQuality | None:
    preset = video_preset_from_id(preset_id)
    return preset.quality if preset else None


def coerce_audio_quality_id(preset_id: str, codec: str) -> str:
    preset = audio_preset_from_id(preset_id, codec)
    if preset is not None:
        return preset.id
    fallback = default_audio_quality_id_for_codec(codec)
    logger.debug(
        "[QualityPresets] Invalid audio quality %r for codec %r; using %s",
        preset_id,
        codec,
        fallback,
    )
    return fallback


def coerce_video_quality_id(preset_id: str) -> str:
    preset = video_preset_from_id(preset_id)
    if preset is not None:
        return preset.id
    logger.debug(
        "[QualityPresets] Invalid video quality %r; using %s",
        preset_id,
        DEFAULT_VIDEO_QUALITY_ID,
    )
    return DEFAULT_VIDEO_QUALITY_ID


def migrate_audio_quality_value(value: object, codec: str = "mp3") -> str:
    codec = _normalise_codec(codec)
    value_text = _clean_legacy_text(value)
    if value_text in AUDIO_PRESETS_BY_ID:
        preset = AUDIO_PRESETS_BY_ID[value_text]
        if preset.codec == codec:
            return preset.id
        logger.debug(
            "[QualityPresets] Audio preset %s does not apply to codec %s; using codec default",
            value_text,
            codec,
        )
        return default_audio_quality_id_for_codec(codec)

    enum_name_map = {
        "BEST": "best",
        "HIGH": "high",
        "MEDIUM": "balanced",
        "LOW": "compact",
    }
    rank = enum_name_map.get(value_text.upper())
    if rank:
        return _audio_id_for_rank(codec, rank)

    normalised = _normalise_alias_text(value_text)
    if not normalised:
        return default_audio_quality_id_for_codec(codec)

    if "320" in normalised:
        return _audio_id_for_rank(codec, "best")
    if "256" in normalised:
        return _audio_id_for_rank(codec, "high")
    if "192" in normalised:
        return _audio_id_for_rank(codec, "balanced")
    if "160" in normalised and codec == "opus":
        return AudioQuality.OPUS_160.value
    if "128" in normalised:
        return _audio_id_for_rank(codec, "compact")
    if "96" in normalised and codec == "opus":
        return AudioQuality.OPUS_96.value
    if "flac" in normalised or "source" in normalised:
        return AudioQuality.FLAC_SOURCE.value if codec == "flac" else default_audio_quality_id_for_codec(codec)
    if "best" in normalised:
        return _audio_id_for_rank(codec, "best")
    if "high" in normalised:
        return _audio_id_for_rank(codec, "high")
    if "medium" in normalised or "balanced" in normalised:
        return _audio_id_for_rank(codec, "balanced")
    if "low" in normalised or "compact" in normalised:
        return _audio_id_for_rank(codec, "compact")

    logger.debug(
        "[QualityPresets] Unknown legacy audio quality %r for codec %s; using codec default",
        value,
        codec,
    )
    return default_audio_quality_id_for_codec(codec)


def migrate_video_quality_value(value: object) -> str:
    value_text = _clean_legacy_text(value)
    if value_text in VIDEO_PRESETS_BY_ID:
        return value_text

    enum_name_map = {
        "BEST": VideoQuality.BEST.value,
        "UHD_4K": VideoQuality.P2160.value,
        "QHD_2K": VideoQuality.P1440.value,
        "HIGH": VideoQuality.P1080.value,
        "MEDIUM": VideoQuality.P720.value,
        "LOW": VideoQuality.P480.value,
        "WORST": VideoQuality.SMALLEST.value,
    }
    if value_text.upper() in enum_name_map:
        return enum_name_map[value_text.upper()]

    normalised = _normalise_alias_text(value_text)
    if "2160" in normalised:
        return VideoQuality.P2160.value
    if "1440" in normalised:
        return VideoQuality.P1440.value
    if "1080" in normalised:
        return VideoQuality.P1080.value
    if "720" in normalised:
        return VideoQuality.P720.value
    if "480" in normalised:
        return VideoQuality.P480.value
    if "360" in normalised:
        return VideoQuality.P360.value
    if "worst" in normalised or "smallest" in normalised:
        return VideoQuality.SMALLEST.value
    # Historical UI value "Best" meant the old 2160p cap.  The new
    # unrestricted auto best is only selected by the explicit video_best ID
    # or CLI aliases handled outside config migration.
    if normalised == "best" or normalised.startswith("best "):
        return VideoQuality.P2160.value

    logger.debug(
        "[QualityPresets] Unknown legacy video quality %r; using default %s",
        value,
        DEFAULT_VIDEO_QUALITY_ID,
    )
    return DEFAULT_VIDEO_QUALITY_ID


def parse_audio_quality_for_cli(value: object, codec: str) -> AudioQuality:
    return AUDIO_PRESETS_BY_ID[_parse_audio_id_for_cli(value, codec)].quality


def parse_video_quality_for_cli(value: object) -> VideoQuality:
    return VIDEO_PRESETS_BY_ID[_parse_video_id_for_cli(value)].quality


def _parse_audio_id_for_cli(value: object, codec: str) -> str:
    value_text = _clean_legacy_text(value)
    if value_text in AUDIO_PRESETS_BY_ID:
        preset = AUDIO_PRESETS_BY_ID[value_text]
        if preset.codec == _normalise_codec(codec):
            return preset.id
    return migrate_audio_quality_value(value, codec)


def _parse_video_id_for_cli(value: object) -> str:
    value_text = _clean_legacy_text(value)
    if value_text in VIDEO_PRESETS_BY_ID:
        return value_text
    normalised = _normalise_alias_text(value_text)
    if normalised in {"auto", "auto best", "best available", "unrestricted", "video best"}:
        return VideoQuality.BEST.value
    return migrate_video_quality_value(value)


def valid_audio_quality_ids() -> set[str]:
    return set(AUDIO_PRESETS_BY_ID)


def valid_video_quality_ids() -> set[str]:
    return set(VIDEO_PRESETS_BY_ID)


def _audio_id_for_rank(codec: str, rank: str) -> str:
    codec = _normalise_codec(codec)
    if codec == "flac":
        return AudioQuality.FLAC_SOURCE.value
    table = {
        "mp3": {
            "best": AudioQuality.MP3_320.value,
            "high": AudioQuality.MP3_256.value,
            "balanced": AudioQuality.MP3_192.value,
            "compact": AudioQuality.MP3_128.value,
        },
        "m4a": {
            "best": AudioQuality.M4A_BEST.value,
            "high": AudioQuality.M4A_256.value,
            "balanced": AudioQuality.M4A_192.value,
            "compact": AudioQuality.M4A_128.value,
        },
        "opus": {
            "best": AudioQuality.OPUS_BEST.value,
            "high": AudioQuality.OPUS_192.value,
            "balanced": AudioQuality.OPUS_160.value,
            "compact": AudioQuality.OPUS_128.value,
        },
    }
    return table.get(codec, table["mp3"]).get(rank, default_audio_quality_id_for_codec(codec))


def _normalise_codec(codec: str) -> str:
    codec = str(codec or DEFAULT_AUDIO_FORMAT).lower().strip()
    return codec if codec in AUDIO_FORMATS else DEFAULT_AUDIO_FORMAT


def _clean_legacy_text(value: object) -> str:
    return str(value or "").strip().translate({
        ord("\u2066"): None,
        ord("\u2067"): None,
        ord("\u2068"): None,
        ord("\u2069"): None,
        ord("\u200e"): None,
        ord("\u200f"): None,
    })


def _normalise_alias_text(value: str) -> str:
    text = value.lower().strip()
    replacements: Iterable[tuple[str, str]] = (
        ("kbps", "k"),
        ("kb/s", "k"),
        ("(", " "),
        (")", " "),
        ("[", " "),
        ("]", " "),
        ("·", " "),
        ("•", " "),
        ("-", " "),
        ("_", " "),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()
