"""
tests/test_options_bar_persistence.py  –  OptionsBar write-back & model tests
=================================================================================
The OptionsBar always read type/format/quality from AppConfig on startup but
never wrote user changes back, so a user who switched to FLAC found mp3
again after every restart. These tests pin the fix, plus the Type/Format/
Quality redesign that replaced the old "Format" (mp3/mp4 media-mode switch)
and "Codec" (audio output format) controls:

* changing type / audio-format / quality persists to config.json,
* audio and video quality are remembered independently,
* flipping audio -> video -> audio restores the user's saved audio format
  and audio quality,
* a fresh OptionsBar starts from the saved values (full round-trip),
* apply_config() itself never dirties the config file,
* the Type combo's *values* are "audio"/"video" (never "mp3"/"mp4"),
* the audio-format combo displays uppercase text ("MP3") but persists/
  compares on the lowercase value ("mp3") via currentData(),
* no primary control is labeled "Codec".

Quality is persisted as a stable preset ID (e.g. "audio_mp3_320",
"video_1080"), never the translated on-screen text. Tests below select
quality by that ID (via itemData/findData), not by display text, so they
do not depend on the active UI language. The Type and audio-format combos
follow the same discipline: text may be translated/uppercased, but every
comparison, config write, and get_options() value goes through
currentData().

Headless (QT_QPA_PLATFORM=offscreen); skips when PySide6 is missing.
"""

from __future__ import annotations

import os

import pytest


