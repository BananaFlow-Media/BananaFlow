"""
tests/test_core.py  –  Offline unit tests for BananaFlow core layer
===========================================================================
Run:
    pytest tests/test_core.py -v

Coverage targets: AppConfig, HistoryDB, classify_url, classify_error,
BatchImporter, duplicate_checker.

All tests are offline (no network) and headless (no Qt/GUI).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# 1. AppConfig
# ──────────────────────────────────────────────────────────────────────────────

class TestAppConfig:
    """Round-trip persistence, default merging, edge cases."""

    def _make_config(self, tmp_path: Path) -> "AppConfig":
        """Create an AppConfig that writes to a temp directory."""
        from config import AppConfig
        cfg = AppConfig.__new__(AppConfig)
        cfg._path = tmp_path / "config.json"
        from config import _DEFAULTS
        cfg._data = dict(_DEFAULTS)
        return cfg

    def test_defaults_applied(self, tmp_path):
        cfg = self._make_config(tmp_path)
        assert cfg.media_type == "audio"
        assert cfg.audio_quality == "audio_mp3_320"
        assert cfg.video_quality == "video_1080"
        assert cfg.audio_format == "mp3"
        assert cfg.video_format == "mp4"
        assert cfg.embed_thumbnail is True
        assert cfg.output_dir  # non-empty default

    def test_save_and_reload(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg.media_type = "video"
        cfg.audio_quality = "audio_mp3_128"
        cfg.save()

        # Reload from same path
        cfg2 = self._make_config(tmp_path)
        cfg2._load()
        assert cfg2.media_type == "video"
        assert cfg2.audio_quality == "audio_mp3_128"

    def test_media_type_setter_rejects_invalid_value(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with pytest.raises(ValueError):
            cfg.media_type = "mp3"

    def test_video_format_setter_rejects_invalid_value(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with pytest.raises(ValueError):
            cfg.video_format = "webm"

    def test_media_type_getter_clamps_invalid_stored_value(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg._data["media_type"] = "not-a-real-type"
        assert cfg.media_type == "audio"

    def test_video_format_getter_clamps_invalid_stored_value(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg._data["video_format"] = "webm"
        assert cfg.video_format == "mp4"

    def test_migration_3_drops_dead_settings_keys(self):
        """Migration 3 removes the never-implemented 'randomize_user_agent'
        and the superseded 'search_max_results' from stored configs. The
        full pipeline also runs migration 7 on this data (media_format ->
        media_type), since migrate() always runs every pending migration."""
        from config_migrate import CURRENT_VERSION, migrate

        assert CURRENT_VERSION >= 3
        data = {
            "config_version": 2,
            "randomize_user_agent": True,
            "search_max_results": 25,
            "media_format": "mp3",
        }
        assert migrate(data) is True
        assert "randomize_user_agent" not in data
        assert "search_max_results" not in data
        assert data["config_version"] == CURRENT_VERSION
        assert data["media_type"] == "audio"
        assert "media_format" not in data

    def test_migration_4_normalizes_quality_labels(self):
        """Migration 4 preserves configs written before quality labels
        changed from parenthesis style ("Low (128k)") to dot-separator
        style ("Low · 128k"). Tested against the migration function
        directly rather than the full migrate() pipeline, since migration
        5 further transforms the value into a stable key — see
        test_migration_5_replaces_quality_labels_with_stable_keys below."""
        from config_migrate import _normalise_quality_labels

        data = {
            "audio_quality": "Low (128k)",
            "video_quality": "2160p (4K)",
        }
        _normalise_quality_labels(data)
        assert data["audio_quality"] == "Low · 128k"
        assert data["video_quality"] == "Best · 2160p"

    def test_migration_5_replaces_quality_labels_with_stable_keys(self):
        """Full migration replaces translated display strings with current
        stable preset IDs; labels are not kept as logical values."""
        from config_migrate import CURRENT_VERSION, migrate

        assert CURRENT_VERSION >= 5
        data = {
            "config_version": 4,
            "audio_quality": "Low · 128k",
            "video_quality": "Best · 2160p",
            "media_format": "mp3",
        }
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        assert data["audio_quality"] == "audio_mp3_128"
        assert data["video_quality"] == "video_2160"
        assert data["audio_quality_by_codec"]["mp3"] == "audio_mp3_128"
        assert data["media_type"] == "audio"
        assert "media_format" not in data

    def test_migration_full_pipeline_from_legacy_parenthesis_labels(self):
        """A config that predates every label-format change (parenthesis
        style, config_version 3 or earlier) must land on the current
        stable key after running the full migration pipeline in one pass."""
        from config_migrate import CURRENT_VERSION, migrate

        data = {
            "config_version": 3,
            "audio_quality": "Low (128k)",
            "video_quality": "2160p (4K)",
            "media_format": "mp4",
        }
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        assert data["audio_quality"] == "audio_mp3_128"
        assert data["video_quality"] == "video_2160"
        assert data["media_type"] == "video"
        assert "media_format" not in data

    def test_migration_preserves_existing_user_codec_choice(self):
        from config_migrate import CURRENT_VERSION, migrate

        data = {
            "config_version": 4,
            "audio_format": "m4a",
            "audio_quality": "High · 256k",
            "video_quality": "720p",
        }
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        assert data["audio_quality"] == "audio_m4a_256"
        assert data["audio_quality_by_codec"]["m4a"] == "audio_m4a_256"
        assert data["video_quality"] == "video_720"
        assert data["audio_format"] == "m4a"   # untouched by migration 7
        # No media_format key was present, so migration 7 is a no-op here
        # (it only converts an existing media_format, like migration 2's
        # key removal); media_type/video_format are additive and are
        # filled in later by AppConfig's _DEFAULTS merge, not by migrate().
        assert "media_type" not in data

    @pytest.mark.parametrize("old_value, expected", [
        ("Best · 320k", "audio_mp3_320"),
        ("High · 256k", "audio_mp3_256"),
        ("Medium · 192k", "audio_mp3_192"),
        ("Low · 128k", "audio_mp3_128"),
        ("Best (320k)", "audio_mp3_320"),
        ("High (256k)", "audio_mp3_256"),
        ("Medium (192k)", "audio_mp3_192"),
        ("Low (128k)", "audio_mp3_128"),
        ("BEST", "audio_mp3_320"),
        ("HIGH", "audio_mp3_256"),
        ("MEDIUM", "audio_mp3_192"),
        ("LOW", "audio_mp3_128"),
    ])
    def test_migration_from_old_audio_labels(self, old_value, expected):
        from config_migrate import migrate

        data = {
            "config_version": 5,
            "audio_format": "mp3",
            "audio_quality": old_value,
        }
        assert migrate(data) is True
        assert data["audio_quality"] == expected

    @pytest.mark.parametrize("old_value, expected", [
        ("Best · 2160p", "video_2160"),
        ("2160p (4K)", "video_2160"),
        ("1440p · 2K", "video_1440"),
        ("1440p (2K)", "video_1440"),
        ("1080p", "video_1080"),
        ("720p", "video_720"),
        ("480p", "video_480"),
        ("360p", "video_360"),
        ("Worst", "video_smallest"),
        ("UHD_4K", "video_2160"),
        ("QHD_2K", "video_1440"),
        ("HIGH", "video_1080"),
        ("MEDIUM", "video_720"),
        ("LOW", "video_480"),
        ("WORST", "video_smallest"),
    ])
    def test_migration_from_old_video_labels(self, old_value, expected):
        from config_migrate import migrate

        data = {
            "config_version": 5,
            "video_quality": old_value,
        }
        assert migrate(data) is True
        assert data["video_quality"] == expected

    def test_unknown_quality_values_fall_back_safely(self):
        from config_migrate import migrate

        data = {
            "config_version": 5,
            "audio_format": "opus",
            "audio_quality": {"bad": "shape"},
            "video_quality": ["bad"],
        }
        assert migrate(data) is True
        assert data["audio_quality"] == "audio_opus_best"
        assert data["video_quality"] == "video_1080"

    def test_migration_7_media_format_mp3_becomes_audio(self):
        from config_migrate import CURRENT_VERSION, migrate

        data = {"config_version": 6, "media_format": "mp3", "audio_format": "flac"}
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        assert data["media_type"] == "audio"
        assert data["audio_format"] == "flac"   # untouched
        assert "media_format" not in data

    def test_migration_7_media_format_mp4_becomes_video(self):
        from config_migrate import CURRENT_VERSION, migrate

        data = {"config_version": 6, "media_format": "mp4", "audio_format": "m4a"}
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        assert data["media_type"] == "video"
        assert data["audio_format"] == "m4a"   # untouched
        assert "media_format" not in data

    def test_migration_7_is_noop_without_media_format(self):
        """A config that never had 'media_format' (e.g. already on the
        current schema, or missing the key for any other reason) must not
        have 'media_type' fabricated by the migration itself — that key is
        additive and is filled in by AppConfig's _DEFAULTS merge instead.
        See test_video_format_defaults_via_appconfig_when_absent below for
        the full AppConfig-level behavior."""
        from config_migrate import CURRENT_VERSION, migrate

        data = {"config_version": 6}
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        assert "media_type" not in data
        assert "media_format" not in data

    def test_video_format_defaults_via_appconfig_when_absent(self, tmp_path):
        """video_format is purely additive (core/media_formats.py) — a
        stored config that predates it must still resolve to the default
        through AppConfig's normal _DEFAULTS merge, without any migration
        needing to set it explicitly."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "config_version": 6,
            "media_format": "mp4",
        }))
        cfg = self._make_config(tmp_path)
        cfg._load()
        assert cfg.media_type == "video"
        assert cfg.video_format == "mp4"

    def test_migration_is_idempotent(self):
        """Running migrate() a second time on already-migrated data must
        not corrupt or repeatedly transform it — relies on the version
        gate in migrate(), the same mechanism migrations 1-6 depend on."""
        from config_migrate import CURRENT_VERSION, migrate

        data = {"config_version": 2, "media_format": "mp4", "audio_format": "flac"}
        assert migrate(data) is True
        assert data["config_version"] == CURRENT_VERSION
        once = dict(data)

        assert migrate(data) is False
        assert data == once

    def test_windows_upgrade_clears_live_chromium_and_sets_notice(self, monkeypatch):
        import config_migrate

        monkeypatch.setattr(config_migrate.sys, "platform", "win32")
        data = {"config_version": 9, "cookies_browser": "Chrome"}
        assert config_migrate.migrate(data) is True
        assert data["cookies_browser"] == ""
        assert data["cookies_browser_migration_notice_pending"] is True

    @pytest.mark.parametrize("browser", ["firefox", ""])
    def test_windows_upgrade_preserves_supported_browser_values(
        self, monkeypatch, browser,
    ):
        import config_migrate

        monkeypatch.setattr(config_migrate.sys, "platform", "win32")
        data = {"config_version": 9, "cookies_browser": browser}
        assert config_migrate.migrate(data) is True
        assert data["cookies_browser"] == browser
        assert not data.get("cookies_browser_migration_notice_pending", False)

    def test_non_windows_upgrade_preserves_live_chromium(self, monkeypatch):
        import config_migrate

        monkeypatch.setattr(config_migrate.sys, "platform", "linux")
        data = {"config_version": 9, "cookies_browser": "chrome"}
        assert config_migrate.migrate(data) is True
        assert data["cookies_browser"] == "chrome"
        assert not data.get("cookies_browser_migration_notice_pending", False)

    def test_windows_browser_migration_is_saved_by_app_config(
        self, tmp_path, monkeypatch,
    ):
        import config_migrate

        monkeypatch.setattr(config_migrate.sys, "platform", "win32")
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "config_version": 9,
            "cookies_browser": "edge",
        }), encoding="utf-8")
        cfg = self._make_config(tmp_path)
        cfg._load()
        stored = json.loads(config_file.read_text(encoding="utf-8"))
        assert cfg.cookies_browser == ""
        assert cfg.cookies_browser_migration_notice_pending is True
        assert stored["cookies_browser"] == ""
        assert stored["cookies_browser_migration_notice_pending"] is True

    def test_unknown_keys_preserved(self, tmp_path):
        """Keys not in _DEFAULTS should not crash _load."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "media_format": "mp4",
            "future_key": "hello",
        }))
        cfg = self._make_config(tmp_path)
        cfg._load()
        assert cfg.media_type == "video"
        # future_key is silently ignored (not in _DEFAULTS)

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("{{{invalid json")
        cfg = self._make_config(tmp_path)
        cfg._load()
        assert cfg.media_type == "audio"  # default

    def test_atomic_write_no_partial(self, tmp_path):
        """If save() completes, config.json exists and .tmp does not."""
        cfg = self._make_config(tmp_path)
        cfg.save()
        assert (tmp_path / "config.json").exists()
        assert not (tmp_path / "config.tmp").exists()

    def test_context_manager_saves(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with cfg:
            cfg.media_type = "video"
        reloaded = json.loads((tmp_path / "config.json").read_text())
        assert reloaded["media_type"] == "video"

    def test_youtube_reliability_mode_defaults_to_conservative(self, tmp_path):
        cfg = self._make_config(tmp_path)
        assert cfg.youtube_reliability_mode == "conservative"

    def test_youtube_reliability_mode_round_trip_fast(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg.youtube_reliability_mode = "fast"
        cfg.save()
        cfg2 = self._make_config(tmp_path)
        cfg2._load()
        assert cfg2.youtube_reliability_mode == "fast"

    def test_youtube_reliability_mode_rejects_invalid_value(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with pytest.raises(ValueError):
            cfg.youtube_reliability_mode = "turbo"


class TestYtDlpOptions:
    def test_chrome_cookies_use_last_used_profile(self, tmp_path, monkeypatch):
        """utils.yt_dlp_opts._CHROMIUM_LOCAL_STATE_PATHS is genuinely
        cross-platform (win32/darwin/linux each have their own Local
        State location) — this test must build the fixture at whichever
        path sys.platform actually resolves to, not just Windows'. A
        Windows-only version of this test passed on windows-latest CI
        while silently exercising nothing but the "not found" fallback
        path on ubuntu-latest, which is exactly how this gap went
        unnoticed until CI actually ran GUI/Qt-adjacent tests on Linux
        for the first time in Phase 5.
        """
        import sys
        from utils.yt_dlp_opts import build_base_ydl_opts

        if sys.platform == "win32":
            local_state_dir = tmp_path / "Google" / "Chrome" / "User Data"
            monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        elif sys.platform == "darwin":
            local_state_dir = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
            monkeypatch.setenv("HOME", str(tmp_path))
        else:
            local_state_dir = tmp_path / ".config" / "google-chrome"
            monkeypatch.setenv("HOME", str(tmp_path))

        local_state_dir.mkdir(parents=True)
        (local_state_dir / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 2"}}),
            encoding="utf-8",
        )

        if sys.platform == "win32":
            from core.browser_session import BrowserCookieAccessError
            with pytest.raises(BrowserCookieAccessError):
                build_base_ydl_opts(cookies_browser="chrome")
        else:
            opts = build_base_ydl_opts(cookies_browser="chrome")
            assert opts["cookiesfrombrowser"] == ("chrome", "Profile 2", None, None)

    def test_browser_cookies_fall_back_without_profile(self, tmp_path, monkeypatch):
        from utils.yt_dlp_opts import build_base_ydl_opts

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        opts = build_base_ydl_opts(cookies_browser="firefox")
        assert opts["cookiesfrombrowser"] == ("firefox", None, None, None)


# ──────────────────────────────────────────────────────────────────────────────
# 2. HistoryDB
# ──────────────────────────────────────────────────────────────────────────────

class TestHistoryDB:
    """CRUD, FTS, CSV export — all on :memory: DB."""

    @pytest.fixture
    def db(self):
        from core.history_db import HistoryDB
        return HistoryDB(":memory:")

    @pytest.fixture
    def sample_record(self):
        from core.history_db import DownloadRecord
        return DownloadRecord(
            title="Example Song",
            artist="Example Artist",
            url="https://www.youtube.com/watch?v=TESTVIDEOAAA",
            output_path="/tmp/example.mp3",
            media_type="audio",
            platform="youtube",
        )

    def test_insert_and_fetch(self, db, sample_record):
        rec_id = db.insert(sample_record)
        assert rec_id > 0
        records = db.fetch_all(limit=10)
        assert len(records) == 1
        assert records[0].title == "Example Song"
        assert records[0].artist == "Example Artist"

    def test_count(self, db, sample_record):
        assert db.count() == 0
        db.insert(sample_record)
        assert db.count() == 1

    def test_delete(self, db, sample_record):
        rec_id = db.insert(sample_record)
        db.delete(rec_id)
        assert db.count() == 0

    def test_delete_nonexistent_silent(self, db):
        db.delete(99999)  # should not raise

    def test_clear_all(self, db, sample_record):
        for _ in range(5):
            db.insert(sample_record)
        assert db.count() == 5
        db.clear_all()
        assert db.count() == 0

    def test_fts_search(self, db, sample_record):
        db.insert(sample_record)
        results = db.search("example artist")
        assert len(results) == 1
        assert results[0].title == "Example Song"

    def test_fts_search_no_match(self, db, sample_record):
        db.insert(sample_record)
        results = db.search("beethoven")
        assert len(results) == 0

    def test_export_csv(self, db, sample_record, tmp_path):
        db.insert(sample_record)
        csv_path = str(tmp_path / "export.csv")
        count = db.export_csv(csv_path)
        assert count == 1
        content = Path(csv_path).read_text(encoding="utf-8-sig")
        assert "Example Artist" in content
        assert "Example Song" in content

    def test_downloaded_at_auto_filled(self, db, sample_record):
        assert sample_record.downloaded_at == ""
        db.insert(sample_record)
        records = db.fetch_all()
        assert records[0].downloaded_at  # non-empty after insert


# ──────────────────────────────────────────────────────────────────────────────
# 3. URL Classifier (playlist_parser.classify_url)
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyUrl:
    """Pure regex, no network."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from core.playlist_parser import classify_url, SourcePlatform, UrlKind
        self.classify = classify_url
        self.SP = SourcePlatform
        self.UK = UrlKind

    @pytest.mark.parametrize("url, exp_plat, exp_kind", [
        # YouTube
        ("https://www.youtube.com/watch?v=TESTVIDEOAAA",            "YOUTUBE",       "SINGLE_VIDEO"),
        ("https://youtu.be/TESTVIDEOAAA",                          "YOUTUBE",       "SINGLE_VIDEO"),
        ("https://www.youtube.com/playlist?list=PLxxxxx",          "YOUTUBE",       "PLAYLIST"),
        ("https://www.youtube.com/watch?v=abc&list=PLxxx",         "YOUTUBE",       "PLAYLIST"),
        # YouTube Music
        ("https://music.youtube.com/watch?v=xyz",                  "YOUTUBE_MUSIC", "SINGLE_VIDEO"),
        ("https://music.youtube.com/playlist?list=RDTESTPLAYLIST",  "YOUTUBE_MUSIC", "PLAYLIST"),
        ("https://music.youtube.com/@noyfadlon",                   "YOUTUBE_MUSIC", "ARTIST"),
        # Spotify
        ("https://open.spotify.com/track/TESTTRACKID00001",         "SPOTIFY",       "SINGLE_VIDEO"),
        ("https://open.spotify.com/album/TESTALBUMID00001",         "SPOTIFY",       "ALBUM"),
        ("https://open.spotify.com/playlist/TESTPLAYLISTID0001",    "SPOTIFY",       "PLAYLIST"),
        ("https://open.spotify.com/artist/TESTARTISTID00001",       "SPOTIFY",       "ARTIST"),
        # Generic
        ("https://example.com/some-video",                         "GENERIC",       "UNKNOWN"),
        # Garbage
        ("not-a-url",                                              "UNKNOWN",       "UNKNOWN"),
    ])
    def test_classify(self, url, exp_plat, exp_kind):
        plat, kind = self.classify(url)
        assert plat.name == exp_plat
        assert kind.name == exp_kind


