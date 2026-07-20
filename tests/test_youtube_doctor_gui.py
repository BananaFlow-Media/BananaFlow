"""
tests/test_youtube_doctor_gui.py  –  Reliability-hardening phase 4
========================================================================
Lightweight, headless (QT_QPA_PLATFORM=offscreen) smoke tests for the
"Run YouTube Doctor" settings card and its results dialog. Skips
gracefully if PySide6/qfluentwidgets isn't available (headless CI),
mirroring the existing pattern in tests/test_p0_gates.py
(TestSearchPanelRestoresYTMusic).
"""

from __future__ import annotations

import os

import pytest


def _make_app_and_config(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    from PySide6.QtWidgets import QApplication
    from config import AppConfig

    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    return app, cfg


class TestSettingsPanelYoutubeDoctorCard:

    def test_card_exists_and_click_triggers_handler(self, tmp_path, monkeypatch):
        try:
            _app, cfg = _make_app_and_config(tmp_path, monkeypatch)
            from ui.panels.settings_panel import SettingsPanel
            from ui.theme_manager import ThemeManager
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        theme = ThemeManager(cfg)
        panel = SettingsPanel(config=cfg, theme=theme)
        try:
            assert hasattr(panel, "_youtube_doctor_card")

            called = []
            monkeypatch.setattr(
                "ui.panels.settings_panel.show_youtube_doctor_dialog",
                lambda report, parent=None: called.append(report),
            )
            # Emit the card's clicked signal directly rather than simulating
            # a real mouse click — proves the signal is actually connected
            # to _on_run_youtube_doctor, not just that the card exists.
            panel._youtube_doctor_card.clicked.emit()
            assert len(called) == 1
        finally:
            panel.deleteLater()

    def test_run_doctor_action_returns_structured_report(self, tmp_path, monkeypatch):
        try:
            _app, cfg = _make_app_and_config(tmp_path, monkeypatch)
            from ui.panels.settings_panel import SettingsPanel
            from ui.theme_manager import ThemeManager
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        theme = ThemeManager(cfg)
        panel = SettingsPanel(config=cfg, theme=theme)
        try:
            captured = {}

            def fake_show(report, parent=None):
                captured["report"] = report

            monkeypatch.setattr(
                "ui.panels.settings_panel.show_youtube_doctor_dialog", fake_show
            )
            panel._on_run_youtube_doctor()

            report = captured.get("report")
            assert report is not None
            categories = {c.category for c in report.checks}
            assert categories == {
                "yt_dlp_version", "yt_dlp_ejs", "js_runtime",
                "cookies", "po_token_provider", "youtube_reliability_mode",
            }
        finally:
            panel.deleteLater()


class TestYoutubeDoctorDialogWidget:

    def test_dialog_constructs_and_shows_recommended_actions(self, tmp_path, monkeypatch):
        try:
            _app, _cfg = _make_app_and_config(tmp_path, monkeypatch)
            from core.youtube_doctor import DoctorCheck, DoctorStatus, YoutubeDoctorReport
            from ui.dialogs.youtube_doctor_dialog import YoutubeDoctorDialog, build_report_text
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        report = YoutubeDoctorReport(checks=[
            DoctorCheck(
                category="po_token_provider", status=DoctorStatus.WARN,
                message="No PO Token Provider plugin detected.",
                detail="Update or reinstall BananaFlow so bundled provider files are present.",
            ),
        ])
        dlg = YoutubeDoctorDialog(report)
        try:
            assert "Update or reinstall BananaFlow" in build_report_text(report)
        finally:
            dlg.deleteLater()

    def test_dialog_never_shows_cookie_values(self, tmp_path, monkeypatch):
        try:
            _app, _cfg = _make_app_and_config(tmp_path, monkeypatch)
            from core.youtube_doctor import run_youtube_doctor
            from ui.dialogs.youtube_doctor_dialog import YoutubeDoctorDialog, build_report_text
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
            return

        secret = "SECRET_COOKIE_VALUE_MUST_NEVER_APPEAR"
        cookies_path = tmp_path / "cookies.txt"
        cookies_path.write_text(f".youtube.com\tTRUE\t/\tFALSE\t0\tLOGIN_INFO\t{secret}\n")

        report = run_youtube_doctor(cookies_file=str(cookies_path))
        dlg = YoutubeDoctorDialog(report)
        try:
            assert secret not in build_report_text(report)
        finally:
            dlg.deleteLater()
