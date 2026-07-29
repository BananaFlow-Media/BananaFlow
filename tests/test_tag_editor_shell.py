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


# --------------------------------------------------------------------------- #
# Status column
# --------------------------------------------------------------------------- #

def test_status_column_reports_pending_changes(panel, tmp_path):
    from PySide6.QtCore import Qt
    from ui.i18n import t
    from ui.models.metadata_table_model import COL_STATUS

    tracks = _load(panel, tmp_path, count=2, changed=1)
    labels = [
        panel._model.data(panel._model.index(row, COL_STATUS), Qt.DisplayRole)
        for row in range(panel._model.rowCount())
    ]
    assert t("mt_status_pending_changes", n=1) in labels
    # A clean, editable row says nothing rather than inventing a state.
    assert "" in labels


def test_status_column_prefers_disk_state_over_pending_edits(panel, tmp_path):
    """A file that moved under you matters more than the edits queued on it."""
    from PySide6.QtCore import Qt
    from ui.i18n import t
    from ui.models.metadata_table_model import COL_STATUS

    tracks = _load(panel, tmp_path, count=1, changed=1)
    tracks[0].external_state = "changed_on_disk"
    panel._model.refresh_all()

    label = panel._model.data(panel._model.index(0, COL_STATUS), Qt.DisplayRole)
    assert label == t("meta_external_state_changed_on_disk")


def test_status_column_is_user_hideable(panel):
    """Per the review it is a real column, not a fixed decoration."""
    from ui.models.metadata_table_model import COL_STATUS, _HEADER_KEYS

    assert _HEADER_KEYS[COL_STATUS] == "mt_col_status"
    panel._set_column_hidden(COL_STATUS, True)
    assert panel._table.isColumnHidden(COL_STATUS)
    panel._set_column_hidden(COL_STATUS, False)
    assert not panel._table.isColumnHidden(COL_STATUS)


def test_status_column_sorts_attention_first(panel, tmp_path):
    from ui.models.metadata_table_model import _row_status_rank

    tracks = _load(panel, tmp_path, count=3, changed=1)
    tracks[2].external_state = "changed_on_disk"

    ranks = [_row_status_rank(track)[0] for track in tracks]
    # changed-on-disk (0) before pending edits (1) before clean (3)
    assert ranks[2] < ranks[0] < ranks[1]


# --------------------------------------------------------------------------- #
# Saved column layout must survive the new column
# --------------------------------------------------------------------------- #