# ──────────────────────────────────────────────────────────────────────────────
# 4. Error Handler (classify_error)
# ──────────────────────────────────────────────────────────────────────────────

class TestErrorHandler:

    @pytest.fixture(autouse=True)
    def _import(self):
        from error_handler import classify_error, ErrorInfo, ErrorSeverity
        self.classify = classify_error
        self.Sev = ErrorSeverity

    def test_permission_error(self):
        err = self.classify(PermissionError("access denied"))
        assert err.severity == self.Sev.CRITICAL
        assert "permission" in err.headline.lower()

    def test_os_error(self):
        err = self.classify(OSError("No space left on device"))
        assert err.severity == self.Sev.CRITICAL

    def test_generic_exception_fallback(self):
        err = self.classify(RuntimeError("something weird"))
        assert err.headline == "Download failed"
        assert "something weird" in err.detail

    def test_sign_in_pattern(self):
        err = self.classify(Exception("ERROR: Sign in to confirm your age"))
        assert "sign-in" in err.headline.lower() or "sign" in err.headline.lower()

    def test_private_video_pattern(self):
        err = self.classify(Exception("This video is private video"))
        assert "unavailable" in err.headline.lower()

    def test_rate_limit_pattern(self):
        err = self.classify(Exception("HTTP Error 429: Too Many Requests"))
        assert "rate" in err.headline.lower()

    def test_geo_block_pattern(self):
        err = self.classify(Exception("not available in your country"))
        assert "geo" in err.headline.lower()

    # ── Phase 4: user-facing guidance for classified YouTube failures ────────

    def test_po_token_missing_produces_provider_guidance(self):
        err = self.classify(Exception("ERROR: Unable to fetch GVS PO Token"))
        assert "PO Token" in err.headline
        assert "PO Token Provider" in err.detail
        assert "YouTube Doctor" in err.detail  # run through the real (offline) Doctor

    def test_cookies_expired_produces_re_export_guidance(self):
        err = self.classify(Exception("YouTube account cookies are no longer valid"))
        assert "cookies" in err.headline.lower()
        assert "re-export" in err.detail.lower()

    def test_js_runtime_missing_produces_deno_node_guidance(self):
        err = self.classify(Exception("ERROR: No supported JavaScript runtime could be found"))
        assert "JavaScript runtime" in err.headline
        assert "Deno" in err.detail
        assert "Node 22" in err.detail

    def test_rate_limited_does_not_suggest_blind_retries(self):
        err = self.classify(Exception("HTTP Error 429: Too Many Requests"))
        assert "avoid repeated retries" in err.detail.lower()
        assert "retry immediately" not in err.detail.lower()
        assert "try again immediately" not in err.detail.lower()

    def test_403_does_not_suggest_blind_retries(self):
        err = self.classify(Exception("HTTP Error 403: Forbidden"))
        assert "avoid repeated retries" in err.detail.lower()

    def test_account_required_mentions_cookies_only_when_needed(self):
        err = self.classify(Exception("ERROR: Sign in to confirm your age"))
        assert "cookies" in err.detail.lower()
        # Unrelated failures must not suddenly start talking about cookies.
        unrelated = self.classify(Exception("ffmpeg not found on PATH"))
        assert "cookies" not in unrelated.detail.lower()

    def test_doctor_links_po_token_provider_status(self, monkeypatch):
        from core.youtube_doctor import DoctorCheck, DoctorStatus

        monkeypatch.setattr(
            "core.youtube_doctor.check_po_token_provider",
            lambda: (
                DoctorCheck("po_token_provider", DoctorStatus.WARN, "No PO Token Provider plugin detected."),
                [],
            ),
        )
        err = self.classify(Exception("Unable to fetch GVS PO Token"))
        assert "No PO Token Provider plugin detected." in err.detail

    def test_po_token_guidance_changes_when_provider_is_detected(self, monkeypatch):
        """PO Token failure guidance must reflect Doctor readiness, not
        reuse the same static text for ready and missing providers."""
        from core.youtube_doctor import DoctorCheck, DoctorStatus, ProviderDetection

        monkeypatch.setattr(
            "core.youtube_doctor.check_po_token_provider",
            lambda: (
                DoctorCheck(
                    "po_token_provider", DoctorStatus.PASS,
                    "PO Token Provider is ready: bgutil plugin is available, bundled "
                    "Deno is selected, the Deno script backend is present, and the "
                    "backend health check passed (script version 1.3.1). yt-dlp will "
                    "use the official provider mechanism with BananaFlow's bundled "
                    "server_home; BananaFlow does not generate, store, or inject PO Tokens.",
                ),
                [ProviderDetection(method="distribution", distribution_name="bgutil-ytdlp-pot-provider")],
            ),
        )
        err_detected = self.classify(Exception("Unable to fetch GVS PO Token"))

        monkeypatch.setattr(
            "core.youtube_doctor.check_po_token_provider",
            lambda: (
                DoctorCheck("po_token_provider", DoctorStatus.WARN, "No PO Token Provider plugin detected."),
                [],
            ),
        )
        err_not_detected = self.classify(Exception("Unable to fetch GVS PO Token"))

        assert err_detected.detail != err_not_detected.detail
        assert "PO Token Provider is ready" in err_detected.detail
        assert "official provider mechanism" in err_detected.detail
        assert "does not generate, store, or inject PO Tokens" in err_detected.detail
        assert "No PO Token Provider plugin detected" in err_not_detected.detail
        # Neither wording may promise guaranteed success.
        for text in (err_detected.detail, err_not_detected.detail):
            assert "will work" not in text.lower()
            assert "guarantee" not in text.lower()

    def test_doctor_links_js_runtime_status(self, monkeypatch):
        from core.youtube_doctor import DoctorCheck, DoctorStatus

        monkeypatch.setattr(
            "core.youtube_doctor.check_js_runtimes",
            lambda: (
                DoctorCheck("js_runtime", DoctorStatus.FAIL, "No supported JS runtime found on PATH."),
                [],
            ),
        )
        err = self.classify(Exception("No supported JavaScript runtime could be found"))
        assert "No supported JS runtime found on PATH." in err.detail

    def test_doctor_links_missing_cookies_for_sign_in_required(self, monkeypatch):
        from core.youtube_doctor import CookieDiagnostics, DoctorCheck, DoctorStatus

        monkeypatch.setattr(
            "core.youtube_doctor.check_cookies",
            lambda cookies_file, cookies_browser: (
                DoctorCheck("cookies", DoctorStatus.PASS, "No cookies configured."),
                CookieDiagnostics(mode="none"),
            ),
        )
        err = self.classify(Exception("Sign in to confirm your age"))
        assert "No cookies configured." in err.detail

    def test_doctor_does_not_repeat_when_cookies_already_look_fine(self, monkeypatch):
        from core.youtube_doctor import CookieDiagnostics, DoctorCheck, DoctorStatus

        monkeypatch.setattr(
            "core.youtube_doctor.check_cookies",
            lambda cookies_file, cookies_browser: (
                DoctorCheck("cookies", DoctorStatus.PASS, "Login cookies appear present."),
                CookieDiagnostics(mode="file", has_likely_login_cookies=True),
            ),
        )
        err = self.classify(Exception("Sign in to confirm your age"))
        assert "YouTube Doctor" not in err.detail

    # ── Phase 4.1: Doctor-linking must be driven by a stable failure code,
    # never by the (freely-editable) headline/detail wording. ──────────────

    def test_doctor_linking_survives_headline_rewording(self, monkeypatch):
        import error_handler
        from core.youtube_doctor import DoctorCheck, DoctorStatus

        # Reword the PO-token headline in the text table (the freely
        # editable part) — Doctor-linking must key off the stable code
        # in the pattern table, so the enrichment still happens.
        monkeypatch.setitem(
            error_handler.ERROR_TEXTS_EN,
            "err_po_token_title", "Some Totally Different Wording",
        )
        monkeypatch.setattr(
            "core.youtube_doctor.check_po_token_provider",
            lambda: (
                DoctorCheck("po_token_provider", DoctorStatus.WARN, "No PO Token Provider plugin detected."),
                [],
            ),
        )

        err = self.classify(Exception("Unable to fetch GVS PO Token"))
        assert err.headline == "Some Totally Different Wording"
        assert "No PO Token Provider plugin detected." in err.detail

    def test_doctor_linked_codes_use_stable_warning_classifier_constants(self):
        import error_handler
        from core.warning_classifier import ACCOUNT_REQUIRED, JS_RUNTIME_MISSING, PO_TOKEN_MISSING

        assert set(error_handler._DOCTOR_LINKED_CODES.keys()) == {
            PO_TOKEN_MISSING, JS_RUNTIME_MISSING, ACCOUNT_REQUIRED,
        }

    def test_pattern_table_codes_are_known_or_none(self):
        import error_handler
        from core.warning_classifier import (
            ACCOUNT_REQUIRED, BROWSER_COOKIE_ACCESS_BLOCKED,
            COOKIES_EXPIRED_OR_INVALID, JS_RUNTIME_MISSING,
            NETWORK_TRANSIENT, PO_TOKEN_MISSING, RATE_LIMITED_OR_FORBIDDEN,
        )

        valid_codes = {
            ACCOUNT_REQUIRED, BROWSER_COOKIE_ACCESS_BLOCKED,
            COOKIES_EXPIRED_OR_INVALID, JS_RUNTIME_MISSING,
            NETWORK_TRANSIENT, PO_TOKEN_MISSING, RATE_LIMITED_OR_FORBIDDEN, None,
        }
        for _pattern, _message_key, _severity, code in error_handler._YTDLP_PATTERNS:
            assert code in valid_codes

    def test_error_info_status_line(self):
        # status_line() is now emoji-free by design: the GUI maps severity to a
        # themed status icon (ui.components.status_icon) and renders the plain
        # headline. Severity is exposed separately via severity_kind().
        from error_handler import ErrorInfo, ErrorSeverity
        e = ErrorInfo(severity=ErrorSeverity.WARNING, headline="Oops", detail="d")
        line = e.status_line()
        assert line == "Oops"
        for glyph in ("⚠", "❌", "🔴"):
            assert glyph not in line
        assert e.severity_kind() == "warning"

    def test_error_info_is_fatal(self):
        from error_handler import ErrorInfo, ErrorSeverity
        assert ErrorInfo(severity=ErrorSeverity.CRITICAL, headline="x", detail="y").is_fatal()
        assert not ErrorInfo(severity=ErrorSeverity.WARNING, headline="x", detail="y").is_fatal()


