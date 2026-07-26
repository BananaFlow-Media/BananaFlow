"""
config.py  –  Persistent user-preferences manager  (v3.1)
==========================================================================
Backward-compatible additive update: every v1/v2 key is preserved.
New keys for Phase 2 features are purely additive.
Includes politeness defaults (request throttling) so the client stays
well within typical service rate limits.

New fields (Phase 2 & rate-limit handling)
--------------------
  accent_color             – custom accent hex (theme_manager v3)
  sponsorblock_enabled     – SponsorBlock segment removal
  lyrics_enabled           – auto-fetch+embed lyrics (Advanced Setting, default False)
  replay_gain_enabled      – post-download ReplayGain analysis (Advanced, default False)
  square_thumbnails        – crop 16:9 art to 1:1 before embed (Advanced, default False)
  musicbrainz_enabled      – enrich tags from MusicBrainz (default True)
  playlist_subfolders      – organise playlist downloads into named subfolders
  playlist_index_prefix    – prefix filenames with 01, 02, … track index
  duplicate_action         – "skip" | "warn" | "overwrite" on detected duplicate
  accessibility_mode       – high-contrast / keyboard-nav mode
  tray_on_close            – minimise to system tray instead of quitting
  global_hotkeys_enabled   – register OS-level pause/open hotkeys
  queue_state              – serialised queue for smart auto-resume
  download_delay_range     – Min/Max seconds between requests (rate-limit politeness)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config_migrate import CURRENT_VERSION, migrate as _run_migrations
from core.media_formats import DEFAULT_VIDEO_FORMAT, VIDEO_FORMATS
from core.quality_presets import (
    DEFAULT_AUDIO_QUALITY_ID,
    DEFAULT_VIDEO_QUALITY_ID,
    coerce_audio_quality_id,
    coerce_video_quality_id,
    default_audio_quality_id_for_codec,
    valid_audio_quality_ids,
)


def _detect_system_language() -> str:
    """Detect the system language and default to Hebrew ('he') if found, else English ('en')."""
    try:
        import locale
        lang, _ = locale.getlocale()
        if lang and lang.lower().startswith(("he", "heb")):
            return "he"
    except Exception:
        pass
    try:
        import locale
        lang, _ = locale.getdefaultlocale()
        if lang and lang.lower().startswith(("he", "heb")):
            return "he"
    except Exception:
        pass
    try:
        import ctypes
        if ctypes.windll.kernel32.GetUserDefaultUILanguage() == 1037:
            return "he"
    except Exception:
        pass
    return "en"


# ──────────────────────────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    "config_version":       CURRENT_VERSION,

    # ── Download location ─────────────────────────────────────────────────────
    "output_dir":           str(Path.home() / "Downloads" / "BananaFlow"),

    # ── Format & quality ──────────────────────────────────────────────────────
    "media_type":           "audio",
    "audio_quality":        DEFAULT_AUDIO_QUALITY_ID,
    "video_quality":        DEFAULT_VIDEO_QUALITY_ID,
    "audio_format":         "mp3",
    "video_format":         DEFAULT_VIDEO_FORMAT,
    "audio_quality_by_codec": {},

    # ── Metadata embedding ────────────────────────────────────────────────────
    "embed_thumbnail":      True,
    "embed_metadata":       True,

    # ── Cookies ───────────────────────────────────────────────────────────────
    "cookies_file":         "",
    "cookies_browser":      "",

    # ── Appearance ────────────────────────────────────────────────────────────
    "theme":                "light",
    "accent_color":         "#0ea5e9",      # NEW v3 – custom accent
    "language":             _detect_system_language(),
    "accessibility_mode":   False,          # NEW – high-contrast / keyboard nav

    # ── Window state ──────────────────────────────────────────────────────────
    "window_state":         "",

    # ── Clipboard / tray / updates ────────────────────────────────────────────
    "clipboard_monitor":    False,
    "check_updates":        True,
    "tray_on_close":        False,          # NEW – minimise to tray on close
    "global_hotkeys_enabled": False,        # NEW – OS-level hotkeys

    # ── History DB path ───────────────────────────────────────────────────────
    "history_db_path":      "",

    # ── Search ────────────────────────────────────────────────────────────────
    "last_search_query":    "",
    "last_search_platform": "youtube",
    "youtube_max_results":  15,
    "spotify_max_results":  15,

    # ── Batch import ──────────────────────────────────────────────────────────
    "batch_import_dir":     "",

    # ── Proxies ───────────────────────────────────────────────────────────────
    "proxy_server_url":         "http://localhost:8000",   # Spotify search proxy
    "youtube_proxy_url":        "",                        # HTTP/SOCKS proxy for yt-dlp
    "spotify_client_id":        "",
    "spotify_client_secret":    "",
    "spotify_app_api_key":      "",

    # ── Rate-limit politeness & parallelism ───────────────────────────────────
    "max_parallel_downloads": 3,           # Safer sweet spot
    "download_delay_range":   [1.5, 4.0],  # Min/Max seconds between requests

    # ── YouTube-only conservative reliability mode ────────────────────────────
    # "conservative" (default): YouTube jobs are serialized with a 5-10s
    # cooldown between them and single-fragment concurrency, regardless of
    # max_parallel_downloads. "fast" is opt-in and uses the normal
    # parallelism/delay settings above. Spotify/generic downloads are never
    # affected by this setting — see core/youtube_reliability.py.
    "youtube_reliability_mode": "conservative",

    # ── Advanced: audio post-processing ──────────────────────────────────────
    "sponsorblock_enabled":     False,      # cut non-music segments
    "sponsorblock_categories":  ["music_offtopic", "sponsor", "intro", "outro", "selfpromo"],
    "lyrics_enabled":        False,         # NEW – auto-fetch + embed lyrics
    "replay_gain_enabled":   False,         # NEW – ReplayGain analysis
    "square_thumbnails":     True,          # NEW – crop 16:9 art to 1:1
    "expand_thumbnails":     True,          # NEW – pad 1:1 art to 16:9
    "musicbrainz_enabled":   True,          # NEW – MusicBrainz tag enrichment

    # ── Playlist behaviour ────────────────────────────────────────────────────
    "playlist_subfolders":   True,          # NEW – create per-playlist subfolder
    "playlist_index_prefix": True,          # NEW – prefix filenames with 01-
    "singles_subfolder":     True,          # NEW – save singles inside category folder

    # ── Duplicate detection ───────────────────────────────────────────────────
    # "skip"      – silently skip if output file already exists
    # "warn"      – show a dialog and let the user decide
    # "overwrite" – always re-download (old behaviour)
    "duplicate_action":      "warn",        # NEW

    # ── Auto-resume (smart queue persistence) ─────────────────────────────────
    "queue_state":           [],            # NEW – list[dict] of pending TrackMeta
    # paused_items removed — superseded by core.paused_batch_store (see below).
    "magic_auto_ops":        ["title_strip", "track_num", "normalize_spaces"],
    "tag_editor_zoom":       100,
    "tag_editor_splitter_sizes": [],
    "tag_editor_column_widths":     {},
    "tag_editor_column_visibility": [],
    "tag_editor_column_order":      [],
    "tag_editor_sort_column":       -1,
    "tag_editor_sort_order":        0,
    "tag_editor_last_folder":       "",

    # ── Tag Cleaning Settings ─────────────────────────────────────────────────
    "tag_clean_title_remove_brackets":     True,
    "tag_clean_title_remove_web_junk":     True,
    "tag_clean_title_remove_hebrew":       True,
    "tag_clean_title_fix_punctuation":     True,
    "tag_clean_filename_smart_brackets":   True,
    "tag_clean_filename_remove_domains":   True,
    "tag_clean_filename_remove_emojis":    True,
    "tag_clean_filename_fix_spaces":       True,
}


# ──────────────────────────────────────────────────────────────────────────────
# Config file location
# ──────────────────────────────────────────────────────────────────────────────

def _config_path() -> Path:
    # Single source of truth for the app-data dir lives in utils.paths so
    # the config file, log dir, and history DB never drift across platforms
    # (Windows %APPDATA%, macOS ~/Library/Application Support, Linux XDG).
    from utils.paths import get_app_data_dir
    return get_app_data_dir() / "config.json"


def _default_db_path() -> str:
    return str(_config_path().parent / "downloads.db")


# ──────────────────────────────────────────────────────────────────────────────
# AppConfig
# ──────────────────────────────────────────────────────────────────────────────

class AppConfig:
    """
    Read/write wrapper around ~/.bananaflow/config.json.

    All public attributes are typed properties backed by self._data.
    Backward-compatible: every v1/v2 key is preserved unchanged.
    """

    def __init__(self) -> None:
        self._path: Path           = _config_path()
        self._data: dict[str, Any] = dict(_DEFAULTS)
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw    = self._path.read_text(encoding="utf-8")
            stored = json.loads(raw)
            if not isinstance(stored, dict):
                return

            # Run schema migrations (mutates stored in place)
            migrated = _run_migrations(stored)

            # Merge: only copy keys that exist in _DEFAULTS
            for key in _DEFAULTS:
                if key in stored:
                    self._data[key] = stored[key]

            # If migrations were applied, persist immediately
            if migrated:
                self.save()

        except (json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        try:
            from utils.security import write_private_text

            write_private_text(
                self._path,
                json.dumps(self._data, indent=2, ensure_ascii=False),
            )
        except OSError:
            pass

    def reset_to_defaults(self) -> None:
        self._data = dict(_DEFAULTS)
        self.save()

    # ── Context manager ────────────────────────────────────────────────────────

    def __enter__(self) -> "AppConfig":
        return self

    def __exit__(self, *_: Any) -> None:
        self.save()

    # ── Low-level ─────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Return a copy suitable for diagnostics/exports by default.

        Internal callers that explicitly need persisted credential values may
        opt in.  Environment-provided secrets are never added to this mapping.
        """
        data = dict(self._data)
        if include_secrets:
            return data
        from utils.security import redact_data

        return redact_data(data)

    def _secret_value(self, key: str, *environment_names: str) -> str:
        for name in environment_names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return str(self._data.get(key, "")).strip()

    # ──────────────────────────────────────────────────────────────────────────
    # Typed properties – v1 (preserved)
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def output_dir(self) -> str:
        v = self._data.get("output_dir", _DEFAULTS["output_dir"])
        return str(v) if v else _DEFAULTS["output_dir"]

    @output_dir.setter
    def output_dir(self, value: str) -> None:
        self._data["output_dir"] = str(value)

    @property
    def media_type(self) -> str:
        val = str(self._data.get("media_type", _DEFAULTS["media_type"]))
        return val if val in ("audio", "video") else "audio"

    @media_type.setter
    def media_type(self, value: str) -> None:
        if value not in ("audio", "video"):
            raise ValueError("media_type must be 'audio' or 'video'.")
        self._data["media_type"] = value

    @property
    def video_format(self) -> str:
        val = str(self._data.get("video_format", DEFAULT_VIDEO_FORMAT))
        return val if val in VIDEO_FORMATS else DEFAULT_VIDEO_FORMAT

    @video_format.setter
    def video_format(self, value: str) -> None:
        if value not in VIDEO_FORMATS:
            raise ValueError(f"video_format must be one of {VIDEO_FORMATS!r}.")
        self._data["video_format"] = value

    @property
    def audio_quality(self) -> str:
        return coerce_audio_quality_id(
            str(self._data.get("audio_quality", _DEFAULTS["audio_quality"])),
            self.audio_format,
        )

    @audio_quality.setter
    def audio_quality(self, value: str) -> None:
        self._data["audio_quality"] = str(value)

    @property
    def video_quality(self) -> str:
        return coerce_video_quality_id(
            str(self._data.get("video_quality", _DEFAULTS["video_quality"]))
        )

    @video_quality.setter
    def video_quality(self, value: str) -> None:
        self._data["video_quality"] = str(value)

    @property
    def audio_format(self) -> str:
        return str(self._data.get("audio_format", _DEFAULTS["audio_format"]))

    @audio_format.setter
    def audio_format(self, value: str) -> None:
        self._data["audio_format"] = str(value)

    @property
    def audio_quality_by_codec(self) -> dict[str, str]:
        raw = self._data.get("audio_quality_by_codec", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, str] = {}
        for codec in ("mp3", "m4a", "opus", "flac"):
            value = raw.get(codec)
            if value in valid_audio_quality_ids():
                result[codec] = coerce_audio_quality_id(str(value), codec)
        return result

    @audio_quality_by_codec.setter
    def audio_quality_by_codec(self, value: dict[str, str]) -> None:
        clean: dict[str, str] = {}
        if isinstance(value, dict):
            for codec in ("mp3", "m4a", "opus", "flac"):
                preset_id = value.get(codec)
                if preset_id:
                    clean[codec] = coerce_audio_quality_id(
                        str(preset_id),
                        codec,
                    )
                else:
                    clean[codec] = default_audio_quality_id_for_codec(codec)
        self._data["audio_quality_by_codec"] = clean

    @property
    def embed_thumbnail(self) -> bool:
        return bool(self._data.get("embed_thumbnail", _DEFAULTS["embed_thumbnail"]))

    @embed_thumbnail.setter
    def embed_thumbnail(self, value: bool) -> None:
        self._data["embed_thumbnail"] = bool(value)

    @property
    def embed_metadata(self) -> bool:
        return bool(self._data.get("embed_metadata", _DEFAULTS["embed_metadata"]))

    @embed_metadata.setter
    def embed_metadata(self, value: bool) -> None:
        self._data["embed_metadata"] = bool(value)

    @property
    def cookies_file(self) -> str:
        return str(self._data.get("cookies_file", ""))

    @cookies_file.setter
    def cookies_file(self, value: str) -> None:
        self._data["cookies_file"] = str(value)

    @property
    def window_state(self) -> str:
        return str(self._data.get("window_state", ""))

    @window_state.setter
    def window_state(self, value: str) -> None:
        self._data["window_state"] = str(value)

    @property
    def clipboard_monitor(self) -> bool:
        return bool(self._data.get("clipboard_monitor", _DEFAULTS["clipboard_monitor"]))

    @clipboard_monitor.setter
    def clipboard_monitor(self, value: bool) -> None:
        self._data["clipboard_monitor"] = bool(value)

    @property
    def check_updates(self) -> bool:
        return bool(self._data.get("check_updates", _DEFAULTS["check_updates"]))

    @check_updates.setter
    def check_updates(self, value: bool) -> None:
        self._data["check_updates"] = bool(value)

    @property
    def history_db_path(self) -> str:
        return str(self._data.get("history_db_path", ""))

    @history_db_path.setter
    def history_db_path(self, value: str) -> None:
        self._data["history_db_path"] = str(value)

    def resolved_history_db_path(self) -> str:
        stored = self.history_db_path.strip()
        return stored if stored else _default_db_path()

    @property
    def last_search_query(self) -> str:
        return str(self._data.get("last_search_query", ""))

    @last_search_query.setter
    def last_search_query(self, value: str) -> None:
        self._data["last_search_query"] = str(value)

    @property
    def last_search_platform(self) -> str:
        val = str(self._data.get("last_search_platform", "youtube"))
        return val if val in ("youtube", "ytmusic", "spotify", "both") else "youtube"

    @last_search_platform.setter
    def last_search_platform(self, value: str) -> None:
        self._data["last_search_platform"] = value

    @property
    def youtube_max_results(self) -> int:
        raw = self._data.get("youtube_max_results", 15)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = 15
        return max(1, min(100, val))

    @youtube_max_results.setter
    def youtube_max_results(self, value: int) -> None:
        self._data["youtube_max_results"] = max(1, min(100, int(value)))

    @property
    def spotify_max_results(self) -> int:
        raw = self._data.get("spotify_max_results", 15)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = 15
        return max(1, min(100, val))

    @spotify_max_results.setter
    def spotify_max_results(self, value: int) -> None:
        self._data["spotify_max_results"] = max(1, min(100, int(value)))

    @property
    def batch_import_dir(self) -> str:
        return str(self._data.get("batch_import_dir", ""))

    @batch_import_dir.setter
    def batch_import_dir(self, value: str) -> None:
        self._data["batch_import_dir"] = str(value)

    @property
    def proxy_server_url(self) -> str:
        return str(self._data.get("proxy_server_url", _DEFAULTS["proxy_server_url"]))

    @proxy_server_url.setter
    def proxy_server_url(self, value: str) -> None:
        self._data["proxy_server_url"] = str(value)

    @property
    def cookies_browser(self) -> str:
        return str(self._data.get("cookies_browser", ""))

    @cookies_browser.setter
    def cookies_browser(self, value: str) -> None:
        self._data["cookies_browser"] = value.lower().strip()

    @property
    def spotify_client_id(self) -> str:
        return self._secret_value(
            "spotify_client_id", "BANANAFLOW_SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_ID",
        )

    @spotify_client_id.setter
    def spotify_client_id(self, value: str) -> None:
        self._data["spotify_client_id"] = str(value).strip()

    @property
    def spotify_client_secret(self) -> str:
        return self._secret_value(
            "spotify_client_secret",
            "BANANAFLOW_SPOTIFY_CLIENT_SECRET",
            "SPOTIFY_CLIENT_SECRET",
        )

    @spotify_client_secret.setter
    def spotify_client_secret(self, value: str) -> None:
        self._data["spotify_client_secret"] = str(value).strip()

    @property
    def spotify_app_api_key(self) -> str:
        return self._secret_value(
            "spotify_app_api_key",
            "BANANAFLOW_SPOTIFY_APP_API_KEY",
            "SPOTIFY_APP_API_KEY",
        )

    @spotify_app_api_key.setter
    def spotify_app_api_key(self, value: str) -> None:
        self._data["spotify_app_api_key"] = str(value).strip()

    @property
    def max_parallel_downloads(self) -> int:
        raw = self._data.get("max_parallel_downloads", _DEFAULTS["max_parallel_downloads"])
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = _DEFAULTS["max_parallel_downloads"]
        return max(1, min(6, val))

    @max_parallel_downloads.setter
    def max_parallel_downloads(self, value: int) -> None:
        self._data["max_parallel_downloads"] = max(1, min(6, int(value)))

    # ── Rate-limit politeness typed properties ────────────────────────────────

    @property
    def download_delay_range(self) -> list[float]:
        val = self._data.get("download_delay_range", _DEFAULTS["download_delay_range"])
        return val if isinstance(val, list) and len(val) == 2 else _DEFAULTS["download_delay_range"]

    @download_delay_range.setter
    def download_delay_range(self, value: list[float]) -> None:
        if isinstance(value, list) and len(value) == 2:
            self._data["download_delay_range"] = [float(v) for v in value]

    @property
    def youtube_reliability_mode(self) -> str:
        val = str(self._data.get("youtube_reliability_mode", "conservative"))
        return val if val in ("conservative", "fast") else "conservative"

    @youtube_reliability_mode.setter
    def youtube_reliability_mode(self, value: str) -> None:
        if value not in ("conservative", "fast"):
            raise ValueError("youtube_reliability_mode must be 'conservative' or 'fast'.")
        self._data["youtube_reliability_mode"] = value

    # ──────────────────────────────────────────────────────────────────────────
    # Typed properties – v3 (new Phase 2 features)
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def accent_color(self) -> str:
        return str(self._data.get("accent_color", _DEFAULTS["accent_color"]))

    @accent_color.setter
    def accent_color(self, value: str) -> None:
        self._data["accent_color"] = str(value)

    @property
    def accessibility_mode(self) -> bool:
        return bool(self._data.get("accessibility_mode", False))

    @accessibility_mode.setter
    def accessibility_mode(self, value: bool) -> None:
        self._data["accessibility_mode"] = bool(value)

    @property
    def tray_on_close(self) -> bool:
        return bool(self._data.get("tray_on_close", False))

    @tray_on_close.setter
    def tray_on_close(self, value: bool) -> None:
        self._data["tray_on_close"] = bool(value)

    @property
    def global_hotkeys_enabled(self) -> bool:
        return bool(self._data.get("global_hotkeys_enabled", False))

    @global_hotkeys_enabled.setter
    def global_hotkeys_enabled(self, value: bool) -> None:
        self._data["global_hotkeys_enabled"] = bool(value)

    @property
    def sponsorblock_enabled(self) -> bool:
        return bool(self._data.get("sponsorblock_enabled", False))

    @sponsorblock_enabled.setter
    def sponsorblock_enabled(self, value: bool) -> None:
        self._data["sponsorblock_enabled"] = bool(value)

    @property
    def lyrics_enabled(self) -> bool:
        """Advanced setting – disabled by default."""
        return bool(self._data.get("lyrics_enabled", False))

    @lyrics_enabled.setter
    def lyrics_enabled(self, value: bool) -> None:
        self._data["lyrics_enabled"] = bool(value)

    @property
    def replay_gain_enabled(self) -> bool:
        """Advanced setting – disabled by default."""
        return bool(self._data.get("replay_gain_enabled", False))

    @replay_gain_enabled.setter
    def replay_gain_enabled(self, value: bool) -> None:
        self._data["replay_gain_enabled"] = bool(value)

    @property
    def square_thumbnails(self) -> bool:
        """Advanced setting – disabled by default."""
        return bool(self._data.get("square_thumbnails", False))

    @square_thumbnails.setter
    def square_thumbnails(self, value: bool) -> None:
        self._data["square_thumbnails"] = bool(value)

    @property
    def expand_thumbnails(self) -> bool:
        """Advanced setting – pad 1:1 art to 16:9 for video."""
        return bool(self._data.get("expand_thumbnails", True))

    @expand_thumbnails.setter
    def expand_thumbnails(self, value: bool) -> None:
        self._data["expand_thumbnails"] = bool(value)

    @property
    def musicbrainz_enabled(self) -> bool:
        return bool(self._data.get("musicbrainz_enabled", True))

    @musicbrainz_enabled.setter
    def musicbrainz_enabled(self, value: bool) -> None:
        self._data["musicbrainz_enabled"] = bool(value)

    @property
    def playlist_subfolders(self) -> bool:
        return bool(self._data.get("playlist_subfolders", True))

    @playlist_subfolders.setter
    def playlist_subfolders(self, value: bool) -> None:
        self._data["playlist_subfolders"] = bool(value)

    @property
    def playlist_index_prefix(self) -> bool:
        return bool(self._data.get("playlist_index_prefix", True))

    @playlist_index_prefix.setter
    def playlist_index_prefix(self, value: bool) -> None:
        self._data["playlist_index_prefix"] = bool(value)

    @property
    def singles_subfolder(self) -> bool:
        return bool(self._data.get("singles_subfolder", True))

    @singles_subfolder.setter
    def singles_subfolder(self, value: bool) -> None:
        self._data["singles_subfolder"] = bool(value)

    @property
    def duplicate_action(self) -> str:
        val = str(self._data.get("duplicate_action", "warn"))
        return val if val in ("skip", "warn", "overwrite") else "warn"

    @duplicate_action.setter
    def duplicate_action(self, value: str) -> None:
        if value not in ("skip", "warn", "overwrite"):
            raise ValueError("duplicate_action must be 'skip', 'warn', or 'overwrite'.")
        self._data["duplicate_action"] = value

    @property
    def queue_state(self) -> list:
        val = self._data.get("queue_state", [])
        return val if isinstance(val, list) else []

    @queue_state.setter
    def queue_state(self, value: list) -> None:
        self._data["queue_state"] = list(value)

    # NOTE: the former ``paused_items`` config list was a write-only dead
    # path — nothing ever read it back to resume anything. It is superseded
    # by core.paused_batch_store.PausedBatchStore, the single authoritative
    # persisted-pause mechanism (see DownloadController). Any stale
    # ``paused_items`` key left in an existing config file is simply ignored.

    def __repr__(self) -> str:
        return (
            f"AppConfig(path={self._path!r}, theme={self.theme!r}, "
            f"output_dir={self.output_dir!r})"
        )

    # ── Theme property ─────────────────────────────────────────────────────────

    @property
    def theme(self) -> str:
        val = str(self._data.get("theme", "dark"))
        if val == "oled":
            return "dark"
        return val if val in ("dark", "light") else "dark"

    @theme.setter
    def theme(self, value: str) -> None:
        if value not in ("dark", "light"):
            raise ValueError(f"Invalid theme '{value}'.")
        self._data["theme"] = value

    @property
    def language(self) -> str:
        val = str(self._data.get("language", "en"))
        return val if val in ("en", "he") else "en"

    @language.setter
    def language(self, value: str) -> None:
        self._data["language"] = value

    @property
    def magic_auto_ops(self) -> list[str]:
        val = self._data.get("magic_auto_ops", ["title_strip", "track_num", "normalize_spaces"])
        return list(val) if isinstance(val, list) else ["title_strip", "track_num", "normalize_spaces"]

    @magic_auto_ops.setter
    def magic_auto_ops(self, value: list[str]) -> None:
        self._data["magic_auto_ops"] = list(value)

    @property
    def tag_editor_zoom(self) -> int:
        raw = self._data.get("tag_editor_zoom", 100)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = 100
        return max(50, min(200, val))

    @tag_editor_zoom.setter
    def tag_editor_zoom(self, value: int) -> None:
        self._data["tag_editor_zoom"] = max(50, min(200, int(value)))

    @property
    def tag_editor_splitter_sizes(self) -> list[int]:
        val = self._data.get("tag_editor_splitter_sizes", [])
        return val if isinstance(val, list) else []

    @tag_editor_splitter_sizes.setter
    def tag_editor_splitter_sizes(self, value: list[int]) -> None:
        self._data["tag_editor_splitter_sizes"] = list(value)

    @property
    def tag_editor_column_widths(self) -> dict[str, int]:
        val = self._data.get("tag_editor_column_widths", {})
        return val if isinstance(val, dict) else {}

    @tag_editor_column_widths.setter
    def tag_editor_column_widths(self, value: dict[str, int]) -> None:
        self._data["tag_editor_column_widths"] = dict(value)

    @property
    def tag_editor_column_visibility(self) -> list[int]:
        val = self._data.get("tag_editor_column_visibility", [])
        return val if isinstance(val, list) else []

    @tag_editor_column_visibility.setter
    def tag_editor_column_visibility(self, value: list[int]) -> None:
        self._data["tag_editor_column_visibility"] = list(value)

    @property
    def tag_editor_column_order(self) -> list[int]:
        val = self._data.get("tag_editor_column_order", [])
        return val if isinstance(val, list) else []

    @tag_editor_column_order.setter
    def tag_editor_column_order(self, value: list[int]) -> None:
        self._data["tag_editor_column_order"] = list(value)

    @property
    def tag_editor_sort_column(self) -> int:
        return int(self._data.get("tag_editor_sort_column", -1))

    @tag_editor_sort_column.setter
    def tag_editor_sort_column(self, value: int) -> None:
        self._data["tag_editor_sort_column"] = int(value)

    @property
    def tag_editor_sort_order(self) -> int:
        return int(self._data.get("tag_editor_sort_order", 0))

    @tag_editor_sort_order.setter
    def tag_editor_sort_order(self, value: int) -> None:
        self._data["tag_editor_sort_order"] = int(value)

    @property
    def tag_editor_last_folder(self) -> str:
        return str(self._data.get("tag_editor_last_folder", ""))

    @tag_editor_last_folder.setter
    def tag_editor_last_folder(self, value: str) -> None:
        self._data["tag_editor_last_folder"] = str(value)

    # ── Tag Cleaning Settings ─────────────────────────────────────────────────

    @property
    def tag_clean_title_remove_brackets(self) -> bool:
        return bool(self._data.get("tag_clean_title_remove_brackets", True))

    @tag_clean_title_remove_brackets.setter
    def tag_clean_title_remove_brackets(self, value: bool) -> None:
        self._data["tag_clean_title_remove_brackets"] = bool(value)

    @property
    def tag_clean_title_remove_web_junk(self) -> bool:
        return bool(self._data.get("tag_clean_title_remove_web_junk", True))

    @tag_clean_title_remove_web_junk.setter
    def tag_clean_title_remove_web_junk(self, value: bool) -> None:
        self._data["tag_clean_title_remove_web_junk"] = bool(value)

    @property
    def tag_clean_title_remove_hebrew(self) -> bool:
        return bool(self._data.get("tag_clean_title_remove_hebrew", True))

    @tag_clean_title_remove_hebrew.setter
    def tag_clean_title_remove_hebrew(self, value: bool) -> None:
        self._data["tag_clean_title_remove_hebrew"] = bool(value)

    @property
    def tag_clean_title_fix_punctuation(self) -> bool:
        return bool(self._data.get("tag_clean_title_fix_punctuation", True))

    @tag_clean_title_fix_punctuation.setter
    def tag_clean_title_fix_punctuation(self, value: bool) -> None:
        self._data["tag_clean_title_fix_punctuation"] = bool(value)

    @property
    def tag_clean_filename_smart_brackets(self) -> bool:
        return bool(self._data.get("tag_clean_filename_smart_brackets", True))

    @tag_clean_filename_smart_brackets.setter
    def tag_clean_filename_smart_brackets(self, value: bool) -> None:
        self._data["tag_clean_filename_smart_brackets"] = bool(value)

    @property
    def tag_clean_filename_remove_domains(self) -> bool:
        return bool(self._data.get("tag_clean_filename_remove_domains", True))

    @tag_clean_filename_remove_domains.setter
    def tag_clean_filename_remove_domains(self, value: bool) -> None:
        self._data["tag_clean_filename_remove_domains"] = bool(value)

    @property
    def tag_clean_filename_remove_emojis(self) -> bool:
        return bool(self._data.get("tag_clean_filename_remove_emojis", True))

    @tag_clean_filename_remove_emojis.setter
    def tag_clean_filename_remove_emojis(self, value: bool) -> None:
        self._data["tag_clean_filename_remove_emojis"] = bool(value)

    @property
    def tag_clean_filename_fix_spaces(self) -> bool:
        return bool(self._data.get("tag_clean_filename_fix_spaces", True))

    @tag_clean_filename_fix_spaces.setter
    def tag_clean_filename_fix_spaces(self, value: bool) -> None:
        self._data["tag_clean_filename_fix_spaces"] = bool(value)
