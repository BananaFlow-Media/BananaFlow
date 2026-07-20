"""
tests/test_settings_sections.py  –  Phase 3 settings restructure guard
======================================================================
The Settings panel is split into three segmented pages — Basic /
Advanced / Expert & Diagnostics — so a non-technical user meets only
the everyday options first. These tests pin:

* all three pages exist and the segmented nav switches between them,
* every setting card survived the split (nothing was dropped),
* each card lives on the page the product design assigns it to,
* the panel still builds and refreshes cleanly in Hebrew (RTL).

Headless (QT_QPA_PLATFORM=offscreen); skips when PySide6 is missing,
mirroring tests/test_youtube_doctor_gui.py.
"""

from __future__ import annotations

import os

import pytest


# Every card attribute the panel exposed before the split — the split may
# move cards between pages but must never lose one.
ALL_CARDS = (
    "_theme_card", "_lang_card", "_a11y_card",
    "_thumb_card", "_meta_card", "_parallel_card",
    "_subfolder_card", "_singles_subfolder_card", "_index_card", "_dup_card",
    "_clip_card", "_update_card", "_browser_card",
    "_tray_card", "_hotkeys_card",
    "_sb_card", "_mb_card", "_lyrics_card", "_rg_card", "_sq_card", "_expand_card",
    "_cookies_card", "_clear_cookies_card", "_login_fix_card",
    "_youtube_doctor_card", "_youtube_fast_mode_card",
    "_check_app_updates_card", "_check_component_updates_card",
    "_youtube_results_card", "_spotify_results_card",
    "_spotify_proxy_card", "_spotify_proxy_token_card", "_youtube_proxy_card",
)

# Product design: which page each representative card belongs to.
PAGE_OF_CARD = {
    # Basic — appearance, language, accessibility, downloads, sign-in help
    "_theme_card": "basic",
    "_lang_card": "basic",
    "_a11y_card": "basic",
    "_thumb_card": "basic",
    "_parallel_card": "basic",
    "_login_fix_card": "basic",
    # Advanced — playlist, features, system, audio processing, search/proxies
    "_dup_card": "advanced",
    "_clip_card": "advanced",
    "_browser_card": "advanced",
    "_tray_card": "advanced",
    "_sb_card": "advanced",
    "_youtube_results_card": "advanced",
    "_youtube_proxy_card": "advanced",
    # Expert — diagnostics, manual update checks, cookie files, About
    "_youtube_doctor_card": "expert",
    "_youtube_fast_mode_card": "expert",
    "_check_app_updates_card": "expert",
    "_check_component_updates_card": "expert",
    "_cookies_card": "expert",
    "_clear_cookies_card": "expert",
}


def _make_panel(tmp_path, monkeypatch, lang: str = "en"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    try:
        from PySide6.QtWidgets import QApplication
        from config import AppConfig
        from ui.i18n import set_language
        from ui.panels.settings_panel import SettingsPanel
        from ui.theme_manager import ThemeManager
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")

    app = QApplication.instance() or QApplication([])
    set_language(lang)
    cfg = AppConfig()
    cfg.set("language", lang)
    panel = SettingsPanel(config=cfg, theme=ThemeManager(cfg))
    return app, panel


def _page_of(panel, widget) -> str | None:
    w = widget
    while w is not None:
        for key, page in panel._section_pages.items():
            if w is page:
                return key
        w = w.parentWidget()
    return None


class TestSettingsSections:

    def test_three_pages_exist_and_nav_switches(self, tmp_path, monkeypatch):
        app, panel = _make_panel(tmp_path, monkeypatch)
        try:
            assert set(panel._section_pages) == {"basic", "advanced", "expert"}
            assert panel._stack.currentWidget() is panel._section_pages["basic"]
            for key in ("advanced", "expert", "basic"):
                panel._section_nav.setCurrentItem(key)
                app.processEvents()
                assert panel._stack.currentWidget() is panel._section_pages[key]
        finally:
            panel.deleteLater()

    def test_no_setting_card_was_lost_in_the_split(self, tmp_path, monkeypatch):
        _app, panel = _make_panel(tmp_path, monkeypatch)
        try:
            missing = [a for a in ALL_CARDS if not hasattr(panel, a)]
            assert not missing, f"cards lost in the Basic/Advanced/Expert split: {missing}"
        finally:
            panel.deleteLater()

    def test_cards_are_on_their_designed_page(self, tmp_path, monkeypatch):
        _app, panel = _make_panel(tmp_path, monkeypatch)
        try:
            wrong = {
                attr: _page_of(panel, getattr(panel, attr))
                for attr, expected in PAGE_OF_CARD.items()
                if _page_of(panel, getattr(panel, attr)) != expected
            }
            assert not wrong, f"cards on the wrong settings page: {wrong}"
        finally:
            panel.deleteLater()

    def test_hebrew_panel_builds_and_refreshes(self, tmp_path, monkeypatch):
        app, panel = _make_panel(tmp_path, monkeypatch, lang="he")
        try:
            panel.refresh()
            panel._adjust_layouts()
            app.processEvents()
            assert set(panel._section_pages) == {"basic", "advanced", "expert"}
        finally:
            panel.deleteLater()
            from ui.i18n import set_language
            set_language("en")

    def test_section_names_are_translated(self, tmp_path, monkeypatch):
        from ui.i18n import TRANSLATIONS
        for key in ("settings_section_basic", "settings_section_advanced",
                    "settings_section_expert", "signin_group"):
            assert key in TRANSLATIONS["en"], f"missing EN {key}"
            assert key in TRANSLATIONS["he"], f"missing HE {key}"
            assert TRANSLATIONS["en"][key] != TRANSLATIONS["he"][key], (
                f"{key}: Hebrew value looks untranslated"
            )

    def test_spotify_proxy_secret_is_masked(self, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QLineEdit

        _app, panel = _make_panel(tmp_path, monkeypatch)
        try:
            assert (
                panel._spotify_proxy_token_card._edit.echoMode()
                == QLineEdit.EchoMode.Password
            )
        finally:
            panel.deleteLater()
