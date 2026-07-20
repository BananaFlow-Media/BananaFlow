"""
config_migrate.py  –  Schema migration for ~/.bananaflow/config.json
=================================================================
Each time the config schema changes in a breaking way (key renamed,
type changed, value meaning redefined), add a new migration function
to the ``_MIGRATIONS`` list below.

The ``migrate()`` function is called by ``AppConfig._load()`` after
reading the raw JSON.  It inspects ``config_version``, runs any
pending migrations in order, bumps the version, and returns the
updated dict — which AppConfig then saves back to disk.

Rules
-----
* Additive keys (new key with a default) do NOT need a migration —
  AppConfig already merges missing keys from _DEFAULTS.
* Only breaking changes need a migration: renamed keys, changed
  value semantics, removed keys, type changes.
* Each migration takes a ``dict`` and mutates it in place.
* Migrations are numbered sequentially starting at 1.
* ``config_version`` 0 (or absent) means "original schema, no
  migrations ever applied".

Zero GUI imports.  Zero side effects beyond mutating the passed dict.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Callable

from core.quality_presets import (
    DEFAULT_VIDEO_QUALITY_ID,
    audio_preset_from_id,
    default_audio_quality_id_for_codec,
    migrate_audio_quality_value,
    migrate_video_quality_value,
    valid_audio_quality_ids,
)

logger = logging.getLogger(__name__)

# Current schema version — bump this when adding a new migration.
CURRENT_VERSION: int = 9

_RETIRED_SPOTIFY_DEFAULT_SHA256 = (
    "253081b21dd3bfb76e467c5811e5084a4bdd47c3dba9cabdcb2600cda6b27da9"  # pragma: allowlist secret
)


def _remove_retired_spotify_default(data: dict) -> None:
    """Clear only the credential that older releases copied from defaults.

    The retired value itself is intentionally not retained in source.  Custom
    user values remain untouched, preserving existing configuration behavior.
    """
    value = data.get("spotify_app_api_key")
    if not isinstance(value, str) or not value:
        return
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if hmac.compare_digest(digest, _RETIRED_SPOTIFY_DEFAULT_SHA256):
        data["spotify_app_api_key"] = ""


def _normalise_quality_labels(data: dict) -> None:
    audio_map = {
        "Best (320k)": "Best · 320k",
        "High (256k)": "High · 256k",
        "Medium (192k)": "Medium · 192k",
        "Low (128k)": "Low · 128k",
        "320k": "Best · 320k",
        "256k": "High · 256k",
        "192k": "Medium · 192k",
        "128k": "Low · 128k",
    }
    video_map = {
        "Best": "Best · 2160p",
        "2160p (4K)": "Best · 2160p",
        "1440p (2K)": "1440p · 2K",
    }
    audio_quality = data.get("audio_quality")
    if audio_quality in audio_map:
        data["audio_quality"] = audio_map[audio_quality]
    video_quality = data.get("video_quality")
    if video_quality in video_map:
        data["video_quality"] = video_map[video_quality]


def _quality_labels_to_stable_keys(data: dict) -> None:
    """Replace the display-string quality values (any historical format —
    parenthesis style, dot-separator style, or already-current) with the
    stable AudioQuality/VideoQuality enum-name keys that
    ui/panels/options_bar.py now stores. Needed because the display text is
    now translated per-language, so it can no longer double as the
    persisted/matched value (see ui/panels/options_bar.py's quality combo
    and ui/controllers/download_controller.py)."""
    audio_map = {
        "Best (320k)": "BEST", "320k": "BEST", "Best · 320k": "BEST",
        "High (256k)": "HIGH", "256k": "HIGH", "High · 256k": "HIGH",
        "Medium (192k)": "MEDIUM", "192k": "MEDIUM", "Medium · 192k": "MEDIUM",
        "Low (128k)": "LOW", "128k": "LOW", "Low · 128k": "LOW",
    }
    video_map = {
        "Best": "UHD_4K", "2160p (4K)": "UHD_4K", "Best · 2160p": "UHD_4K",
        "1440p (2K)": "QHD_2K", "1440p · 2K": "QHD_2K",
        "1080p": "HIGH",
        "720p": "MEDIUM",
        "480p": "LOW",
        "Worst": "WORST",
    }
    audio_quality = data.get("audio_quality")
    if audio_quality in audio_map:
        data["audio_quality"] = audio_map[audio_quality]
    video_quality = data.get("video_quality")
    if video_quality in video_map:
        data["video_quality"] = video_map[video_quality]


def _quality_values_to_preset_ids(data: dict) -> None:
    """Migrate every historical quality representation to current preset IDs.

    Earlier builds persisted visible labels ("Best · 320k") and a short-lived
    pre-release build persisted enum names ("BEST", "HIGH").  Current builds
    persist stable preset IDs such as "audio_mp3_320" and "video_1080".
    """
    codec = str(data.get("audio_format") or "mp3").lower().strip()
    if codec not in {"mp3", "m4a", "opus", "flac"}:
        codec = "mp3"

    audio_quality = migrate_audio_quality_value(data.get("audio_quality"), codec)
    video_quality = migrate_video_quality_value(data.get("video_quality"))

    if audio_quality != data.get("audio_quality"):
        logger.debug(
            "[ConfigMigrate] audio_quality migrated to preset id %s for codec %s",
            audio_quality,
            codec,
        )
    if video_quality != data.get("video_quality"):
        logger.debug(
            "[ConfigMigrate] video_quality migrated to preset id %s",
            video_quality,
        )

    data["audio_quality"] = audio_quality
    data["video_quality"] = video_quality or DEFAULT_VIDEO_QUALITY_ID

    raw_by_codec = data.get("audio_quality_by_codec")
    by_codec = raw_by_codec if isinstance(raw_by_codec, dict) else {}
    migrated_by_codec: dict[str, str] = {}
    for codec_name in ("mp3", "m4a", "opus", "flac"):
        raw_value = by_codec.get(codec_name)
        if raw_value in valid_audio_quality_ids() and audio_preset_from_id(
            str(raw_value),
            codec_name,
        ):
            migrated_by_codec[codec_name] = str(raw_value)
        elif raw_value is not None:
            migrated_by_codec[codec_name] = migrate_audio_quality_value(
                raw_value,
                codec_name,
            )
        elif codec_name == codec:
            migrated_by_codec[codec_name] = audio_quality
        else:
            migrated_by_codec[codec_name] = default_audio_quality_id_for_codec(codec_name)

    data["audio_quality_by_codec"] = migrated_by_codec


def _media_format_to_media_type(data: dict) -> None:
    """Replace the conflated 'media_format' ('mp3'/'mp4') key with an
    explicit 'media_type' ('audio'/'video').

    'media_format' mixed the media-type switch (audio vs. video mode) with
    the mp3/mp4 file-extension vocabulary, producing UI states like
    Format=MP3 + Codec=FLAC where the real output was FLAC. media_type now
    carries only the mode; audio_format (unchanged) carries the audio
    codec.

    A no-op (like migration 2's key removal) when 'media_format' isn't
    present — e.g. a config that already has 'media_type' from a fresh
    install must not have it clobbered back to a media_format-derived
    default. The new 'video_format' key is purely additive (see
    core/media_formats.py) and needs no migration at all — AppConfig
    already merges it in from _DEFAULTS per this module's own convention.
    """
    if "media_format" not in data:
        return
    old_media_format = str(data.pop("media_format")).strip().lower()
    data["media_type"] = "video" if old_media_format == "mp4" else "audio"

    data.pop("media_format", None)


# ──────────────────────────────────────────────────────────────────────────────
# Migration registry
# ──────────────────────────────────────────────────────────────────────────────
# Each entry: (target_version, description, callable)
# The callable receives the raw config dict and mutates it.

_MIGRATIONS: list[tuple[int, str, Callable[[dict], None]]] = [

    # ── Migration 1: add config_version itself ────────────────────────────────
    # This is a bootstrap migration.  All configs written before the version
    # system existed have no "config_version" key.  After this migration the
    # key exists and is set to 1.
    (
        1,
        "Bootstrap: add config_version key",
        lambda d: None,   # no-op; version is set by migrate() after running
    ),

    # ── Migration 2: drop unused window_geometry key ──────────────────────────
    (
        2,
        "Drop stale 'window_geometry' config key (never read, replaced by window_state)",
        lambda d: d.pop("window_geometry", None),
    ),

    # ── Migration 3: drop dead settings ───────────────────────────────────────
    # 'randomize_user_agent' was threaded through to yt-dlp opts but the
    # terminal parameter was never used (no UA rotation was ever wired).
    # 'search_max_results' was superseded by the per-platform
    # youtube_max_results / spotify_max_results keys and had no reader.
    (
        3,
        "Drop dead 'randomize_user_agent' and 'search_max_results' config keys",
        lambda d: (
            d.pop("randomize_user_agent", None),
            d.pop("search_max_results", None),
        ),
    ),

    (
        4,
        "Normalize renamed quality labels",
        _normalise_quality_labels,
    ),

    # ── Migration 5: quality display strings -> stable enum-name keys ─────────
    # The quality combo's on-screen text is now translated per-language, so
    # it can no longer double as the persisted/matched value (see
    # ui/panels/options_bar.py, ui/controllers/download_controller.py).
    (
        5,
        "Replace quality display strings with stable AudioQuality/VideoQuality keys",
        _quality_labels_to_stable_keys,
    ),

    # ── Migration 6: stable enum-name keys -> typed preset IDs ─────────────────
    (
        6,
        "Replace quality enum names/labels with typed preset IDs",
        _quality_values_to_preset_ids,
    ),

    # ── Migration 7: media_format ('mp3'/'mp4') -> media_type + video_format ──
    (
        7,
        "Replace 'media_format' with explicit 'media_type' and 'video_format'",
        _media_format_to_media_type,
    ),

    (
        8,
        "Add stable Tag Editor last-folder setting",
        lambda d: (
            d.setdefault("tag_editor_last_folder", ""),
        ),
    ),

    (
        9,
        "Remove the retired committed Spotify proxy credential default",
        _remove_retired_spotify_default,
    ),

    # ── Future migrations go here ─────────────────────────────────────────────
]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def migrate(data: dict) -> bool:
    """
    Apply all pending migrations to ``data`` in place.

    Parameters
    ----------
    data : The raw config dict loaded from JSON.

    Returns
    -------
    bool
        True if any migration was applied (caller should save to disk).
    """
    current = data.get("config_version", 0)
    if not isinstance(current, int):
        current = 0

    if current >= CURRENT_VERSION:
        return False   # already up to date

    applied = False
    for target_ver, description, fn in _MIGRATIONS:
        if target_ver <= current:
            continue
        logger.info(
            "[ConfigMigrate] Applying migration %d: %s", target_ver, description,
        )
        try:
            fn(data)
        except Exception:
            logger.error(
                "[ConfigMigrate] Migration %d failed — skipping",
                target_ver, exc_info=True,
            )
            # Don't bump version past the failed migration
            break
        current = target_ver
        applied = True

    data["config_version"] = current
    if applied:
        logger.info("[ConfigMigrate] Config upgraded to version %d", current)
    return applied


# ──────────────────────────────────────────────────────────────────────────────
# Example future migration function (kept commented for reference)
# ──────────────────────────────────────────────────────────────────────────────
#
# def _migrate_v2(data: dict) -> None:
#     """
#     v2: Normalise audio_quality values.
#     Old: "Best (320k)" → New: "320k"
#     """
#     _MAP = {
#         "Best (320k)":   "320k",
#         "High (256k)":   "256k",
#         "Medium (192k)": "192k",
#         "Low (128k)":    "128k",
#     }
#     old_val = data.get("audio_quality", "")
#     if old_val in _MAP:
#         data["audio_quality"] = _MAP[old_val]