def _make_bar(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    # get_app_data_dir()'s Linux branch checks $XDG_CONFIG_HOME *before*
    # falling back to Path.home() — on a machine/CI runner where that's
    # set (unlike Windows, which only ever looks at %APPDATA%), the HOME
    # monkeypatch above is silently ignored and every test in this file
    # reads/writes the same real config.json instead of one confined to
    # tmp_path. This showed up as real, order-dependent cross-test
    # failures the first time this suite ran on ubuntu-latest CI
    # (Phase 5) — never on Windows, where the leak path doesn't exist.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    try:
        from PySide6.QtWidgets import QApplication
        from config import AppConfig
        from ui.panels.options_bar import OptionsBar
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")

    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    bar = OptionsBar(cfg)
    return app, cfg, bar


def _select_by_data(combo, value) -> None:
    """Select a combo item by its stable itemData value, not its
    (possibly translated or uppercased) display text — the Type,
    audio-format, and quality combos all persist/compare on data only."""
    idx = combo.findData(value)
    assert idx >= 0, f"{value!r} not in combo item data"
    combo.setCurrentIndex(idx)


def _reload_config(tmp_path, monkeypatch):
    from config import AppConfig
    return AppConfig()


class TestOptionsBarPersistence:

    def test_type_change_persists(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._type_combo, "video")
            assert _reload_config(tmp_path, monkeypatch).media_type == "video"
        finally:
            bar.deleteLater()

    def test_quality_is_remembered_per_type(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._quality_combo, "audio_mp3_128")
            _select_by_data(bar._type_combo, "video")
            _select_by_data(bar._quality_combo, "video_720")
            fresh = _reload_config(tmp_path, monkeypatch)
            assert fresh.audio_quality == "audio_mp3_128"
            assert fresh.video_quality == "video_720"
        finally:
            bar.deleteLater()

    def test_audio_format_change_persists(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._audio_format_combo, "flac")
            assert _reload_config(tmp_path, monkeypatch).audio_format == "flac"
        finally:
            bar.deleteLater()

    def test_type_flip_restores_saved_audio_quality(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._quality_combo, "audio_mp3_192")
            _select_by_data(bar._type_combo, "video")
            _select_by_data(bar._type_combo, "audio")
            assert bar._quality_combo.currentData() == "audio_mp3_192"
        finally:
            bar.deleteLater()

    def test_type_flip_restores_saved_audio_format(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._audio_format_combo, "flac")
            _select_by_data(bar._type_combo, "video")
            _select_by_data(bar._type_combo, "audio")
            assert bar._audio_format_combo.currentData() == "flac"
        finally:
            bar.deleteLater()

    def test_fresh_bar_starts_from_saved_config(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._type_combo, "video")
            _select_by_data(bar._quality_combo, "video_1080")
        finally:
            bar.deleteLater()

        from config import AppConfig
        from ui.panels.options_bar import OptionsBar
        bar2 = OptionsBar(AppConfig())
        try:
            assert bar2._type_combo.currentData() == "video"
            assert bar2._quality_combo.currentData() == "video_1080"
        finally:
            bar2.deleteLater()

    def test_apply_config_does_not_dirty_the_file(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            cfg_path = cfg._path
            before = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
            bar.apply_config(cfg)
            after = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
            assert before == after
        finally:
            bar.deleteLater()

    def test_unrecognized_quality_key_falls_back_gracefully(self, tmp_path, monkeypatch):
        """Translating legacy display-string values (from before quality
        became a stable key) is config_migrate.py's job, run once on JSON
        load — see test_core.py's migration-5 tests. This only guards
        OptionsBar's own defensive fallback: if AppConfig ever holds a
        value that isn't a current combo key for any reason, the combo
        must fall back to its first item instead of clearing/crashing."""
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            cfg.audio_quality = "not-a-real-key"
            bar.apply_config(cfg)
            assert bar._quality_combo.currentIndex() >= 0
            assert bar._quality_combo.currentData() == "audio_mp3_320"
        finally:
            bar.deleteLater()

    def test_invalid_menu_index_does_not_clear_combo(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            before = bar._quality_combo.currentText()
            bar._quality_combo._onItemClicked(-1)
            assert bar._quality_combo.currentText() == before
            assert bar._quality_combo.currentIndex() >= 0
        finally:
            bar.deleteLater()

    def test_audio_format_switch_preserves_quality_per_format(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._quality_combo, "audio_mp3_128")
            _select_by_data(bar._audio_format_combo, "m4a")
            _select_by_data(bar._quality_combo, "audio_m4a_192")
            _select_by_data(bar._audio_format_combo, "opus")
            _select_by_data(bar._quality_combo, "audio_opus_96")

            _select_by_data(bar._audio_format_combo, "mp3")
            assert bar._quality_combo.currentData() == "audio_mp3_128"
            _select_by_data(bar._audio_format_combo, "m4a")
            assert bar._quality_combo.currentData() == "audio_m4a_192"
            _select_by_data(bar._audio_format_combo, "opus")
            assert bar._quality_combo.currentData() == "audio_opus_96"

            fresh = _reload_config(tmp_path, monkeypatch)
            assert fresh.audio_quality_by_codec["mp3"] == "audio_mp3_128"
            assert fresh.audio_quality_by_codec["m4a"] == "audio_m4a_192"
            assert fresh.audio_quality_by_codec["opus"] == "audio_opus_96"
        finally:
            bar.deleteLater()

    def test_flac_replaces_bitrate_choices_with_source_quality(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._audio_format_combo, "flac")
            assert bar._quality_combo.count() == 1
            assert bar._quality_combo.currentData() == "audio_flac_source"
            assert "320" not in bar._quality_combo.currentText()
        finally:
            bar.deleteLater()

    def test_audio_format_control_visible_only_in_audio_mode(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            assert not bar._audio_format_group.isHidden()
            _select_by_data(bar._type_combo, "video")
            assert bar._audio_format_group.isHidden()
            _select_by_data(bar._type_combo, "audio")
            assert not bar._audio_format_group.isHidden()
        finally:
            bar.deleteLater()

    def test_audio_format_label_uses_format_key(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            from ui.i18n import t
            assert bar._lbl_audio_format.text() == t("options_format_label").rstrip(":").strip()
        finally:
            bar.deleteLater()

    def test_no_control_labeled_codec(self, tmp_path, monkeypatch):
        """No primary download control may be labeled 'Codec' — the word
        must not appear anywhere in the options bar's visible labels."""
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            for lbl in bar._labels:
                assert "codec" not in lbl.text().lower()
                assert "קודק" not in lbl.text()
        finally:
            bar.deleteLater()

    def test_type_combo_values_are_audio_video_not_mp3_mp4(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            datas = [bar._type_combo.itemData(i) for i in range(bar._type_combo.count())]
            assert datas == ["audio", "video"]
            texts = [bar._type_combo.itemText(i) for i in range(bar._type_combo.count())]
            assert "mp3" not in texts
            assert "mp4" not in texts
        finally:
            bar.deleteLater()

    def test_audio_format_display_uppercase_data_lowercase(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._audio_format_combo, "flac")
            assert bar._audio_format_combo.currentText() == "FLAC"
            assert bar._audio_format_combo.currentData() == "flac"
        finally:
            bar.deleteLater()

    def test_get_options_returns_media_type_and_video_format(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            opts = bar.get_options()
            assert opts["media_type"] == "audio"
            assert opts["audio_format"] == "mp3"
            assert opts["video_format"] == "mp4"

            _select_by_data(bar._type_combo, "video")
            opts = bar.get_options()
            assert opts["media_type"] == "video"
            assert opts["video_format"] == "mp4"
        finally:
            bar.deleteLater()

    def test_control_order_is_type_format_quality(self, tmp_path, monkeypatch):
        """Visible pill order must be Type -> Format -> Quality (the row's
        widget add-order — see options_bar.py's _build() docstring on RTL)."""
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            row = bar.layout()
            pills = [row.itemAt(i).widget() for i in range(row.count())]
            type_pos = pills.index(bar._type_group)
            format_pos = pills.index(bar._audio_format_group)
            quality_pos = pills.index(bar._quality_group)
            assert type_pos < format_pos < quality_pos
        finally:
            bar.deleteLater()

    def test_quality_width_tracks_current_media_type(self, tmp_path, monkeypatch):
        from ui.i18n import current_language, set_language

        previous_language = current_language()
        bar = None
        try:
            set_language("he")
            _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
            audio_width = bar._quality_value_lbl.minimumWidth()

            _select_by_data(bar._type_combo, "video")
            video_width = bar._quality_value_lbl.minimumWidth()

            _select_by_data(bar._type_combo, "audio")
            assert bar._quality_value_lbl.minimumWidth() == audio_width
            assert audio_width < video_width
        finally:
            set_language(previous_language)
            if bar is not None:
                bar.deleteLater()


class TestQualityKeyValidity:
    """Each quality combo item's stable ID must resolve through the central
    registry. Translated display text must never be a logical key."""

    def test_audio_quality_keys_resolve_to_distinct_qualities(self):
        from core.quality_presets import AUDIO_QUALITY_PRESETS
        keys = [preset.id for preset in AUDIO_QUALITY_PRESETS]
        resolved = [preset.quality for preset in AUDIO_QUALITY_PRESETS]
        assert len(set(resolved)) == len(resolved)
        assert len(keys) == len(set(keys))

    def test_video_quality_keys_resolve_to_distinct_qualities(self):
        from core.quality_presets import VIDEO_QUALITY_PRESETS
        keys = [preset.id for preset in VIDEO_QUALITY_PRESETS]
        resolved = [preset.quality for preset in VIDEO_QUALITY_PRESETS]
        assert len(set(resolved)) == len(resolved)
        assert len(keys) == len(set(keys))

    def test_live_audio_combo_items_carry_valid_keys(self, tmp_path, monkeypatch):
        from core.quality_presets import audio_preset_from_id
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            keys = [bar._quality_combo.itemData(i) for i in range(bar._quality_combo.count())]
            assert keys == ["audio_mp3_320", "audio_mp3_256", "audio_mp3_192", "audio_mp3_128"]
            assert all(audio_preset_from_id(k, "mp3") for k in keys)
        finally:
            bar.deleteLater()

    def test_live_video_combo_items_carry_valid_keys(self, tmp_path, monkeypatch):
        from core.quality_presets import video_preset_from_id
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._type_combo, "video")
            keys = [bar._quality_combo.itemData(i) for i in range(bar._quality_combo.count())]
            assert keys == [
                "video_best", "video_2160", "video_1440", "video_1080",
                "video_720", "video_480", "video_360", "video_smallest",
            ]
            assert all(video_preset_from_id(k) for k in keys)
        finally:
            bar.deleteLater()

    def test_m4a_and_opus_combo_orders_are_format_specific(self, tmp_path, monkeypatch):
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._audio_format_combo, "m4a")
            assert [bar._quality_combo.itemData(i) for i in range(bar._quality_combo.count())] == [
                "audio_m4a_best", "audio_m4a_256", "audio_m4a_192", "audio_m4a_128",
            ]
            _select_by_data(bar._audio_format_combo, "opus")
            assert [bar._quality_combo.itemData(i) for i in range(bar._quality_combo.count())] == [
                "audio_opus_best", "audio_opus_192", "audio_opus_160",
                "audio_opus_128", "audio_opus_96",
            ]
        finally:
            bar.deleteLater()

    def test_display_text_not_used_as_persisted_value(self, tmp_path, monkeypatch):
        from ui.i18n import current_language, set_language

        previous_language = current_language()
        bar = None
        try:
            set_language("he")
            _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
            _select_by_data(bar._quality_combo, "audio_mp3_256")
            opts = bar.get_options()
            assert opts["quality_label"] == "audio_mp3_256"
            assert opts["quality_label"] != bar._quality_combo.currentText()
        finally:
            set_language(previous_language)
            if bar is not None:
                bar.deleteLater()

    def test_audio_format_display_text_not_used_as_persisted_value(self, tmp_path, monkeypatch):
        """Even though English audio-format display text and data happen to
        collide in lowercase form ("mp3" == "mp3"), the uppercase display
        form must never leak into get_options() or config."""
        _app, cfg, bar = _make_bar(tmp_path, monkeypatch)
        try:
            _select_by_data(bar._audio_format_combo, "opus")
            opts = bar.get_options()
            assert opts["audio_format"] == "opus"
            assert opts["audio_format"] != bar._audio_format_combo.currentText()
        finally:
            bar.deleteLater()

    def test_quality_display_isolates_latin_suffixes_for_rtl_text(self):
        from ui.i18n import current_language, set_language, t
        from ui.direction import (
            _LTR_ISOLATE,
            _POP_DIRECTIONAL_ISOLATE,
            quality_display,
        )

        previous_language = current_language()
        try:
            set_language("he")
            audio_label = quality_display("quality_best", "quality_audio_320")
            assert audio_label == (
                f"{t('quality_best')} · "
                f"{_LTR_ISOLATE}320 kbps{_POP_DIRECTIONAL_ISOLATE}"
            )
            assert "spbk" not in audio_label

            video_label = quality_display("quality_video_4k", "quality_video_2160")
            assert video_label == (
                f"{_LTR_ISOLATE}4K{_POP_DIRECTIONAL_ISOLATE} · "
                f"{_LTR_ISOLATE}2160p{_POP_DIRECTIONAL_ISOLATE}"
            )
            assert "p0612" not in video_label
        finally:
            set_language(previous_language)