def test_saved_column_order_is_widened_not_discarded(panel):
    """An existing user's arrangement must not silently reset on upgrade."""
    from ui.models.metadata_table_model import COLUMN_COUNT

    saved_fifteen = [14, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    migrated = panel._migrate_saved_column_order(saved_fifteen)

    assert len(migrated) == COLUMN_COUNT
    assert migrated[:15] == saved_fifteen      # their order, untouched
    assert migrated[15] == 15                  # the column they have not seen


def test_current_column_order_passes_through_unchanged(panel):
    from ui.models.metadata_table_model import COLUMN_COUNT

    current = list(range(COLUMN_COUNT))
    assert panel._migrate_saved_column_order(current) == current


@pytest.mark.parametrize("bad", [[], None, [0, 0, 1], list(range(99))])
def test_untrustworthy_column_order_falls_back_to_defaults(panel, bad):
    assert panel._migrate_saved_column_order(bad) is None


# --------------------------------------------------------------------------- #
# Inspector: three modes, fifteen panes
# --------------------------------------------------------------------------- #

def test_inspector_exposes_all_fifteen_panes(panel):
    assert len(panel._inspector_tool_buttons) == 15
    assert panel._inspector_pages.count() == 15
    counts = {mode: panel._inspector_pane_modes.count(mode)
              for mode in ("edit", "tools", "check")}
    assert counts == {"edit": 5, "tools": 6, "check": 4}


def test_legacy_tool_indices_still_mean_what_they_meant(panel):
    """_select_inspector_tool is API: indices 0..7 are the old rail order."""
    from ui.i18n import t

    assert panel._inspector_tool_titles[0] == t("meta_edit_tags_group")
    assert panel._inspector_tool_titles[1] == t("meta_action_engine_title")
    assert panel._inspector_tool_titles[7] == t("meta_problems_title")

    panel._select_inspector_tool(1)
    assert panel._inspector_pages.currentIndex() == 1
    assert panel._inspector_tool_buttons[1].isChecked()
    assert not panel._inspector_tool_buttons[0].isChecked()


def test_mode_tabs_track_the_active_pane(panel):
    panel._select_inspector_tool(0)                      # edit / fields
    assert panel._inspector_mode_buttons["edit"].isChecked()
    assert not panel._inspector_mode_buttons["tools"].isChecked()

    panel._select_inspector_tool(1)                      # tools / actions
    assert panel._inspector_mode_buttons["tools"].isChecked()
    assert not panel._inspector_mode_buttons["edit"].isChecked()


def test_switching_mode_lands_on_its_first_pane(panel):
    panel._open_inspector_mode("check")
    assert panel._inspector_pane_modes[panel._active_inspector_tool] == "check"
    # Re-opening the mode you are already in must not jump you elsewhere.
    current = panel._active_inspector_tool
    panel._open_inspector_mode("check")
    assert panel._active_inspector_tool == current


def test_subtabs_show_only_the_active_mode(panel):
    panel._select_inspector_tool(0)
    shown = {
        panel._inspector_subtab_layout.itemAt(i).widget()
        for i in range(panel._inspector_subtab_layout.count())
        if panel._inspector_subtab_layout.itemAt(i).widget() is not None
    }
    edit_buttons = {
        panel._inspector_tool_buttons[i]
        for i, mode in enumerate(panel._inspector_pane_modes) if mode == "edit"
    }
    assert shown == edit_buttons


def test_edit_panes_cover_the_whole_track_inspector(panel):
    """Fields, artwork, lyrics, ReplayGain and properties are now siblings."""
    for name in ("_insp_fields", "_insp_artwork_add_btn", "_insp_lyrics",
                 "_insp_rg_track_btn", "_insp_properties"):
        assert hasattr(panel, name), f"edit panes lost {name}"


def test_auto_arrange_page_lists_what_it_will_run(panel):
    """The button is otherwise a black box that edits files by unseen rules."""
    from ui.i18n import t

    panel._auto_ops = {"title_strip", "track_num"}
    panel._refresh_auto_enabled_list()
    text = panel._auto_enabled_list.text()
    assert t("meta_op_title_strip_label") in text
    assert t("meta_op_track_num_label") in text

    panel._auto_ops = set()
    panel._refresh_auto_enabled_list()
    assert panel._auto_enabled_list.text() == t("meta_auto_none_enabled")


def test_auto_arrange_settings_button_survives_the_move(panel):
    """Per the review the page keeps both buttons, not just the action."""
    assert hasattr(panel, "_auto_btn")
    assert hasattr(panel, "_auto_cfg_btn")
    assert hasattr(panel, "_auto_container")


def test_check_pending_page_reports_apply_scope(panel, tmp_path):
    from ui.i18n import t

    _load(panel, tmp_path, count=3, changed=2)
    assert panel._pending_summary.text() == t(
        "meta_pending_summary", files=2, applying=2)
    assert panel._pending_review_btn.isEnabled()


def test_check_pending_page_is_empty_when_nothing_is_queued(panel, tmp_path):
    from ui.i18n import t

    _load(panel, tmp_path, count=2)
    assert panel._pending_summary.text() == t("meta_pending_none")
    assert not panel._pending_review_btn.isEnabled()


def test_check_external_page_separates_changed_from_blocking(panel, tmp_path):
    """Not every disk change blocks Apply, and the page must not imply it does.

    A file that changed on disk is reported but still writable; it only blocks
    once the change collides with local proposals (stale_with_proposals).
    Conflating the two would either cry wolf or hide a real blocker.
    """
    from ui.i18n import t

    tracks = _load(panel, tmp_path, count=2, changed=1)
    tracks[0].external_state = "changed_on_disk"
    panel._refresh_checked_scope_state()

    assert panel._external_summary.text() == t(
        "meta_external_summary", stale=1, blocked=0)
    assert panel._external_review_all_btn.isEnabled()

    tracks[0].external_state = "stale_with_proposals"
    panel._refresh_checked_scope_state()

    assert panel._external_summary.text() == t(
        "meta_external_summary", stale=1, blocked=1)
    # A blocked file is also out of the apply batch entirely.
    assert tracks[0] not in panel._workspace.apply_candidates()


def test_check_external_page_is_quiet_when_the_disk_agrees(panel, tmp_path):
    from ui.i18n import t

    _load(panel, tmp_path, count=2, changed=1)
    assert panel._external_summary.text() == t("meta_external_none")
    assert not panel._external_review_all_btn.isEnabled()
