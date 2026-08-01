"""Tag Editor surface contract — the redesign safety net.

The Tag Editor redesign (docs/design/tag-editor/) moves nearly every widget to a
new home: the toolbar splits between a slim bar and a footer, the eight-tool
inspector rail becomes three modes with fifteen sub-tabs, and panel.py is broken
into modules.  None of that is allowed to *lose* anything.

This file is deliberately written against the surface a user (or an existing
test) can reach, not against layout: it asserts that every action, field,
operation, dialog, menu entry, shortcut and scope rule still exists and still
means the same thing.  It is committed *before* the redesign starts and must
keep passing at every phase, so a silently dropped affordance fails here rather
than in a bug report months later.

Deliberately NOT asserted: pixel geometry, stylesheets, parent/child nesting, or
which container a widget happens to live in.  Those are exactly what the
redesign is allowed to change.
"""

from __future__ import annotations

import os

import pytest


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

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


def _loaded_panel(panel, tmp_path, *, count: int = 2):
    """Put the panel into the 'folder scanned, tracks present' state."""
    from core.metadata_models import AudioTrackItem, OriginalTags, ScanResult

    tracks = [
        AudioTrackItem(
            path=tmp_path / f"{i:02d} song.mp3",
            folder=tmp_path,
            ext=".mp3",
            # format_id drives FormatCapabilityRegistry lookups; without it
            # every field reads as unsupported and proposals are silently
            # skipped rather than applied.
            format_id="mp3",
            original=OriginalTags(title=f"Song {i}"),
        )
        for i in range(count)
    ]
    panel._root_folder = tmp_path
    panel.on_scan_complete(
        ScanResult(root=tmp_path, tracks=tracks, folder_set={tmp_path})
    )
    return tracks


class _FakeSignal:
    def connect(self, *_args, **_kwargs) -> None:
        pass


class _FakeAction:
    def __init__(self, text: str = "", separator: bool = False) -> None:
        self._text = text
        self._separator = separator
        self.triggered = _FakeSignal()

    def text(self) -> str:
        return self._text

    def isSeparator(self) -> bool:
        return self._separator

    def setCheckable(self, _value) -> None:
        pass

    def setChecked(self, _value) -> None:
        pass

    def setEnabled(self, _value) -> None:
        pass


