"""
tests/test_youtube_fast_mode_toggle.py  –  Phase 3.1 fast-mode UI toggle
=========================================================================
youtube_reliability_mode has always been a real, tested config property
("conservative"/"fast"), but had no UI — README documented it as
"config-only, no UI toggle yet". This adds a small SwitchSettingCard on
the Expert & Diagnostics page next to YouTube Doctor, since it's a
risk-accepting power setting, not a everyday one.

These tests pin: the card reflects the saved config on build/refresh,
toggling it persists through the typed property (not the generic
dict-key _persist(), which would bypass the "conservative"/"fast"
string validation), and it survives a restart.
"""

from __future__ import annotations

import os

import pytest


def _make_panel(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    try:
        from PySide6.QtWidgets import QApplication
        from config import AppConfig
        from ui.panels.settings_panel import SettingsPanel
        from ui.theme_manager import ThemeManager
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")

    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    panel = SettingsPanel(config=cfg, theme=ThemeManager(cfg))
    return app, cfg, panel


class TestYoutubeFastModeToggle:

    def test_defaults_to_off_matching_conservative_default(self, tmp_path, monkeypatch):
        _app, cfg, panel = _make_panel(tmp_path, monkeypatch)
        try:
            assert cfg.youtube_reliability_mode == "conservative"
            assert panel._youtube_fast_mode_card.isChecked() is False
        finally:
            panel.deleteLater()

    def test_toggling_on_persists_fast_via_typed_property(self, tmp_path, monkeypatch):
        _app, cfg, panel = _make_panel(tmp_path, monkeypatch)
        try:
            panel._youtube_fast_mode_card.setChecked(True)
            assert cfg.youtube_reliability_mode == "fast"

            from config import AppConfig
            assert AppConfig().youtube_reliability_mode == "fast"
        finally:
            panel.deleteLater()

    def test_toggling_off_restores_conservative(self, tmp_path, monkeypatch):
        _app, cfg, panel = _make_panel(tmp_path, monkeypatch)
        try:
            panel._youtube_fast_mode_card.setChecked(True)
            panel._youtube_fast_mode_card.setChecked(False)
            assert cfg.youtube_reliability_mode == "conservative"
        finally:
            panel.deleteLater()

    def test_refresh_reflects_externally_changed_config(self, tmp_path, monkeypatch):
        _app, cfg, panel = _make_panel(tmp_path, monkeypatch)
        try:
            cfg.youtube_reliability_mode = "fast"
            cfg.save()
            panel.refresh()
            assert panel._youtube_fast_mode_card.isChecked() is True
        finally:
            panel.deleteLater()

    def test_card_is_on_expert_page(self, tmp_path, monkeypatch):
        _app, cfg, panel = _make_panel(tmp_path, monkeypatch)
        try:
            w = panel._youtube_fast_mode_card
            while w is not None and w is not panel._section_pages["expert"]:
                w = w.parentWidget()
            assert w is panel._section_pages["expert"]
        finally:
            panel.deleteLater()

    def test_title_and_desc_are_translated(self):
        from ui.i18n import TRANSLATIONS
        for key in ("youtube_fast_mode_title", "youtube_fast_mode_desc"):
            assert key in TRANSLATIONS["en"]
            assert key in TRANSLATIONS["he"]
            assert TRANSLATIONS["en"][key] != TRANSLATIONS["he"][key]
