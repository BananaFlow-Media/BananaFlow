"""
tests/test_accessibility_qss.py  –  High-contrast accessibility mode guards
============================================================================
The Accessibility Mode setting previously shipped a stylesheet that could
never work, for two independent reasons:

  1. Every rule used ``!important``, which Qt Style Sheets do not support
     (the declaration is silently dropped).
  2. It was appended to the QApplication stylesheet, but ThemeManager
     styles the main *window* directly — and window-level stylesheets
     always outrank application-level ones in Qt — so even valid rules
     were dead on arrival, and any theme/accent switch rebuilt the window
     stylesheet without the overlay.

These tests pin the fixed contract: no ``!important`` anywhere in app
QSS, and the overlay is applied through ThemeManager, replaces the theme
stylesheet, and survives theme switches.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────────────
# 1. Source-level guard: Qt QSS has no !important — it must never appear
# ──────────────────────────────────────────────────────────────────────────────

def test_no_important_in_any_ui_source():
    pure_comment = re.compile(r"^\s*#")
    offenders: list[str] = []
    for source_dir in ("ui", "main.py"):
        target = REPO_ROOT / source_dir
        paths = target.rglob("*.py") if target.is_dir() else [target]
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                # Pure comment lines may *mention* !important (e.g. to
                # explain this very rule); QSS lives in string literals,
                # which are never pure comment lines.
                if "!important" in line and not pure_comment.match(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "Qt Style Sheets do not support '!important' — the declaration is "
        f"silently dropped. Remove it from: {offenders}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. Behavior: the overlay replaces the theme QSS and survives theme switches
# ──────────────────────────────────────────────────────────────────────────────

def _make_app_and_config(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    from PySide6.QtWidgets import QApplication
    from config import AppConfig

    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    return app, cfg


class TestAccessibilityOverlayLifecycle:

    def _theme_manager(self, tmp_path, monkeypatch):
        try:
            app, cfg = _make_app_and_config(tmp_path, monkeypatch)
            from ui.theme_manager import ThemeManager
        except ImportError:
            pytest.skip("PySide6 / qfluentwidgets not available")
        return app, ThemeManager(cfg)

    def test_overlay_replaces_theme_and_survives_theme_switch(self, tmp_path, monkeypatch):
        app, tm = self._theme_manager(tmp_path, monkeypatch)
        from ui.app_window import _A11Y_QSS

        # No theme windows exist in this test, so ThemeManager styles the
        # QApplication — the same code path decides between overlay and theme.
        tm.apply("dark")
        theme_qss = app.styleSheet()
        assert theme_qss and _A11Y_QSS not in theme_qss

        tm.set_accessibility_qss(_A11Y_QSS)
        assert app.styleSheet() == _A11Y_QSS, "overlay must replace the theme QSS"

        # A theme switch used to rebuild the stylesheet and wipe the overlay.
        tm.apply("light")
        assert app.styleSheet() == _A11Y_QSS, "overlay must survive a theme switch"

        tm.set_accessibility_qss("")
        restored = app.styleSheet()
        assert restored != _A11Y_QSS
        assert restored, "disabling the overlay must restore the theme QSS"

        # Leave global state clean for other Qt tests in the session.
        tm.apply("dark")

    def test_a11y_qss_is_valid_for_dialog_inheritance(self, tmp_path, monkeypatch):
        """styled_dialog copies a parent window's stylesheet onto dialogs only
        when it contains a QDialog rule and a recognised theme marker color.
        The overlay must satisfy both so dialogs go high-contrast too."""
        _app, _tm = self._theme_manager(tmp_path, monkeypatch)
        from ui.app_window import _A11Y_QSS
        from ui.dialogs.styled_dialog import _qss_theme_is_light

        assert "QDialog" in _A11Y_QSS
        assert _qss_theme_is_light(_A11Y_QSS) is False, (
            "the overlay must be recognised as a dark theme by styled_dialog, "
            "or dialogs silently fall back to the decorative theme"
        )
        assert not re.search(r"!important", _A11Y_QSS)
