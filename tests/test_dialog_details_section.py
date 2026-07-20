"""
tests/test_dialog_details_section.py  –  Phase 3 error-dialog polish guard
==========================================================================
Error dialogs show a plain-language title and message; raw technical
output (yt-dlp / Playwright / DPAPI text) lives behind a collapsed
"Show details" toggle. These tests pin:

* passing no details keeps the legacy dialog exactly as before,
* details start collapsed and toggle open/closed with the button,
* the details box is read-only and always LTR (raw tool output),
* the toggle labels come from the translation tables (EN + HE).

Headless (QT_QPA_PLATFORM=offscreen); skips when PySide6 is missing.
"""

from __future__ import annotations

import os

import pytest


def _make_dialog(tmp_path, monkeypatch, *, details: str = "", lang: str = "en"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    try:
        from PySide6.QtWidgets import QApplication
        from ui.dialogs.styled_dialog import StyledMessageDialog
        from ui.i18n import set_language
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")

    app = QApplication.instance() or QApplication([])
    set_language(lang)
    dlg = StyledMessageDialog(
        "Download failed", "Something went wrong.", None,
        kind="warning", details=details,
    )
    return app, dlg


class TestDialogDetailsSection:

    def test_no_details_keeps_legacy_dialog(self, tmp_path, monkeypatch):
        _app, dlg = _make_dialog(tmp_path, monkeypatch)
        try:
            assert dlg._details_box is None
            assert dlg._details_btn is None
        finally:
            dlg.deleteLater()

    def test_details_start_collapsed_and_toggle(self, tmp_path, monkeypatch):
        from ui.i18n import t
        app, dlg = _make_dialog(
            tmp_path, monkeypatch, details="ERROR: [youtube] raw upstream text",
        )
        try:
            dlg.show()
            app.processEvents()
            assert dlg._details_box is not None
            assert not dlg._details_box.isVisible()
            assert dlg._details_btn.text() == t("details_show_btn")

            dlg._details_btn.click()
            app.processEvents()
            assert dlg._details_box.isVisible()
            assert dlg._details_btn.text() == t("details_hide_btn")

            dlg._details_btn.click()
            app.processEvents()
            assert not dlg._details_box.isVisible()
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_details_box_is_readonly_and_ltr(self, tmp_path, monkeypatch):
        from PySide6.QtCore import Qt
        app, dlg = _make_dialog(
            tmp_path, monkeypatch, details="raw", lang="he",
        )
        try:
            dlg.show()
            app.processEvents()
            assert dlg._details_box.isReadOnly()
            assert dlg._details_box.layoutDirection() == Qt.LayoutDirection.LeftToRight
        finally:
            dlg.close()
            dlg.deleteLater()
            from ui.i18n import set_language
            set_language("en")

    def test_toggle_labels_translated(self, tmp_path, monkeypatch):
        from ui.i18n import TRANSLATIONS
        for key in ("details_show_btn", "details_hide_btn"):
            assert key in TRANSLATIONS["en"]
            assert key in TRANSLATIONS["he"]
            assert TRANSLATIONS["en"][key] != TRANSLATIONS["he"][key]

    def test_whitespace_only_details_treated_as_none(self, tmp_path, monkeypatch):
        _app, dlg = _make_dialog(tmp_path, monkeypatch, details="   \n  ")
        try:
            assert dlg._details_box is None
        finally:
            dlg.deleteLater()
