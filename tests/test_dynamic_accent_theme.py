from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AMBER = "#F5A623"
OCEAN = "#0ea5e9"
VIOLET = "#7c3aed"


def _make_app_config_theme(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    try:
        from PySide6.QtWidgets import QApplication
        from config import AppConfig
        from ui.theme_manager import ThemeManager
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")

    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()
    cfg.theme = "light"
    cfg.accent_color = OCEAN
    tm = ThemeManager(cfg)
    tm.apply("light")
    tm.set_accent(OCEAN)
    return app, cfg, tm


def _stylesheets(*widgets) -> str:
    return "\n".join(w.styleSheet() for w in widgets)


def test_top_bars_restyle_when_accent_changes(tmp_path, monkeypatch):
    app, cfg, tm = _make_app_config_theme(tmp_path, monkeypatch)

    from ui.panels.options_bar import OptionsBar
    from ui.panels.status_bar import StatusBar
    from ui.panels.url_bar import UrlBar

    url_bar = UrlBar(cfg)
    options_bar = OptionsBar(cfg)
    status_bar = StatusBar()
    try:
        first_qss = _stylesheets(
            url_bar._url_entry,
            url_bar._fetch_btn,
            *url_bar._tool_btns,
            options_bar._type_combo,
            options_bar._dir_entry,
            options_bar._browse_btn,
            status_bar._det_bar,
        )
        assert OCEAN in first_qss
        assert AMBER not in first_qss

        tm.set_accent(VIOLET)
        app.processEvents()

        updated_qss = _stylesheets(
            url_bar._url_entry,
            url_bar._fetch_btn,
            *url_bar._tool_btns,
            options_bar._type_combo,
            options_bar._dir_entry,
            options_bar._browse_btn,
            status_bar._det_bar,
        )
        assert VIOLET in updated_qss
        assert OCEAN not in updated_qss
        assert AMBER not in updated_qss
    finally:
        url_bar.deleteLater()
        options_bar.deleteLater()
        status_bar.deleteLater()


def test_main_panels_restyle_when_accent_changes(tmp_path, monkeypatch):
    app, cfg, tm = _make_app_config_theme(tmp_path, monkeypatch)

    from ui.panels.converter_panel import ConverterPanel
    from ui.panels.history_panel import HistoryPanel
    from ui.panels.queue_panel import QueuePanel
    from ui.panels.search_panel import SearchPanel

    class _FakeHistoryDB:
        def fetch_all(self, limit=500):
            return []

        def search(self, query, limit=500):
            return []

    queue_panel = QueuePanel()
    search_panel = SearchPanel(cfg)
    history_panel = HistoryPanel(_FakeHistoryDB(), cfg)
    converter_panel = ConverterPanel()
    try:
        first_qss = _stylesheets(
            queue_panel,
            search_panel,
            history_panel,
            converter_panel,
        )
        assert OCEAN in first_qss
        assert AMBER not in first_qss

        tm.set_accent(VIOLET)
        app.processEvents()

        updated_qss = _stylesheets(
            queue_panel,
            search_panel,
            history_panel,
            converter_panel,
        )
        assert VIOLET in updated_qss
        assert OCEAN not in updated_qss
        assert AMBER not in updated_qss
    finally:
        queue_panel.deleteLater()
        search_panel.deleteLater()
        history_panel.deleteLater()
        converter_panel.deleteLater()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Open design decision, not a bug to paper over. The Tag Editor redesign "
        "made secondary buttons a neutral surface with a plain border, so "
        "btn_style() no longer contains the accent at all -- only "
        "primary_btn_style() does. Whether a secondary button should follow the "
        "user's accent has not been decided. strict=True on purpose: if someone "
        "makes btn_style() accent-aware again, this XPASSes and fails the run, "
        "which is the reminder to delete this marker."
    ),
)
def test_metadata_editor_helpers_use_live_accent(tmp_path, monkeypatch):
    _app, _cfg, tm = _make_app_config_theme(tmp_path, monkeypatch)

    from ui.panels.metadata_editor_panel import _btn_style, _primary_btn_style

    assert OCEAN in _btn_style()
    assert OCEAN in _primary_btn_style()
    assert AMBER not in _btn_style()
    assert AMBER not in _primary_btn_style()

    tm.set_accent(VIOLET)
    assert VIOLET in _btn_style()
    assert VIOLET in _primary_btn_style()
    assert OCEAN not in _btn_style()
    assert OCEAN not in _primary_btn_style()


def test_metadata_editor_empty_state_uses_shared_minimal_accent_icon():
    """No bespoke, hardcoded-brand styling in the Tag Editor panel.

    This guarded the *intent* -- shared icon component, no frozen hex values,
    styles from the shared helpers -- with a list of literal source strings.
    Four of those strings described one specific implementation: a gradient
    Auto-Order frame tinted from nine derived accent shades. The redesign
    replaced that frame with a plain container holding two ordinary buttons,
    which left the assertions pinning code that no longer had a reason to
    exist. The intent below is what actually matters and still holds.
    """
    source = (REPO_ROOT / "ui/panels/metadata_editor/panel.py").read_text(encoding="utf-8")

    assert 'EmptyStateIcon("tag"' in source
    assert 'EmptyStateIcon("sync"' in source
    # Frozen brand purples: the reason this test exists.
    assert "#5147f5" not in source
    assert "#2f2758" not in source
    assert "#d9ccff" not in source
    assert 'for attr in ("_revert_btn", "_restore_btn", "_io_btn")' in source
    assert 'apply_role = "primary" if apply_enabled else "neutral"' in source
    assert "self._browse_btn.setStyleSheet" in source
    assert "auto_text = accent" in source
    # The empty state's call to action is allowed, but it takes the shared
    # accent-derived style rather than inventing its own.
    assert "self._empty_browse_btn.setStyleSheet(primary_btn_style())" in source


def test_fluent_accent_updates_immediately(tmp_path, monkeypatch):
    _app, _cfg, tm = _make_app_config_theme(tmp_path, monkeypatch)

    import ui.theme_manager as theme_manager

    calls: list[tuple[str, bool]] = []
    original_get = theme_manager.qconfig.get

    def fake_get(item):
        if item is theme_manager.qconfig.themeColor:
            return "#000000"
        return original_get(item)

    monkeypatch.setattr(theme_manager.qconfig, "get", fake_get)
    monkeypatch.setattr(
        theme_manager,
        "setThemeColor",
        lambda color, lazy=False, save=False: calls.append((QColor(color).name(), lazy)),
    )

    from PySide6.QtGui import QColor

    tm.set_accent(VIOLET)

    assert calls
    assert calls[-1] == (VIOLET, False)


def test_non_theme_modules_do_not_import_static_accent():
    allowed = {
        Path("ui/theme_manager.py"),
        Path("ui/dialogs/styled_dialog.py"),
    }
    offenders: list[str] = []
    for path in (REPO_ROOT / "ui").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel in allowed or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ACCENT_COLOR" in text:
            offenders.append(str(rel))

    assert not offenders, (
        "Use get_colors().accent for UI styling; importing ACCENT_COLOR freezes "
        f"the amber default in these modules: {offenders}"
    )
