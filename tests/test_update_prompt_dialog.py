"""
tests/test_update_prompt_dialog.py  –  Update prompt content & decisions
=========================================================================
The prompt's body text and update-id bookkeeping are pure functions, so
most of this file needs no QApplication. A separate lightweight Qt smoke
class (offscreen, skipped when PySide6 is unavailable) covers actual
widget construction and the decision handlers, mirroring
tests/test_youtube_doctor_gui.py.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from core.component_updates import ComponentStatus
from core.update_checker import CURRENT_VERSION, ReleaseInfo
from core.update_state import UpdateStateStore
from ui.dialogs.update_prompt_dialog import (
    REMIND_CHOICES,
    build_update_prompt_text,
    collect_update_ids,
)
from ui.i18n import TRANSLATIONS, t


def _release(version: str = "9.9.9") -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        release_url=f"https://github.com/x/y/releases/tag/v{version}",
        release_notes="## Notes\n- Fixed things",
        published_at="2026-07-01T00:00:00Z",
    )


def _component(key: str = "yt-dlp", cur: str = "2026.6.9", new: str = "2026.8.1") -> ComponentStatus:
    return ComponentStatus(
        key=key, display_name=key,
        installed_version=cur, latest_version=new,
        update_available=True, check_ok=True,
    )


def _store(tmp_path) -> UpdateStateStore:
    return UpdateStateStore(
        path=tmp_path / "update_state.json",
        now=lambda: datetime(2026, 7, 5, tzinfo=timezone.utc),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestCollectUpdateIds:

    def test_app_and_components_each_get_an_id(self):
        ids = collect_update_ids(_release("9.9.9"), [_component("yt-dlp", new="2026.8.1")])
        assert ids == ["app:9.9.9", "component:yt-dlp:2026.8.1"]

    def test_app_only(self):
        assert collect_update_ids(_release("9.9.9"), []) == ["app:9.9.9"]

    def test_components_only(self):
        ids = collect_update_ids(None, [_component(), _component("yt-dlp-ejs", "0.8.0", "0.9.0")])
        assert ids == ["component:yt-dlp:2026.8.1", "component:yt-dlp-ejs:0.9.0"]


class TestBuildUpdatePromptText:

    def test_app_update_shows_both_versions(self):
        text = build_update_prompt_text(_release("9.9.9"), [], frozen=False)
        assert "v9.9.9" in text
        assert CURRENT_VERSION in text

    def test_app_update_is_honest_about_opening_download_page(self):
        # Button only opens the release page — the body must say so, and
        # must not imply BananaFlow self-installs the app update.
        text = build_update_prompt_text(_release("9.9.9"), [], frozen=False)
        assert t("update_prompt_app_note") in text
        assert t("update_get_app_btn") == "Open Download Page"

    def test_component_update_shows_installed_and_latest(self):
        text = build_update_prompt_text(None, [_component()], frozen=False)
        assert "yt-dlp" in text
        assert "2026.6.9" in text
        assert "2026.8.1" in text

    def test_source_mode_explains_pip_and_restart(self):
        text = build_update_prompt_text(None, [_component()], frozen=False)
        assert t("update_prompt_component_note") in text
        assert t("update_prompt_frozen_note") not in text

    def test_frozen_mode_explains_verified_overlay_update(self):
        text = build_update_prompt_text(None, [_component()], frozen=True)
        assert t("update_prompt_frozen_note") in text
        assert t("update_prompt_component_note") not in text

    def test_release_notes_are_summarised_without_markdown(self):
        text = build_update_prompt_text(_release(), [], frozen=False)
        assert "Fixed things" in text
        assert "##" not in text

    def test_combined_prompt_shows_one_recommendation_app_update(self):
        """App update is the primary path: when both an app release and
        outdated components exist, components are folded into the app
        update message — no component version table, no pip note, no
        second competing recommendation."""
        text = build_update_prompt_text(_release("9.9.9"), [_component()], frozen=False)
        assert "v9.9.9" in text
        assert t("update_prompt_app_includes_components").format(names="yt-dlp") in text
        assert t("update_prompt_components_heading") not in text
        assert t("update_prompt_component_note") not in text
        assert t("update_prompt_frozen_note") not in text

    def test_combined_prompt_folds_components_in_frozen_mode_too(self):
        text = build_update_prompt_text(_release("9.9.9"), [_component()], frozen=True)
        assert t("update_prompt_app_includes_components").format(names="yt-dlp") in text
        assert t("update_prompt_frozen_note") not in text


class TestI18nKeysComplete:
    """Every key the update UI references must exist in both languages."""

    KEYS = [
        "updates_group", "update_check_btn",
        "check_app_updates_title", "check_app_updates_desc",
        "check_component_updates_title", "check_component_updates_desc",
        "update_check_failed_title", "update_check_failed_msg",
        "up_to_date_title", "app_up_to_date_msg", "components_up_to_date_msg",
        "update_prompt_title", "update_prompt_subtitle",
        "update_prompt_app_line", "update_prompt_app_note",
        "update_prompt_app_includes_components",
        "update_prompt_components_heading",
        "update_prompt_component_line", "update_prompt_component_note",
        "update_prompt_frozen_note",
        "update_get_app_btn", "update_components_btn", "update_open_releases_btn",
        "update_remind_btn", "update_skip_btn",
        "component_install_running", "component_install_ok_msg",
        "component_install_failed_msg",
    ]

    def test_update_keys_exist_in_both_languages(self):
        for key in self.KEYS:
            assert key in TRANSLATIONS["en"], key
            assert key in TRANSLATIONS["he"], key

    def test_remind_choice_keys_exist_in_both_languages(self):
        for key, _days in REMIND_CHOICES:
            assert key in TRANSLATIONS["en"], key
            assert key in TRANSLATIONS["he"], key


# ──────────────────────────────────────────────────────────────────────────────
# Qt smoke tests (offscreen; skipped without PySide6)
# ──────────────────────────────────────────────────────────────────────────────

def _make_qapp(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class TestUpdatePromptDialogWidget:

    def test_skip_dismisses_every_shown_update(self, tmp_path, monkeypatch):
        try:
            _make_qapp(tmp_path, monkeypatch)
            from ui.dialogs.update_prompt_dialog import UpdatePromptDialog
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        store = _store(tmp_path)
        dlg = UpdatePromptDialog(store, _release("9.9.9"), [_component()])
        try:
            dlg._on_skip()
            assert not store.should_notify("app:9.9.9")
            assert not store.should_notify("component:yt-dlp:2026.8.1")
            # A strictly newer future version still notifies.
            assert store.should_notify("app:10.0.0")
        finally:
            dlg.deleteLater()

    def test_remind_later_snoozes_and_next_launch_records_nothing(self, tmp_path, monkeypatch):
        try:
            _make_qapp(tmp_path, monkeypatch)
            from ui.dialogs.update_prompt_dialog import UpdatePromptDialog
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        store = _store(tmp_path)
        dlg = UpdatePromptDialog(store, _release("9.9.9"), [])
        try:
            dlg._on_remind(3.0)
            assert not store.should_notify("app:9.9.9")   # snoozed
        finally:
            dlg.deleteLater()

        store2 = _store(tmp_path / "second")
        dlg2 = UpdatePromptDialog(store2, _release("9.9.9"), [])
        try:
            dlg2._on_remind(None)                          # "on next launch"
            assert store2.should_notify("app:9.9.9")       # nothing persisted
        finally:
            dlg2.deleteLater()

    def test_frozen_component_prompt_offers_verified_component_install(self, tmp_path, monkeypatch):
        try:
            _make_qapp(tmp_path, monkeypatch)
            import ui.dialogs.update_prompt_dialog as upd
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        monkeypatch.setattr(upd, "can_update_in_place", lambda: True)
        monkeypatch.setattr(upd, "is_frozen", lambda: True)
        dlg = upd.UpdatePromptDialog(_store(tmp_path), None, [_component()])
        try:
            assert hasattr(dlg, "_install_components_btn")
            assert t("update_prompt_frozen_note") in dlg._body_lbl.text()
        finally:
            dlg.deleteLater()

    def test_source_component_prompt_offers_component_install(self, tmp_path, monkeypatch):
        try:
            _make_qapp(tmp_path, monkeypatch)
            import ui.dialogs.update_prompt_dialog as upd
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        monkeypatch.setattr(upd, "can_update_in_place", lambda: True)
        dlg = upd.UpdatePromptDialog(_store(tmp_path), None, [_component()])
        try:
            assert hasattr(dlg, "_install_components_btn")
        finally:
            dlg.deleteLater()

    def test_app_update_suppresses_component_button_even_in_source_mode(self, tmp_path, monkeypatch):
        """One main recommendation: with an app release on offer, the pip
        button must not appear as a competing action — not even for a
        source checkout where the pip path would technically work."""
        try:
            _make_qapp(tmp_path, monkeypatch)
            import ui.dialogs.update_prompt_dialog as upd
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        monkeypatch.setattr(upd, "can_update_in_place", lambda: True)
        dlg = upd.UpdatePromptDialog(_store(tmp_path), _release("9.9.9"), [_component()])
        try:
            assert not hasattr(dlg, "_install_components_btn")
            assert t("update_prompt_app_includes_components").format(names="yt-dlp") \
                in dlg._body_lbl.text()
        finally:
            dlg.deleteLater()

    def test_app_update_opens_official_download_page_not_github(self, tmp_path, monkeypatch):
        try:
            _make_qapp(tmp_path, monkeypatch)
            import ui.dialogs.update_prompt_dialog as upd
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        opened = []
        official_url = "https://bananaflow.bananaflow-media.workers.dev/he/download/"
        monkeypatch.setattr(upd, "site_url", lambda page: official_url)
        monkeypatch.setattr(
            upd.QDesktopServices,
            "openUrl",
            lambda url: opened.append(url.toString()),
        )
        dlg = upd.UpdatePromptDialog(_store(tmp_path), _release("9.9.9"), [])
        try:
            dlg._on_get_app_update()
            assert opened == [official_url]
            assert "github.com" not in opened[0]
        finally:
            dlg.deleteLater()


class TestHebrewRtlRendering:
    """Headless (offscreen) check that the update prompt renders in
    Hebrew with RTL layout when the app language is Hebrew."""

    def test_dialog_is_rtl_and_hebrew_when_language_is_he(self, tmp_path, monkeypatch):
        try:
            app = _make_qapp(tmp_path, monkeypatch)
            from PySide6.QtCore import Qt
            import ui.dialogs.update_prompt_dialog as upd
            from ui.i18n import set_language
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        set_language("he")
        app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        try:
            dlg = upd.UpdatePromptDialog(_store(tmp_path), _release("9.9.9"), [])
            try:
                assert dlg.layoutDirection() == Qt.LayoutDirection.RightToLeft
                body = dlg._body_lbl.text()
                # Body must actually be Hebrew, not the English fallback.
                assert any("֐" <= ch <= "׿" for ch in body)
                assert TRANSLATIONS["he"]["update_prompt_app_note"] in body
                # Buttons carry the Hebrew labels.
                assert dlg._skip_btn.text() == TRANSLATIONS["he"]["update_skip_btn"]
                assert TRANSLATIONS["he"]["update_remind_btn"] in dlg._remind_btn.text()
            finally:
                dlg.deleteLater()
        finally:
            set_language("en")
            app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)


class TestSettingsPanelUpdateCards:

    def test_cards_exist_and_start_the_right_check(self, tmp_path, monkeypatch):
        try:
            _make_qapp(tmp_path, monkeypatch)
            from config import AppConfig
            from ui.panels.settings_panel import SettingsPanel
            from ui.theme_manager import ThemeManager
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        started = []

        class FakeWorker:
            def __init__(self, *, check_app, check_components, parent=None):
                self.check_app = check_app
                self.check_components = check_components

                class _Sig:
                    def connect(self, *_a, **_k):
                        pass
                self.results_ready = _Sig()

            def start(self):
                started.append((self.check_app, self.check_components))

        monkeypatch.setattr("ui.panels.settings_panel.UpdateWorker", FakeWorker)

        cfg = AppConfig()
        panel = SettingsPanel(config=cfg, theme=ThemeManager(cfg))
        try:
            assert hasattr(panel, "_check_app_updates_card")
            assert hasattr(panel, "_check_component_updates_card")

            panel._check_app_updates_card.clicked.emit()
            panel._check_component_updates_card.clicked.emit()
            assert started == [(True, False), (False, True)]
        finally:
            panel.deleteLater()