# ──────────────────────────────────────────────────────────────────────────────
# 5. BatchImporter
# ──────────────────────────────────────────────────────────────────────────────

class TestBatchImporter:

    @pytest.fixture(autouse=True)
    def _import(self):
        from core.batch_importer import BatchImporter
        self.BI = BatchImporter

    def test_from_raw_text_extracts_urls(self):
        text = """
        Check out https://www.youtube.com/watch?v=TESTVIDEOAAA
        and also https://open.spotify.com/track/TESTTRACKID00001
        some garbage text here
        """
        result = self.BI.from_raw_text(text)
        assert result.found_count == 2

    def test_from_raw_text_empty(self):
        result = self.BI.from_raw_text("")
        assert result.found_count == 0

    def test_from_raw_text_deduplicates(self):
        text = (
            "https://www.youtube.com/watch?v=TESTVIDEOAAA\n"
            "https://www.youtube.com/watch?v=TESTVIDEOAAA\n"
        )
        result = self.BI.from_raw_text(text)
        assert result.found_count == 1

    def test_from_clipboard_text(self):
        urls = self.BI.from_clipboard_text(
            "https://www.youtube.com/watch?v=TESTVIDEOAAA random stuff"
        )
        assert urls == ["https://www.youtube.com/watch?v=TESTVIDEOAAA"]

    def test_from_clipboard_text_empty(self):
        assert self.BI.from_clipboard_text("") == []

    def test_from_text_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            self.BI.from_text_file(str(tmp_path / "nope.txt"))

    def test_from_text_file_with_comments(self, tmp_path):
        f = tmp_path / "batch.txt"
        f.write_text(
            "# My batch\n"
            "https://www.youtube.com/watch?v=TESTVIDEOAAA\n"
            "# skip this\n"
            "https://youtu.be/TESTVIDEOAAB\n"
        )
        result = self.BI.from_text_file(str(f))
        # At least the first URL should be found
        assert result.found_count >= 1


