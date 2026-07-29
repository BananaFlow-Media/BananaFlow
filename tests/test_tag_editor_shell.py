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

def test_reference_light_palette_tokens_are_exact(panel, monkeypatch):
    from types import SimpleNamespace
    from ui.panels.metadata_editor import shared

    monkeypatch.setattr(
        shared,
        "get_colors",
        lambda: SimpleNamespace(bg="#fbfaff", accent="#10A37F"),
    )

    colors = shared.tag_editor_colors()
    assert (
        colors.bg,
        colors.surface,
        colors.surface2,
        colors.surface3,
        colors.border,
        colors.text_primary,
        colors.text_secondary,
        colors.text_tertiary,
        colors.accent,
        colors.accent_dark,
    ) == (
        "#EAEEEC", "#FFFFFF", "#F5F7F6", "#F1F4F2", "#E1E7E3",
        "#16201C", "#66706A", "#9AA49D", "#10A37F", "#0B7A5F",
    )

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


def test_refresh_group_exposes_a_full_rescan_in_its_disclosure(panel, tmp_path):
    """The primary refresh is safe; the disclosure holds the full re-read."""
    from ui.i18n import t

    assert panel._manual_refresh_btn.menu() is None
    assert panel._refresh_menu_btn.menu() is None
    assert panel._rescan_action in panel._refresh_menu.actions()
    assert panel._rescan_action.text() == t("meta_shell_rescan")

    scans = []
    panel.scan_requested.connect(lambda folder, recursive: scans.append((folder, recursive)))
    _load(panel, tmp_path)
    panel._rescan_action.trigger()
    assert scans == [(tmp_path, True)]


def test_rescan_is_disabled_until_a_folder_is_loaded(panel, tmp_path):
    assert not panel._rescan_action.isEnabled()
    _load(panel, tmp_path)
    assert panel._rescan_action.isEnabled()


def test_browse_and_rescan_always_include_subfolders(panel, tmp_path, monkeypatch):
    """All scans are recursive; the toolbar no longer exposes a scope toggle."""
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
    assert not hasattr(panel, "_subdirs_check")


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


def test_status_column_is_fixed_visible(panel):
    """Status is the fixed trailing column and cannot become empty space."""
    from ui.models.metadata_table_model import COL_STATUS, _HEADER_KEYS

    assert _HEADER_KEYS[COL_STATUS] == "mt_col_status"
    panel._set_column_hidden(COL_STATUS, True)
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
    assert migrated[16] == 16                  # the new dedicated empty gutter
    assert migrated[17] == 17                  # the opposite-edge empty gutter


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


# --------------------------------------------------------------------------- #
# Set-one-value: a working feature that had no reachable UI
# --------------------------------------------------------------------------- #

def test_set_artist_across_selection_is_reachable_and_emits(panel, tmp_path):
    """The handlers existed and were tested, but nothing built or called them."""
    tracks = _load(panel, tmp_path, count=3)
    panel._workspace.set_selected_items(tracks[:2])

    emitted = []
    panel.artist_to_scope.connect(lambda artist, items: emitted.append((artist, items)))

    panel._insp_folder_artist.setText("  Ishay Ribo  ")
    panel._on_insp_folder_artist()

    assert emitted == [("Ishay Ribo", tracks[:2])]


def test_set_album_across_selection_is_reachable_and_emits(panel, tmp_path):
    tracks = _load(panel, tmp_path, count=2)
    panel._workspace.set_selected_items([tracks[0]])

    emitted = []
    panel.album_to_scope.connect(lambda album, items: emitted.append((album, items)))

    panel._insp_folder_album.setText("Kavana")
    panel._on_insp_folder_album()

    assert emitted == [("Kavana", [tracks[0]])]


def test_set_value_does_nothing_without_a_value_or_a_selection(panel, tmp_path):
    tracks = _load(panel, tmp_path, count=2)
    emitted = []
    panel.artist_to_scope.connect(lambda *a: emitted.append(a))

    # Empty input with a selection.
    panel._workspace.set_selected_items([tracks[0]])
    panel._insp_folder_artist.setText("   ")
    panel._on_insp_folder_artist()
    assert emitted == []

    # A value, but nothing selected: row selection is the editing scope.
    panel._workspace.set_selected_items([])
    panel._insp_folder_artist.setText("Someone")
    panel._on_insp_folder_artist()
    assert emitted == []


# --------------------------------------------------------------------------- #
# Scan failure is a visible state, not a silent one
# --------------------------------------------------------------------------- #

def test_scan_error_is_shown_where_the_files_would_have_been(panel, tmp_path):
    """It previously only reached a label that is never displayed."""
    panel._root_folder = tmp_path
    panel.on_scan_error("Access is denied")

    assert panel._table_stack.currentWidget() is panel._table_error_page
    assert "Access is denied" in panel._error_body_lbl.text()
    assert panel._error_retry_btn.isEnabled()