class _FakeMenu:
    """Stand-in for QMenu that records entries instead of showing them.

    Monkeypatching ``QMenu.exec`` does not work: it is a Shiboken slot wrapper,
    so the real modal menu still opens and faults under the offscreen platform.
    The panel imports QMenu *inside* each handler, so replacing the module
    attribute is both reliable and free of native code.
    """

    opened: list[list[str]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self._actions: list[_FakeAction] = []

    def addAction(self, *args) -> _FakeAction:
        # Called as addAction(text) or addAction(icon, text).
        text = args[-1] if args else ""
        action = _FakeAction(str(text))
        self._actions.append(action)
        return action

    def addSeparator(self) -> _FakeAction:
        action = _FakeAction(separator=True)
        self._actions.append(action)
        return action

    def actions(self) -> list[_FakeAction]:
        return list(self._actions)

    def exec(self, *_args, **_kwargs):
        type(self).opened.append([
            "---" if action.isSeparator() else action.text()
            for action in self._actions
        ])
        return None


def _capture_menu(monkeypatch):
    """Record what a context menu would have shown, without opening one."""
    _FakeMenu.opened = []
    monkeypatch.setattr("PySide6.QtWidgets.QMenu", _FakeMenu)
    return _FakeMenu.opened


# --------------------------------------------------------------------------- #
# 1. Panel internals the existing suite binds to
# --------------------------------------------------------------------------- #

# Every name here is referenced by at least one other test file. The redesign
# may move the widget, but the attribute has to keep resolving or ~20 test
# modules break at once.
CONTRACT_ATTRIBUTES = (
    # toolbar / primary actions
    "_apply_btn", "_revert_btn", "_review_btn", "_undo_btn", "_redo_btn",
    "_browse_btn", "_auto_btn", "_auto_container", "_io_btn", "_restore_btn",
    "_backup_manager_btn", "_manual_refresh_btn",
    "_scan_progress", "_summary_lbl",
    # table chrome
    "_table", "_model", "_proxy", "_workspace", "_navigation",
    "_apply_scope_lbl", "_excluded_chip", "_stale_chip", "_table_info_lbl",
    "_exclude_apply_btn", "_search_edit",
    # zoom + navigation
    "_zoom_minus_btn", "_zoom_plus_btn", "_zoom_val_lbl",
    "_nav_back_btn", "_nav_forward_btn", "_nav_up_btn", "_breadcrumbs_layout",
    # tree
    "_tree", "_tree_toggle_btn",
    # inspector
    "_inspector", "_inspector_pages", "_inspector_tool_buttons",
    "_insp_fields", "_insp_clear_buttons", "_insp_field_dirty",
    "_insp_draft_item_ids", "_insp_external_status", "_insp_external_review_btn",
    "_insp_lyrics", "_insp_replay_values", "_insp_properties",
    "_op_rows", "_dupes_btn",
    # state
    "_root_folder", "_file_operations", "_cfg",
)

CONTRACT_METHODS = (
    "_on_apply", "_on_revert", "_on_review_changes", "_on_browse", "_on_scan",
    "_on_auto_arrange", "_on_auto_arrange_settings", "_on_clean_settings",
    "_on_metadata_io", "_on_restore_from_backup", "_on_backup_manager",
    "_on_find_duplicates", "_on_online_metadata", "_on_action_engine",
    "_on_more_columns", "_on_header_context_menu",
    "_on_table_context_menu", "_on_tree_context_menu",
    "_on_tree_rename", "_on_tree_delete", "_on_tree_add_folder",
    "_on_tree_item_moved", "_move_tree_path", "_copy_tree_path",
    "_show_path_properties",
    "_on_insp_apply_fields", "_populate_track_inspector",
    "_commit_inspector_draft", "_discard_inspector_draft",
    "_clear_insp_field", "_mark_insp_field_dirty",
    "_refresh_checked_scope_state", "_refresh_selection_scope_state",
    "_review_blocked_records", "_restore_body_sizes",
    "_apply_display_filter", "_apply_navigation_filter",
    "_rebuild_tree_from_loaded_tracks", "_create_action_engine_dialog",
    "_select_inspector_tool", "_size_all_columns_to_fit", "_size_column_to_fit",
    "_set_zoom", "_toggle_selected_apply_exclusion",
    "_on_navigate_back", "_on_navigate_forward", "_on_navigate_up",
    "_open_tracks", "_reveal_tracks", "_copy_paths", "_rename_tracks",
    "_move_tracks", "_show_properties", "_request_delete_files",
)


def test_every_contract_attribute_survives(panel):
    missing = [name for name in CONTRACT_ATTRIBUTES if not hasattr(panel, name)]
    assert missing == [], f"Tag Editor lost widget contract: {missing}"


def test_every_contract_method_survives(panel):
    missing = [
        name for name in CONTRACT_METHODS
        if not callable(getattr(panel, name, None))
    ]
    assert missing == [], f"Tag Editor lost method contract: {missing}"


def test_reference_apply_and_move_dialogs_are_real_tag_editor_modals(panel, tmp_path):
    """The prototype's modal functions are production dialogs, not mock JS."""
    from types import SimpleNamespace
    from PySide6.QtWidgets import QFrame

    from ui.panels.metadata_editor.dialogs import (
        ApplyConfirmationDialog,
        ApplyResultDialog,
        MovePathDialog,
        PropertiesDialog,
    )

    summary = SimpleNamespace(
        changed_fields=4,
        filename_changes=1,
        excluded_files=0,
    )
    dialogs = [
        ApplyConfirmationDialog(
            summary, candidate_count=2, blocker_count=0, parent=panel),
        ApplyResultDialog(error_message="write failed", parent=panel),
        MovePathDialog(tmp_path / "song.mp3", [tmp_path], parent=panel),
        PropertiesDialog(
            [("song.mp3", [("Path", str(tmp_path / "song.mp3"))])],
            parent=panel,
        ),
    ]
    try:
        for dialog in dialogs:
            assert dialog.property("tagEditorDialog") is True
            assert dialog.findChild(QFrame, "dialogHeaderFrame") is not None
        move_dialog = dialogs[2]
        move_dialog._combo.setCurrentIndex(0)
        move_dialog._accept_destination()
        assert move_dialog.destination == tmp_path
    finally:
        for dialog in dialogs:
            dialog.deleteLater()


# --------------------------------------------------------------------------- #
# 2. Signals — the entire controller-facing API
# --------------------------------------------------------------------------- #

CONTRACT_SIGNALS = (
    "scan_requested", "auto_requested", "auto_sequence_requested",
    "apply_requested", "revert_requested", "undo_requested", "redo_requested",
    "review_include_requested", "review_revert_records_requested",
    "review_revert_files_requested", "review_opened", "unsaved_choice_requested",
    "restore_requested", "undo_applied_requested",
    "draft_restore_requested", "draft_discard_requested",
    "recover_requested", "keep_recovery_requested", "forget_recovery_requested",
    "artist_to_scope", "album_to_scope",
    "title_from_filename", "track_from_filename", "rename_from_title",
    "split_artist_title", "album_artist_from_artist",
    "clear_comments", "clear_track_num", "clear_year", "clear_genre",
    "clear_title", "clear_artist", "clear_album", "clear_album_artist",
    "normalize_title_spaces", "strip_web_junk",
    "clean_filename", "strip_filename_numbering",
    "replaygain_track_requested", "replaygain_album_requested",
    "replaygain_cancel_requested",
    "incompatible_disk_action_requested",
    "find_duplicates_requested", "delete_duplicates_requested",
    "revalidate_problems_requested",
    "problem_fix_requested", "problem_fix_preview_requested",
    "problem_fix_accept_requested",
    "online_search_requested", "online_cancel_requested",
    "online_preview_requested", "online_artwork_requested",
    "online_accept_requested",
    "delete_files_requested", "manual_refresh_requested",
    "conflict_resolution_requested",
)


def test_every_controller_facing_signal_survives(panel):
    missing = [name for name in CONTRACT_SIGNALS if not hasattr(panel, name)]
    assert missing == [], f"Tag Editor lost signal contract: {missing}"


# --------------------------------------------------------------------------- #
# 3. Controller slots — what AppWindow calls back into
# --------------------------------------------------------------------------- #

CONTRACT_SLOTS = (
    "on_track_discovered", "on_tracks_discovered", "on_scan_progress",
    "on_scan_error", "on_scan_complete", "on_auto_rules_applied",
    "on_monitoring_state_changed", "on_external_changes_updated",
    "on_workspace_refresh_applied", "on_conflict_resolution_finished",
    "on_apply_started", "on_apply_progress", "on_apply_file_outcome",
    "on_apply_error", "on_apply_batch_complete",
    "on_recovery_available", "on_status_update", "on_draft_available",
    "on_unsaved_changes_action_required",
    "on_duplicate_scan_progress", "on_duplicate_scan_complete",
    "on_duplicate_scan_error", "on_duplicate_delete_complete",
    "on_restore_complete", "on_restore_started", "on_restore_progress",
    "on_validation_updated",
    "on_problem_fix_preview", "on_problem_fix_preview_failed",
    "on_online_lookup_started", "on_online_lookup_finished",
    "on_online_release_detail_finished", "on_online_match_preview",
    "on_online_artwork_ready", "on_online_artwork_error",
    "on_online_acceptance_error", "on_online_acceptance_complete",
    "on_replaygain_analysis_started", "on_replaygain_analysis_progress",
    "on_replaygain_analysis_complete",
    "on_metadata_io_started", "on_metadata_io_finished", "on_metadata_io_error",
    "on_workspace_replacement_started",
)


def test_every_controller_slot_survives(panel):
    missing = [
        name for name in CONTRACT_SLOTS
        if not callable(getattr(panel, name, None))
    ]
    assert missing == [], f"Tag Editor lost controller slot: {missing}"


# --------------------------------------------------------------------------- #
# 4. The 21 metadata fields
# --------------------------------------------------------------------------- #

EXPECTED_FIELDS = (
    # 10 basic
    "title", "artist", "album", "album_artist", "track_num", "track_total",
    "disc_num", "disc_total", "year", "genre",
    # 11 advanced
    "comment", "composer", "publisher", "copyright", "bpm", "isrc", "grouping",
    "sort_title", "sort_artist", "sort_album", "sort_album_artist",
)


def test_all_twenty_one_metadata_fields_exist(panel):
    assert len(EXPECTED_FIELDS) == 21
    assert set(panel._insp_fields) == set(EXPECTED_FIELDS)


def test_every_field_has_its_own_clear_button(panel):
    assert set(panel._insp_clear_buttons) == set(EXPECTED_FIELDS)


# --------------------------------------------------------------------------- #
# 5. The 17 magic operations
# --------------------------------------------------------------------------- #

EXPECTED_OPS = (
    "title_strip", "title_full", "normalize_spaces", "track_num", "split_at",
    "album_artist", "strip_junk",
    "clear_comments", "clear_track_num", "clear_year", "clear_genre",
    "clear_title", "clear_artist", "clear_album", "clear_album_artist",
    "clean_filename", "strip_filename_numbering",
)


def test_the_seventeen_original_operations_all_survive():
    """This file exists to catch a *lost* affordance, not to freeze the list.

    It asserted equality, which also made every new operation a test failure.
    A superset still fails the moment one of the seventeen disappears, which
    is the guarantee this file is actually for.
    """
    from ui.panels.metadata_editor.shared import MAGIC_OP_DEFS

    assert len(EXPECTED_OPS) == 17
    defined = {op_id for op_id, _, _ in MAGIC_OP_DEFS}
    assert set(EXPECTED_OPS) <= defined, set(EXPECTED_OPS) - defined


def test_every_operation_has_a_label_and_description():
    from ui.i18n import t
    from ui.panels.metadata_editor.shared import MAGIC_OP_DEFS

    for op_id, label_key, desc_key in MAGIC_OP_DEFS:
        assert t(label_key) != label_key, f"{op_id}: untranslated label"
        assert t(desc_key) != desc_key, f"{op_id}: untranslated description"


def test_every_operation_is_reachable_as_a_row(panel):
    # One OpRow per operation must be built somewhere in the inspector.
    assert len(panel._op_rows) >= len(EXPECTED_OPS)


def test_default_auto_ops_are_a_subset_of_all_ops():
    from ui.panels.metadata_editor.shared import DEFAULT_AUTO_OPS

    assert set(DEFAULT_AUTO_OPS) <= set(EXPECTED_OPS)


# --------------------------------------------------------------------------- #
# 6. Table columns
# --------------------------------------------------------------------------- #

def test_user_selectable_columns_are_offered_in_the_picker(panel):
    """Every named data column is offered; fixed utility columns are not."""
    from ui.models.metadata_table_model import COLUMN_COUNT, COL_CHECK, COL_END_GUTTER, COL_GUTTER, _HEADER_KEYS

    offered = {
        col for col in range(COLUMN_COUNT)
        if col not in (COL_CHECK, COL_GUTTER, COL_END_GUTTER) and _HEADER_KEYS[col]
    }
    # 14 data columns today; the redesign adds a status column (15).
    assert len(offered) >= 14


def test_filename_column_can_never_be_hidden(panel):
    from ui.models.metadata_table_model import COL_FILENAME

    assert not panel._table.isColumnHidden(COL_FILENAME)


def test_checkbox_and_empty_gutter_are_distinct_fixed_columns(panel, tmp_path):
    """The narrow checkbox column sits directly beside the smaller gutter."""
    from PySide6.QtCore import Qt
    from ui.models.metadata_table_model import COL_CHECK, COL_END_GUTTER, COL_FILENAME, COL_GUTTER, COL_STATUS

    _loaded_panel(panel, tmp_path)
    index = panel._model.index(0, COL_CHECK)
    assert not panel._table.isColumnHidden(COL_CHECK)
    header = panel._table.horizontalHeader()
    assert header.visualIndex(COL_GUTTER) == 0
    assert header.visualIndex(COL_CHECK) == 1
    assert header.visualIndex(COL_STATUS) == panel._model.columnCount() - 2
    assert header.visualIndex(COL_END_GUTTER) == panel._model.columnCount() - 1
    assert panel._table.columnWidth(COL_GUTTER) == panel._table._SIDE_EMPTY_GUTTER
    assert panel._table.columnWidth(COL_CHECK) == panel._table._CHECK_COLUMN_WIDTH
    assert panel._table.columnWidth(COL_END_GUTTER) == panel._table._END_EMPTY_GUTTER
    assert panel._table._SIDE_EMPTY_GUTTER == 17
    assert panel._table._CHECK_COLUMN_WIDTH == 24
    assert panel._table._END_EMPTY_GUTTER == 9
    assert panel._table.itemDelegateForColumn(COL_FILENAME)._show_checkbox is False
    assert panel._model.data(index, Qt.CheckStateRole) == Qt.Unchecked
    assert panel._model.setData(index, Qt.Checked, Qt.CheckStateRole) is False
    panel._table.selectRow(0)
    assert panel._table.selectionModel().isRowSelected(0, panel._table.rootIndex())


def test_filename_column_can_be_reordered_between_fixed_edges(panel):
    from ui.models.metadata_table_model import COL_CHECK, COL_END_GUTTER, COL_FILENAME, COL_GUTTER, COL_STATUS, COLUMN_COUNT

    header = panel._table.horizontalHeader()
    original = header.visualIndex(COL_FILENAME)
    header.moveSection(original, 4)

    assert header.visualIndex(COL_FILENAME) != original
    assert header.visualIndex(COL_GUTTER) == 0
    assert header.visualIndex(COL_CHECK) == 1
    assert header.visualIndex(COL_STATUS) == COLUMN_COUNT - 2
    assert header.visualIndex(COL_END_GUTTER) == COLUMN_COUNT - 1


def test_status_column_does_not_expand_into_the_leftover_space(panel):
    from ui.models.metadata_table_model import COL_STATUS

    status_width = panel._table.columnWidth(COL_STATUS)
    panel._table.resize(panel._table.width() + 500, panel._table.height())
    panel._fill_leftover_space()

    assert panel._table.columnWidth(COL_STATUS) == status_width


# --------------------------------------------------------------------------- #
# 7. Scope rules — the behavioural invariants stated in the brief
# --------------------------------------------------------------------------- #

def test_edit_scope_is_exactly_the_table_selection(panel, tmp_path):
    tracks = _loaded_panel(panel, tmp_path, count=3)
    state = panel._workspace

    state.set_selected_items([tracks[0], tracks[2]])
    assert state.edit_scope() == [tracks[0], tracks[2]]

    state.set_selected_items([])
    assert state.edit_scope() == []


def test_apply_scope_ignores_selection_and_visibility(panel, tmp_path):
    """Apply covers every non-excluded, non-blocked pending change."""
    tracks = _loaded_panel(panel, tmp_path, count=3)
    state = panel._workspace

    tracks[0].proposed.title = "Changed"
    state.reconcile()

    # Nothing selected, nothing visible — Apply still sees the change.
    state.set_selected_items([])
    state.set_visible_items([])
    assert tracks[0] in state.apply_candidates()

    # Selecting an unrelated row does not narrow it either.
    state.set_selected_items([tracks[1]])
    assert tracks[0] in state.apply_candidates()


def test_excluding_a_change_removes_it_from_apply_only(panel, tmp_path):
    tracks = _loaded_panel(panel, tmp_path, count=2)
    state = panel._workspace

    tracks[0].proposed.title = "Changed"
    state.reconcile()
    assert tracks[0] in state.apply_candidates()

    state.set_apply_excluded([tracks[0].path], True)
    assert tracks[0] not in state.apply_candidates()
    # The proposal itself is still pending, not discarded.
    assert tracks[0].has_changes
    assert tracks[0] in state.excluded_tracks()


def test_search_filter_never_changes_apply_scope(panel, tmp_path):
    tracks = _loaded_panel(panel, tmp_path, count=3)
    state = panel._workspace

    tracks[0].proposed.title = "Changed"
    state.reconcile()
    before = list(state.apply_candidates())

    panel._search_edit.setText("no-such-file-matches-this")
    assert list(state.apply_candidates()) == before


# --------------------------------------------------------------------------- #
# 8. Context menus
# --------------------------------------------------------------------------- #

def test_table_context_menu_keeps_every_entry(panel, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from ui.i18n import t

    tracks = _loaded_panel(panel, tmp_path, count=2)
    panel._workspace.set_selected_items([tracks[0]])
    captured = _capture_menu(monkeypatch)

    panel._on_table_context_menu(QPoint(5, 5))

    assert captured, "row context menu never opened"
    labels = set(captured[-1])
    for key in ("meta_open_file", "meta_reveal_in_explorer", "meta_copy_path",
                "meta_move_menu", "meta_properties", "meta_delete_menu"):
        assert t(key) in labels, f"row menu lost {key}"


def test_tree_context_menu_keeps_move_and_every_other_entry(
    panel, tmp_path, monkeypatch
):
    """'Move to…' is the entry the prototype dropped — it must survive."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QTreeWidgetItem
    from ui.i18n import t

    _loaded_panel(panel, tmp_path)
    item = QTreeWidgetItem(panel._tree)
    item.setData(0, Qt.UserRole, str(tmp_path))
    item.setData(0, panel._ROLE_IS_FILE, False)
    monkeypatch.setattr(panel._tree, "itemAt", lambda _pos: item)
    captured = _capture_menu(monkeypatch)

    panel._on_tree_context_menu(QPoint(5, 5))

    assert captured, "tree context menu never opened"
    labels = set(captured[-1])
    for key in ("meta_reveal_in_explorer", "meta_copy_path", "meta_properties",
                "meta_add_folder", "meta_rename_menu", "meta_move_menu",
                "meta_delete_menu"):
        assert t(key) in labels, f"tree menu lost {key}"


def test_header_context_menu_keeps_sizing_and_more_columns(
    panel, tmp_path, monkeypatch
):
    """The prototype has no header menu; these entries still have to exist."""
    from PySide6.QtCore import QPoint
    from ui.i18n import t

    _loaded_panel(panel, tmp_path)
    captured = _capture_menu(monkeypatch)

    panel._on_header_context_menu(QPoint(5, 5))

    assert captured, "header context menu never opened"
    labels = set(captured[-1])
    assert t("mt_size_all_to_fit") in labels
    assert t("mt_more_columns") in labels


# --------------------------------------------------------------------------- #
# 9. Keyboard shortcuts
# --------------------------------------------------------------------------- #

def test_toolbar_shortcuts_are_bound(panel):
    assert panel._undo_btn.shortcut().toString() == "Ctrl+Z"
    assert panel._redo_btn.shortcut().toString() == "Ctrl+Y"
    assert panel._review_btn.shortcut().toString() == "Ctrl+Shift+R"


def _detached_view(panel, tmp_path, *, count: int = 3):
    """A view bound to real data but not to the panel's handlers.

    The shortcuts under test belong to ``ExplorerDetailsView``. Driving the
    panel's own table would also fire the panel's slots, which open modal
    confirm/rename dialogs — those fault under the offscreen platform and would
    be testing the panel's wiring rather than the view's key contract.
    """
    from PySide6.QtWidgets import QAbstractItemView
    from ui.panels.metadata_editor.explorer_view import ExplorerDetailsView

    _loaded_panel(panel, tmp_path, count=count)
    view = ExplorerDetailsView()
    view.setModel(panel._proxy)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection)
    return view


@pytest.mark.parametrize(
    "key, signal_name",
    [
        ("Key_Delete", "deleteRequested"),
        ("Key_F2", "renameRequested"),
        ("Key_Return", "openRequested"),
    ],
)
def test_table_view_keyboard_actions_survive(panel, tmp_path, key, signal_name):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    view = _detached_view(panel, tmp_path, count=2)
    try:
        view.selectRow(0)
        view.setCurrentIndex(view.model().index(0, 0))

        received = []
        getattr(view, signal_name).connect(lambda payload: received.append(payload))

        view.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, getattr(Qt, key), Qt.NoModifier)
        )

        assert received, f"{key} no longer emits {signal_name}"
    finally:
        view.setModel(None)
        view.deleteLater()


def test_select_all_and_escape_still_drive_selection(panel, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    view = _detached_view(panel, tmp_path, count=3)
    try:
        view.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, Qt.Key_A, Qt.ControlModifier)
        )
        assert len(view.selectionModel().selectedRows()) == 3

        view.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        )
        assert view.selectionModel().selectedRows() == []
    finally:
        view.setModel(None)
        view.deleteLater()


# --------------------------------------------------------------------------- #
# 10. Dialogs — every window in the design must have a class behind it
# --------------------------------------------------------------------------- #

def test_every_dialog_class_is_importable():
    from ui.panels.metadata_editor.action_dialog import TagActionDialog
    from ui.panels.metadata_editor.dialogs import (
        AutoArrangeSettingsDialog,
        CleanSettingsDialog,
        MoreColumnsDialog,
    )
    from ui.panels.metadata_editor.io_dialog import MetadataIODialog
    from ui.panels.metadata_editor.online_metadata_dialog import (
        OnlineMetadataDialog,
    )

    for cls in (
        TagActionDialog, AutoArrangeSettingsDialog, CleanSettingsDialog,
        MoreColumnsDialog, MetadataIODialog, OnlineMetadataDialog,
    ):
        assert isinstance(cls, type)


def test_io_hub_offers_all_seven_operations():
    from ui.panels.metadata_editor.io_dialog import _OPERATIONS

    assert {operation for operation, _ in _OPERATIONS} == {
        "metadata_export", "metadata_import",
        "change_report", "problems_report",
        "playlist",
        "preset_export", "preset_import",
    }


def test_action_engine_keeps_every_preset_operation(panel, tmp_path):
    _loaded_panel(panel, tmp_path)
    dialog = panel._create_action_engine_dialog()
    try:
        for name in (
            "_preset_save_btn", "_preset_update_btn", "_preset_rename_btn",
            "_preset_duplicate_btn", "_preset_delete_btn", "_preset_reset_btn",
            "_preset_transfer_btn",
        ):
            assert hasattr(dialog, name), f"action engine lost {name}"
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------- #
# 11. Inspector capabilities that are easy to drop
# --------------------------------------------------------------------------- #

def test_artwork_keeps_all_six_actions(panel):
    for name in (
        "_insp_artwork_add_btn", "_insp_artwork_replace_btn",
        "_insp_artwork_remove_btn", "_insp_artwork_paste_btn",
        "_insp_artwork_export_btn", "_insp_artwork_revert_btn",
    ):
        assert hasattr(panel, name), f"artwork lost {name}"


def test_replaygain_keeps_all_six_actions_and_five_values(panel):
    for name in (
        "_insp_rg_track_btn", "_insp_rg_album_btn", "_insp_rg_cancel_btn",
        "_insp_rg_clear_track_btn", "_insp_rg_clear_album_btn",
        "_insp_rg_revert_btn",
    ):
        assert hasattr(panel, name), f"replaygain lost {name}"
    assert len(panel._insp_replay_values) == 5


def test_lyrics_keeps_editable_language_and_description(panel):
    """The prototype renders these read-only; they must stay editable."""
    assert not panel._insp_lyrics_language.isReadOnly()
    assert not panel._insp_lyrics_description.isReadOnly()
    for name in (
        "_insp_lyrics_set_btn", "_insp_lyrics_clear_btn", "_insp_lyrics_revert_btn",
    ):
        assert hasattr(panel, name), f"lyrics lost {name}"


def test_inspector_exposes_every_tool_page(panel):
    # Eight today; the redesign regroups them but must not reduce the count of
    # reachable pages.
    assert len(panel._inspector_tool_buttons) >= 8
    assert panel._inspector_pages.count() >= 8


# --------------------------------------------------------------------------- #
# 12. Editing never writes to disk
# --------------------------------------------------------------------------- #

def test_field_edits_only_produce_proposals(panel, tmp_path):
    """The core safety rule: only Apply touches the filesystem."""
    tracks = _loaded_panel(panel, tmp_path, count=1)
    panel._workspace.set_selected_items([tracks[0]])

    panel._mark_insp_field_dirty("title", "Proposed Title")
    panel._on_insp_apply_fields()

    assert tracks[0].proposed.title == "Proposed Title"
    # Untouched on disk: the original is still what the scan read.
    assert tracks[0].original.title == "Song 0"


def test_panel_refuses_file_mutations_without_a_controller_owner(panel, tmp_path):
    """No owner installed => refuse, never perform an unmonitored mutation."""
    panel._file_operation_owner = None
    assert panel._run_file_operation("rename_path", tmp_path, "x") is None