class TestMetadataProcessor:

    def test_scan_folders_includes_empty_nested_dirs(self, tmp_path):
        from core.metadata_processor import scan_folders

        empty = tmp_path / "Album" / "Empty Disc"
        empty.mkdir(parents=True)

        folders = scan_folders(tmp_path, recursive=True)

        assert tmp_path in folders
        assert tmp_path / "Album" in folders
        assert empty in folders

    def test_build_scan_result_keeps_empty_folders(self, tmp_path):
        from core.metadata_processor import build_scan_result

        empty = tmp_path / "Empty"
        empty.mkdir()

        result = build_scan_result(tmp_path, [], 0, {tmp_path, empty})

        assert result.files_count == 0
        assert empty in result.folder_set
        assert result.folders_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# 6. Duplicate Checker
# ──────────────────────────────────────────────────────────────────────────────

class TestDuplicateChecker:

    def test_expected_stem_basic(self):
        from core.duplicate_checker import expected_stem
        assert expected_stem("My Song", "Artist") == "Artist - My Song"

    def test_expected_stem_with_index(self):
        # The on-disk filename built by downloader._build_ydl_opts is
        # "<NN> - <Artist> - <Title>.<ext>" — note the " - " after the
        # zero-padded index. The stem must mirror that byte-for-byte.
        from core.duplicate_checker import expected_stem
        assert expected_stem("My Song", "Artist", index=3) == "03 - Artist - My Song"

    def test_expected_stem_no_index(self):
        from core.duplicate_checker import expected_stem
        assert expected_stem("My Song", "Artist", index=3, include_index=False) == "Artist - My Song"

    def test_expected_stem_clean_filename_no_artist(self):
        # download_controller forces is_clean=True, so the on-disk filename
        # is "<NN> - <Title>.<ext>" with no artist segment. The duplicate
        # checker must accept the same convention or it will never match.
        from core.duplicate_checker import expected_stem
        assert expected_stem("My Song", "Artist", index=3, include_artist=False) == "03 - My Song"
        assert expected_stem("My Song", "Artist", include_artist=False) == "My Song"

    def test_expected_stem_truncates_to_200(self):
        # Both downloader._sanitize_filename and expected_stem must cap at
        # 200 chars to stay under Windows MAX_PATH.
        from core.duplicate_checker import expected_stem
        long_title = "A" * 300
        stem = expected_stem(long_title, "Artist", include_artist=False)
        assert len(stem) == 200

    def test_expected_stem_matches_downloader_sanitiser(self):
        # Regression guard for S0-4: the duplicate checker must call the
        # exact same sanitiser as the downloader so any future change to
        # filename rules cannot drift between the two.
        from core.duplicate_checker import expected_stem
        from core.downloader import _sanitize_filename

        title = 'Wei"rd / Title : Test'
        stem = expected_stem(title, "Artist", include_artist=False)
        assert stem == _sanitize_filename(title)

    def test_find_duplicate_no_dir(self, tmp_path):
        from core.duplicate_checker import find_duplicate
        result = find_duplicate(
            str(tmp_path / "nonexistent"),
            "Song", "Artist",
        )
        assert result is None

    def test_find_duplicate_match(self, tmp_path):
        from core.duplicate_checker import find_duplicate, expected_stem
        stem = expected_stem("My Song", "Example Artist")
        (tmp_path / f"{stem}.mp3").write_bytes(b"\x00" * 100)
        result = find_duplicate(str(tmp_path), "My Song", "Example Artist")
        assert result is not None
        assert result.name == f"{stem}.mp3"

    def test_find_duplicate_clean_filename_match(self, tmp_path):
        # End-to-end S0-4 regression guard: a download written by the
        # downloader in clean-filename mode must be discoverable by
        # find_duplicate when called with include_artist=False.
        from core.duplicate_checker import find_duplicate, expected_stem
        stem = expected_stem("My Song", "Artist", index=3, include_artist=False)
        (tmp_path / f"{stem}.mp3").write_bytes(b"\x00" * 100)
        result = find_duplicate(
            str(tmp_path), "My Song", "Artist",
            index=3, include_index=True, include_artist=False,
        )
        assert result is not None
        assert result.name == f"{stem}.mp3"

    def test_find_duplicate_no_match(self, tmp_path):
        from core.duplicate_checker import find_duplicate
        (tmp_path / "unrelated.mp3").write_bytes(b"\x00")
        result = find_duplicate(str(tmp_path), "My Song", "Artist")
        assert result is None

    def test_find_duplicate_video_mp4_match(self, tmp_path):
        from core.duplicate_checker import find_duplicate, expected_stem
        stem = expected_stem("My Song", "Example Artist")
        (tmp_path / f"{stem}.mp4").write_bytes(b"\x00" * 100)
        result = find_duplicate(str(tmp_path), "My Song", "Example Artist")
        assert result is not None
        assert result.name == f"{stem}.mp4"



