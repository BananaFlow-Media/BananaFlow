"""Focused Phase 6 tests for the pure unified Inspector state."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.metadata_inspector import CapabilityCoverage, MetadataInspectorState, ValueState
from core.metadata_models import AudioTrackItem, LyricsEntry, LyricsValue, OriginalTags, LYRICS_FIELD
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.models.metadata_table_model import COL_FILENAME
from ui.panels.metadata_editor.panel import MetadataEditorPanel


def _track(tmp_path: Path, name: str, *, title: str = "", format_id: str = "mp3", editable: bool = True):
    path = tmp_path / name
    return AudioTrackItem(
        path=path,
        folder=tmp_path,
        ext=path.suffix,
        format_id=format_id,
        metadata_editable=editable,
        original=OriginalTags(title=title),
    )


def _panel_with_tracks(tracks):
    QApplication.instance() or QApplication([])
    panel = MetadataEditorPanel()
    panel._model.load_tracks(tracks)
    QApplication.processEvents()
    return panel


def _select(panel, tracks):
    selection = panel._table.selectionModel()
    selection.clearSelection()
    flags = (
        QItemSelectionModel.SelectionFlag.Select
        | QItemSelectionModel.SelectionFlag.Rows
    )
    for track in tracks:
        row = next(
            row for row in range(panel._proxy.rowCount())
            if panel._proxy.track_at_row(row) is track
        )
        selection.select(panel._proxy.index(row, 0), flags)
    QApplication.processEvents()


def _type_title(panel, value: str):
    edit = panel._insp_fields["title"]
    edit.clear()
    QTest.keyClicks(edit, value)
    QApplication.processEvents()


def test_single_selection_and_proposed_value_display(tmp_path):
    inspector = MetadataInspectorState()
    track = _track(tmp_path, "one.mp3", title="Stored")
    assert inspector.field_state([track], "title").value == "Stored"
    inspector.propose_set([track], "title", "Proposed")
    state = inspector.field_state([track], "title")
    assert state.value_state is ValueState.VALUE
    assert state.value == "Proposed"
    assert state.pending_count == 1


def test_mixed_state_never_becomes_clear_without_explicit_action(tmp_path):
    inspector = MetadataInspectorState()
    first = _track(tmp_path, "a.mp3", title="A")
    second = _track(tmp_path, "b.mp3", title="B")
    state = inspector.field_state([first, second], "title")
    assert state.value_state is ValueState.MIXED and state.value is None
    assert first.proposed.title is None and second.proposed.title is None
    inspector.propose_set([first, second], "artist", "Shared")
    assert first.proposed.title is None and second.proposed.title is None


def test_explicit_clear_is_distinct_and_enters_pending_scope(tmp_path):
    inspector = MetadataInspectorState()
    track = _track(tmp_path, "one.mp3", title="Stored")
    inspector.propose_clear([track], "title")
    assert track.proposed.title == ""
    assert track.has_changes
    assert inspector.field_state([track], "title").value_state is ValueState.EMPTY


def test_partial_capability_reports_exact_affected_count(tmp_path):
    inspector = MetadataInspectorState()
    supported = _track(tmp_path, "one.mp3", title="A")
    read_only = _track(tmp_path, "two.aac", title="B", format_id="aac", editable=False)
    state = inspector.field_state([supported, read_only], LYRICS_FIELD)
    assert state.capability is CapabilityCoverage.SOME
    result = inspector.propose_set([supported, read_only], "title", "Shared")
    assert (result.affected_count, result.unsupported_count) == (1, 1)
    assert supported.proposed.title == "Shared"
    assert read_only.proposed.title is None


def test_refresh_token_uses_stable_workspace_identity(tmp_path):
    state = TagEditorWorkspaceState()
    first = _track(tmp_path, "one.mp3")
    second = _track(tmp_path, "two.mp3")
    state.set_tracks([first, second])
    inspector = MetadataInspectorState()
    token = inspector.token(4, [first], state.item_id)
    first.path = tmp_path / "renamed.mp3"
    assert inspector.token_is_current(token, 4, [first], state.item_id)
    assert not inspector.token_is_current(token, 4, [second], state.item_id)
    assert not inspector.token_is_current(token, 5, [first], state.item_id)


def test_selection_visibility_changes_do_not_alter_pending_apply_scope(tmp_path):
    state = TagEditorWorkspaceState()
    first = _track(tmp_path, "one.mp3", title="A")
    second = _track(tmp_path, "two.mp3", title="B")
    state.set_tracks([first, second])
    MetadataInspectorState().propose_set([first], "title", "Changed")
    state.set_selected_items([second])
    state.set_visible_items([second])
    assert state.apply_candidates() == [first]
    assert state.edit_scope() == [second]


def test_multi_lyrics_text_replacement_preserves_each_primary_variant_metadata(tmp_path):
    inspector = MetadataInspectorState()
    first = _track(tmp_path, "one.mp3")
    second = _track(tmp_path, "two.mp3")
    first.original.lyrics = LyricsValue((LyricsEntry("A", "heb", "Hebrew", source="USLT"),))
    second.original.lyrics = LyricsValue((LyricsEntry("B", "eng", "Lyrics", source="USLT"),))
    inspector.propose_set([first, second], LYRICS_FIELD, "Shared\nטקסט")
    first_value = first.proposed.effective_tags(first.original).lyrics.primary
    second_value = second.proposed.effective_tags(second.original).lyrics.primary
    assert (first_value.language, first_value.description, first_value.source) == ("heb", "Hebrew", "USLT")
    assert (second_value.language, second_value.description, second_value.source) == ("eng", "Lyrics", "USLT")


def test_real_panel_draft_cannot_cross_selection_and_commits_original_target(tmp_path):
    first = _track(tmp_path, "a.mp3", title="A")
    second = _track(tmp_path, "b.mp3", title="B")
    panel = _panel_with_tracks([first, second])
    try:
        _select(panel, [first])
        first_id = panel._workspace.item_id(first)
        _type_title(panel, "Draft A")
        assert panel._insp_draft_item_ids == (first_id,)

        _select(panel, [second])
        panel._on_insp_apply_fields()

        assert first.proposed.title == "Draft A"
        assert second.proposed.title is None
        assert panel._insp_field_dirty == set()
    finally:
        panel.deleteLater()


def test_real_panel_multiselection_draft_stays_with_exact_id_snapshot(tmp_path):
    first = _track(tmp_path, "a.mp3", title="A")
    second = _track(tmp_path, "b.mp3", title="B")
    third = _track(tmp_path, "c.mp3", title="C")
    panel = _panel_with_tracks([first, second, third])
    try:
        _select(panel, [first, second])
        expected_ids = tuple(sorted(
            (panel._workspace.item_id(first), panel._workspace.item_id(second))
        ))
        _type_title(panel, "Shared AB")
        assert panel._insp_draft_item_ids == expected_ids

        _select(panel, [third])
        panel._on_insp_apply_fields()
        assert first.proposed.title == second.proposed.title == "Shared AB"
        assert third.proposed.title is None
    finally:
        panel.deleteLater()


def test_real_panel_sort_filter_and_path_change_cannot_remap_draft_identity(tmp_path):
    first = _track(tmp_path, "z.mp3", title="Z")
    second = _track(tmp_path, "a.mp3", title="A")
    panel = _panel_with_tracks([first, second])
    try:
        _select(panel, [first])
        stable_id = panel._workspace.item_id(first)
        _type_title(panel, "Stable draft")
        first.path = tmp_path / "renamed.mp3"
        first.folder = tmp_path
        panel._proxy.sort(COL_FILENAME, Qt.SortOrder.AscendingOrder)
        panel._apply_display_filter(lambda: panel._proxy.set_search_text(""))
        QApplication.processEvents()
        assert panel._workspace.item_id(first) == stable_id

        _select(panel, [second])
        panel._on_insp_apply_fields()
        assert first.proposed.title == "Stable draft"
        assert second.proposed.title is None
    finally:
        panel.deleteLater()


def test_real_panel_population_and_mixed_placeholder_create_no_draft(tmp_path):
    first = _track(tmp_path, "a.mp3", title="A")
    second = _track(tmp_path, "b.mp3", title="B")
    panel = _panel_with_tracks([first, second])
    try:
        _select(panel, [first, second])
        panel._populate_track_inspector([first, second])
        assert panel._insp_fields["title"].text() == ""
        assert panel._insp_fields["title"].placeholderText()
        assert panel._insp_field_dirty == set()
        assert panel._insp_draft_item_ids is None
        assert first.proposed.title is None and second.proposed.title is None
    finally:
        panel.deleteLater()
