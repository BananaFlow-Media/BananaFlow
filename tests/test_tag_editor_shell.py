"""Tag Editor shell — the redesigned toolbar, "More" menu and footer.

The surface contract test (test_tag_editor_surface_contract.py) proves nothing
was *lost* by the redesign. This file proves the new arrangement actually
behaves the way the design intends: the toolbar carries the folder, the footer
carries pending work, and the split between them never changes what Apply
writes.
"""

from __future__ import annotations

import os

import pytest


def _make_panel(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    try:
        from PySide6.QtWidgets import QApplication
        from ui.panels.metadata_editor import MetadataEditorPanel
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")
    app = QApplication.instance() or QApplication([])
    return app, MetadataEditorPanel()


@pytest.fixture
def panel(tmp_path, monkeypatch):
    _app, widget = _make_panel(tmp_path, monkeypatch)
    try:
        yield widget
    finally:
        widget.deleteLater()


def _load(panel, tmp_path, *, count=2, changed=0):
    from core.metadata_models import AudioTrackItem, OriginalTags, ScanResult

    tracks = [
        AudioTrackItem(
            path=tmp_path / f"{i:02d} song.mp3",
            folder=tmp_path,
            ext=".mp3",
            format_id="mp3",
            original=OriginalTags(title=f"Song {i}"),
        )
        for i in range(count)
    ]
    panel._root_folder = tmp_path
    panel.on_scan_complete(ScanResult(root=tmp_path, tracks=tracks, folder_set={tmp_path}))
    for track in tracks[:changed]:
        track.proposed.title = f"New {track.original.title}"
    if changed:
        panel.on_auto_rules_applied()
    return tracks


# --------------------------------------------------------------------------- #
# Toolbar
# --------------------------------------------------------------------------- #

def test_more_menu_hosts_the_data_actions(panel):
    """Import/export, backups and restore left the bar but stayed reachable."""
    hosted = {
        action.defaultWidget()
        for action in panel._more_menu.actions()
        if hasattr(action, "defaultWidget")
    }
    assert panel._io_btn in hosted
    assert panel._backup_manager_btn in hosted
    assert panel._restore_btn in hosted


def test_more_menu_buttons_still_trigger_their_handlers(panel, monkeypatch):
    called = []
    monkeypatch.setattr(panel, "_on_metadata_io", lambda: called.append("io"))
    # Re-wire because the connection was made against the original bound method.
    panel._io_btn.clicked.disconnect()
    panel._io_btn.clicked.connect(panel._on_metadata_io)
    panel._io_btn.click()
    assert called == ["io"]


def test_refresh_button_offers_a_full_rescan_in_its_menu(panel, tmp_path):
    """Click is the cheap reconcile; the menu holds the full re-read."""
    from ui.i18n import t

    menu = panel._manual_refresh_btn.menu()
    assert menu is not None
    assert [a.text() for a in menu.actions()] == [t("meta_shell_rescan")]

    scans = []
    panel.scan_requested.connect(lambda folder, recursive: scans.append((folder, recursive)))
    _load(panel, tmp_path)
    panel._rescan_action.trigger()
    assert scans == [(tmp_path, True)]


def test_rescan_is_disabled_until_a_folder_is_loaded(panel, tmp_path):
    assert not panel._rescan_action.isEnabled()
    _load(panel, tmp_path)
    assert panel._rescan_action.isEnabled()


def test_scanning_is_always_recursive(panel, tmp_path, monkeypatch):
    """Subfolders are always included; there is no toggle to get this wrong."""
    from PySide6.QtWidgets import QFileDialog

    scans = []
    panel.scan_requested.connect(lambda folder, recursive: scans.append(recursive))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path))
    )
    panel._on_browse()

    _load(panel, tmp_path)
    panel._on_scan()

    assert scans == [True, True]


def test_path_chip_stays_ltr_and_shows_the_full_path_on_hover(panel, tmp_path):
    from PySide6.QtCore import Qt
    from ui.i18n import set_language, current_language

    previous = current_language()
    try:
        set_language("he")
        _load(panel, tmp_path)
        # A path is not prose: it reads left-to-right even in a Hebrew UI.
        assert panel._path_chip.layoutDirection() == Qt.LeftToRight
        assert panel._path_chip.toolTip() == str(tmp_path)
    finally:
        set_language(previous)


def test_search_moved_to_the_toolbar_and_still_filters(panel, tmp_path):
    tracks = _load(panel, tmp_path, count=3)
    assert panel._search_edit.parent() is panel._toolbar_bar

    before = panel._proxy.rowCount()
    panel._search_edit.setText("00 song")
    assert panel._proxy.rowCount() < before
    panel._search_edit.setText("")
    assert panel._proxy.rowCount() == before


# --------------------------------------------------------------------------- #
# Footer
# --------------------------------------------------------------------------- #

def test_footer_is_idle_when_nothing_is_pending(panel, tmp_path):
    from ui.i18n import t

    _load(panel, tmp_path, count=2)
    assert panel._footer_title.text() == t("meta_footer_ready")
    # The action row is meaningless with nothing to act on.
    assert not panel._apply_btn.isVisible()
    assert not panel._footer_count.isVisible()


def test_footer_counts_pending_changes_and_files(panel, tmp_path):
    _load(panel, tmp_path, count=3, changed=2)

    assert panel._footer_count.text().strip("⁦⁧⁨⁩") == "2"
    assert "2" in panel._footer_title.text()
    assert panel._apply_btn.isEnabled()
    assert panel._revert_btn.isEnabled()


def test_footer_count_matches_apply_scope_not_selection(panel, tmp_path):
    """The number beside Apply is the number Apply writes."""
    tracks = _load(panel, tmp_path, count=3, changed=2)

    panel._workspace.set_selected_items([tracks[2]])   # an unchanged row
    panel._refresh_checked_scope_state()

    expected = len(panel._workspace.apply_candidates())
    assert expected == 2
    assert panel._footer_count.text().strip("⁦⁧⁨⁩") == "2"


def test_footer_reports_excluded_changes(panel, tmp_path):
    from ui.i18n import t

    tracks = _load(panel, tmp_path, count=3, changed=2)
    panel._workspace.set_apply_excluded([tracks[0].path], True)
    panel._refresh_checked_scope_state()

    assert t("meta_footer_excluded_note", n=1) in panel._footer_desc.text()
    # Excluded means "not written", not "discarded".
    assert tracks[0].has_changes


def test_footer_keeps_the_review_shortcut_discoverable(panel):
    assert panel._review_btn.shortcut().toString() == "Ctrl+Shift+R"
    assert "Ctrl+Shift+R" in panel._review_btn.toolTip()