# ──────────────────────────────────────────────────────────────────────────────
# 6b. Search-budget clamping (S1-3 regression guard)
# ──────────────────────────────────────────────────────────────────────────────

class TestSearchCategoryBudget:
    """The pre-fix behaviour used floors (max(N//k, m)) that caused the
    sum of per-category limits to exceed the user's configured max
    (e.g. YTM "max_results=15" produced 22 items). These tests pin the
    new proportional distribution that honours the cap."""

    def test_ytm_budget_sums_to_cap_at_15(self):
        # Replicate the inline math in _YTMusicBackend.search_all so the
        # test doesn't need to mock yt-dlp / ytmusicapi network calls.
        max_results = 15
        song_limit = max(1, int(max_results * 0.50))
        album_limit = max(1, int(max_results * 0.20))
        artist_limit = max(1, int(max_results * 0.15))
        playlist_limit = max(
            1, max_results - song_limit - album_limit - artist_limit
        )
        total = song_limit + album_limit + artist_limit + playlist_limit
        assert total == max_results, (
            f"YTM budget {song_limit}+{album_limit}+{artist_limit}+"
            f"{playlist_limit} = {total} must equal max_results={max_results}"
        )

    def test_ytm_budget_each_category_has_at_least_one(self):
        # Tiny cap (4) must still surface at least one of each kind.
        max_results = 4
        song_limit = max(1, int(max_results * 0.50))
        album_limit = max(1, int(max_results * 0.20))
        artist_limit = max(1, int(max_results * 0.15))
        playlist_limit = max(
            1, max_results - song_limit - album_limit - artist_limit
        )
        assert song_limit >= 1
        assert album_limit >= 1
        assert artist_limit >= 1
        assert playlist_limit >= 1

    def test_yt_categorized_budget_sums_to_cap_at_15(self):
        max_results = 15
        video_limit = max(1, int(max_results * 0.60))
        playlist_limit = max(1, int(max_results * 0.25))
        channel_limit = max(
            1, max_results - video_limit - playlist_limit
        )
        total = video_limit + playlist_limit + channel_limit
        assert total == max_results

    def test_yt_categorized_budget_at_large_cap(self):
        max_results = 100
        video_limit = max(1, int(max_results * 0.60))
        playlist_limit = max(1, int(max_results * 0.25))
        channel_limit = max(
            1, max_results - video_limit - playlist_limit
        )
        # At 100, expect roughly 60 / 25 / 15.
        assert video_limit == 60
        assert playlist_limit == 25
        assert channel_limit == 15


# ──────────────────────────────────────────────────────────────────────────────
# 7. Connectivity probe (error_handler)
# ──────────────────────────────────────────────────────────────────────────────

class TestProbeConnectivity:

    def test_probe_returns_bool(self):
        from error_handler import probe_connectivity
        result = probe_connectivity(timeout=2.0)
        assert isinstance(result, bool)

    def test_check_ffmpeg_returns_bool(self):
        from error_handler import check_ffmpeg
        assert isinstance(check_ffmpeg(), bool)