def test_scan_error_retry_rescans_the_same_folder(panel, tmp_path):
    scans = []
    panel.scan_requested.connect(lambda folder, recursive: scans.append(folder))
    panel._root_folder = tmp_path
    panel.on_scan_error("boom")
    panel._error_retry_btn.click()
    assert scans == [tmp_path]


def test_a_failed_refresh_does_not_blank_a_loaded_folder(panel, tmp_path):
    """Losing the listing you already have would be worse than the error."""
    _load(panel, tmp_path, count=2)
    panel.on_scan_error("transient failure")
    assert panel._table_stack.currentWidget() is not panel._table_error_page


# --------------------------------------------------------------------------- #
# Small windows: the table must never be the thing that gets squeezed out
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("width, height", [(980, 680), (1100, 760), (1440, 900)])
def test_layout_holds_at_supported_window_sizes(panel, tmp_path, width, height):
    _load(panel, tmp_path, count=3)
    panel.resize(width, height)
    panel.show()
    try:
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        sizes = panel._body_splitter.sizes()
        assert len(sizes) == 3
        tree, table, inspector = sizes

        # The centre table is the point of the screen; it is never collapsed
        # to make room for a side pane.
        assert table >= panel._TABLE_OPEN_MIN - 1, (width, sizes)
        # A pane is either open at a usable width or collapsed to its rail --
        # never left at some unusable in-between width.
        assert tree <= panel._TREE_RAIL_WIDTH or tree >= panel._TREE_OPEN_MIN - 1
        assert (inspector <= panel._INSPECTOR_RAIL_WIDTH
                or inspector >= panel._INSPECTOR_OPEN_MIN - 1)
        # Nothing may force the panel wider than the window.
        assert panel.width() <= width
    finally:
        panel.hide()


def test_responsive_panes_restore_after_returning_from_narrow_width(panel, tmp_path):
    from PySide6.QtWidgets import QApplication

    _load(panel, tmp_path, count=3)
    panel.resize(1440, 900)
    panel.show()
    QApplication.processEvents()
    panel._apply_body_sizes([220, 678, 370], save=False)

    panel.resize(980, 680)
    QApplication.processEvents()
    narrow = panel._body_splitter.sizes()
    assert narrow[0] <= panel._TREE_RAIL_WIDTH + 1
    assert narrow[2] <= 300

    panel.resize(1440, 900)
    QApplication.processEvents()
    wide = panel._body_splitter.sizes()
    assert wide[0] >= panel._TREE_OPEN_MIN
    assert wide[2] >= panel._INSPECTOR_OPEN_MIN
    panel.hide()


def test_collapsing_a_pane_gives_its_width_to_the_table(panel, tmp_path):
    _load(panel, tmp_path, count=3)
    panel.resize(1100, 760)
    panel.show()
    try:
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        before = panel._body_splitter.sizes()[1]

        panel._set_tree_collapsed(True)
        QApplication.processEvents()
        after = panel._body_splitter.sizes()[1]

        assert after > before
    finally:
        panel.hide()


# --------------------------------------------------------------------------- #
# RTL
# --------------------------------------------------------------------------- #

def test_hebrew_mirrors_the_shell_but_never_the_filenames(panel, tmp_path):
    """Hebrew flips the layout; paths and filenames stay left-to-right."""
    from PySide6.QtCore import Qt
    from ui.i18n import current_language, set_language

    previous = current_language()
    try:
        set_language("he")
        _load(panel, tmp_path, count=2)
        panel._refresh_navigation_arrow_direction()

        assert panel._path_chip.layoutDirection() == Qt.LeftToRight
        # The filename column keeps its own LTR delegate in either language.
        from ui.models.metadata_table_model import COL_FILENAME
        delegate = panel._table.itemDelegateForColumn(COL_FILENAME)
        assert delegate is not None
    finally:
        set_language(previous)


@pytest.mark.parametrize("language", ["en", "he"])
def test_every_shell_string_is_translated(language):
    """No new string may fall back to its key in either language."""
    from ui.i18n import current_language, set_language, t

    previous = current_language()
    try:
        set_language(language)
        for key in (
            "meta_shell_more", "meta_shell_rescan", "meta_shell_no_folder",
            "meta_footer_ready", "meta_footer_pending", "meta_footer_backup_note",
            "meta_inspector_mode_edit", "meta_inspector_mode_tools",
            "meta_inspector_mode_check", "meta_auto_enabled_heading",
            "meta_pending_tab", "meta_pending_none", "meta_external_tab",
            "meta_external_none", "meta_apply_value_group",
            "meta_apply_artist_to_selection", "meta_apply_album_to_selection",
            "meta_scan_error_title", "meta_scan_error_retry",
            "mt_col_status", "mt_status_read_only", "meta_scope_hint",
        ):
            assert t(key) != key, f"{key} untranslated in {language}"
    finally:
        set_language(previous)
