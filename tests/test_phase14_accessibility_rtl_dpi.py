"""Phase 14: accessibility, keyboard, RTL and DPI safety on production widgets.

These tests instantiate the real Tag Editor panel and dialogs.  The pre-existing
accessibility test greps the panel source for i18n keys, which cannot tell
whether a control actually ends up with a name — the Phase 14 audit found ~30
icon-only buttons that had a tooltip, passed that grep, and were still silent to
a screen reader.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QComboBox, QLineEdit,
    QProgressBar,
)

from core.filesystem_monitoring import ExternalChangeState
from ui import a11y
from ui.i18n import TRANSLATIONS, set_language, t
from ui.panels.metadata_editor.panel import MetadataEditorPanel

INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox, QAbstractItemView)


def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel():
    app()
    value = MetadataEditorPanel()
    yield value
    value.close()
    value.deleteLater()
    QApplication.processEvents()


def accessible_name(widget) -> str:
    return (widget.accessibleName() or "").strip()


def visible_text(widget) -> str:
    return (widget.text() or "").strip() if hasattr(widget, "text") else ""


def icon_only_buttons(panel):
    """Buttons whose meaning is not in their visible text."""
    values = []
    for widget in panel.findChildren(QAbstractButton):
        if visible_text(widget):
            continue
        icon = widget.icon()
        if icon is None or icon.isNull():
            continue
        values.append(widget)
    return values


# ── TE-A11Y-SR-01/02: names for icon-only and ambiguous controls ────────────


def test_every_icon_only_button_has_an_accessible_name(panel):
    unnamed = [w for w in icon_only_buttons(panel) if not accessible_name(w)]
    described = [f"{type(w).__name__} tip={w.toolTip()!r}" for w in unnamed]
    assert not unnamed, (
        "icon-only buttons a screen reader cannot announce: " + "; ".join(described))


def test_named_tag_editor_controls_expose_accessible_names(panel):
    for attribute in (
        "_apply_btn", "_revert_btn", "_review_btn", "_undo_btn", "_redo_btn",
        "_dupes_btn", "_manual_refresh_btn", "_monitoring_status",
        "_excluded_chip", "_stale_chip", "_search_edit", "_zoom_minus_btn",
        "_zoom_plus_btn", "_zoom_val_lbl", "_tree", "_table", "_scan_progress",
        "_insp_external_review_btn", "_tree_toggle_btn", "_auto_cfg_btn",
    ):
        widget = getattr(panel, attribute, None)
        assert widget is not None, f"{attribute} is missing"
        assert accessible_name(widget), f"{attribute} has no accessible name"


def test_inspector_fields_and_their_clear_buttons_are_individually_named(panel):
    fields = getattr(panel, "_insp_fields", {})
    assert fields, "inspector fields were not built"
    for name, edit in fields.items():
        assert accessible_name(edit), f"inspector field {name} has no accessible name"
    clears = [w for w in panel.findChildren(QAbstractButton)
              if visible_text(w) == t("meta_inspector_clear_short")]
    assert clears, "inspector clear buttons were not built"
    names = {accessible_name(w) for w in clears}
    # Every row shows the same word "Clear"; the names must still distinguish them.
    assert len(names) == len(clears), f"ambiguous clear buttons: {sorted(names)}"
    assert all(names)


def test_table_tree_and_header_expose_semantics(panel):
    assert accessible_name(panel._tree)
    assert (panel._tree.accessibleDescription() or "").strip()
    assert accessible_name(panel._table)
    assert (panel._table.accessibleDescription() or "").strip()
    assert accessible_name(panel._table.horizontalHeader())


# ── TE-A11Y-SR-03/05: state and progress are not animation or colour alone ──


def test_filter_chips_describe_what_they_filter_and_their_state(panel):
    for chip in (panel._excluded_chip, panel._stale_chip):
        assert chip.isCheckable()
        assert accessible_name(chip)
        assert (chip.accessibleDescription() or "").strip(), (
            "a chip's highlight is its only state cue without a description")


def test_stale_chip_name_tracks_its_count(panel):
    from core.metadata_models import AudioTrackItem, OriginalTags

    tracks = [AudioTrackItem(Path(f"C:/p14/{index}.mp3"), Path("C:/p14"), ".mp3",
                             original=OriginalTags(title=f"T{index}"),
                             format_id="mp3", metadata_editable=True)
              for index in range(3)]
    panel._model.load_tracks(tracks)
    for track in tracks:
        panel._workspace.set_external_state(
            panel._workspace.item_id(track), ExternalChangeState.CONFLICT.value)
    panel.on_external_changes_updated(3)
    # The label a sighted user reads and the name a screen reader hears are the
    # same string, so a count can never be announced stale.
    assert accessible_name(panel._stale_chip) == panel._stale_chip.text()
    assert "3" in accessible_name(panel._stale_chip)

    for track in tracks:
        panel._workspace.set_external_state(
            panel._workspace.item_id(track), ExternalChangeState.CURRENT.value)
    panel.on_external_changes_updated(0)
    assert accessible_name(panel._stale_chip) == panel._stale_chip.text()
    assert "0" in accessible_name(panel._stale_chip)


def test_scan_progress_is_available_as_text_not_only_animation(panel):
    assert not panel._scan_progress.isTextVisible()
    panel.on_scan_progress(3, 10)
    description = (panel._scan_progress.accessibleDescription() or "").strip()
    assert description and "3" in description and "10" in description
    assert "3" in panel._summary_lbl.text()


def test_active_inspector_tool_is_a_checked_state_not_only_a_background(panel):
    buttons = panel._inspector_tool_buttons
    assert buttons and all(button.isCheckable() for button in buttons)
    panel._select_inspector_tool(1)
    assert buttons[1].isChecked()
    assert not buttons[0].isChecked()
    panel._select_inspector_tool(0)
    assert buttons[0].isChecked() and not buttons[1].isChecked()


def test_every_external_state_has_localized_text_not_colour_alone():
    for state in ExternalChangeState:
        key = f"meta_external_state_{state.value}"
        for language in ("en", "he"):
            set_language(language)
            label = t(key)
            assert label and label != key, f"{key} missing for {language}"
    set_language("en")


# ── TE-A11Y-KB: keyboard reachability and focus ─────────────────────────────


def test_disabled_controls_do_not_take_focus(panel):
    panel._apply_btn.setEnabled(False)
    assert not panel._apply_btn.hasFocus()
    panel._apply_btn.setFocus(Qt.OtherFocusReason)
    assert not panel._apply_btn.hasFocus(), "a disabled control must not trap focus"


def test_primary_controls_are_keyboard_focusable(panel):
    for attribute in ("_search_edit", "_table", "_tree", "_apply_btn",
                      "_review_btn", "_stale_chip", "_manual_refresh_btn"):
        widget = getattr(panel, attribute)
        assert widget.focusPolicy() != Qt.NoFocus, f"{attribute} is not focusable"


def test_table_offers_a_keyboard_context_menu(panel, monkeypatch):
    """Shift+F10 and the Menu key must reach the menu the mouse reaches."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QKeyEvent

    assert panel._table.contextMenuPolicy() == Qt.CustomContextMenu
    opened = []
    monkeypatch.setattr(panel, "_on_table_context_menu", lambda pos: opened.append(pos))
    panel._table.keyboardContextMenuRequested.connect(panel._on_table_context_menu)
    for key, modifier in ((Qt.Key_Menu, Qt.NoModifier), (Qt.Key_F10, Qt.ShiftModifier)):
        opened.clear()
        panel._table.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, key, modifier))
        assert opened, f"key {key} did not request a context menu"
        assert isinstance(opened[0], QPoint)


def test_tree_offers_a_keyboard_context_menu(panel, monkeypatch):
    """The proven gap: ExplorerTreeWidget had no keyPressEvent override at
    all, so Menu/Shift+F10 silently did nothing on the tree even though the
    table already supported both. Shift+F10 and the Menu key must reach the
    same menu the mouse reaches."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QKeyEvent

    assert panel._tree.contextMenuPolicy() == Qt.CustomContextMenu
    opened = []
    monkeypatch.setattr(panel, "_on_tree_context_menu", lambda pos: opened.append(pos))
    panel._tree.keyboardContextMenuRequested.connect(panel._on_tree_context_menu)
    for key, modifier in ((Qt.Key_Menu, Qt.NoModifier), (Qt.Key_F10, Qt.ShiftModifier)):
        opened.clear()
        panel._tree.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, key, modifier))
        assert opened, f"key {key} did not request a context menu on the tree"
        assert isinstance(opened[0], QPoint)


def test_tree_keyboard_navigation_still_works_after_the_context_menu_fix():
    """The tree's keyPressEvent override must fall through to Qt's own
    navigation for every key it does not own itself."""
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QTreeWidgetItem
    from ui.panels.metadata_editor.tree import ExplorerTreeWidget

    app()
    tree = ExplorerTreeWidget()
    root = QTreeWidgetItem(tree, ["root"])
    QTreeWidgetItem(root, ["child"])
    tree.setCurrentItem(root)
    root.setExpanded(False)
    tree.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    assert root.isExpanded(), "arrow-key navigation must still reach QTreeWidget"
    tree.deleteLater()
    QApplication.processEvents()


def test_focus_restored_after_returns_focus_to_a_live_enabled_initiator():
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    app()
    window = QWidget()
    layout = QVBoxLayout(window)
    initiator = QPushButton(); other = QPushButton()
    layout.addWidget(initiator); layout.addWidget(other)
    window.show()
    QApplication.processEvents()
    try:
        other.setFocus(Qt.OtherFocusReason)
        QApplication.processEvents()
        with a11y.focus_restored_after(initiator):
            other.setFocus(Qt.OtherFocusReason)  # simulates the dialog stealing focus
            QApplication.processEvents()
        assert initiator.hasFocus()
    finally:
        window.deleteLater()
        QApplication.processEvents()


def test_focus_restored_after_skips_a_disabled_or_destroyed_initiator():
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    app()
    window = QWidget()
    layout = QVBoxLayout(window)
    disabled = QPushButton(); other = QPushButton()
    layout.addWidget(disabled); layout.addWidget(other)
    window.show()
    try:
        with a11y.focus_restored_after(disabled):
            disabled.setEnabled(False)
            other.setFocus(Qt.OtherFocusReason)
        assert not disabled.hasFocus(), "a disabled initiator must not be focused"

        doomed = QPushButton()
        with a11y.focus_restored_after(doomed):
            doomed.deleteLater()
            QApplication.processEvents()
        # No RuntimeError from touching a deleted C++ object -- reaching this
        # line at all is the assertion.
    finally:
        window.deleteLater()
        QApplication.processEvents()


def test_tag_editor_shortcuts_do_not_silently_conflict(panel):
    seen: dict[str, list[str]] = {}
    for widget in panel.findChildren(QAbstractButton):
        sequence = widget.shortcut() if hasattr(widget, "shortcut") else None
        if sequence is None or sequence.isEmpty():
            continue
        label = f"{type(widget).__name__}:{widget.objectName() or visible_text(widget)}"
        seen.setdefault(sequence.toString(), []).append(label)
    conflicts = {key: owners for key, owners in seen.items() if len(owners) > 1}
    assert not conflicts, f"shortcut conflicts within the Tag Editor panel: {conflicts}"


def test_destructive_delete_is_not_an_accidental_default(panel):
    assert not panel._apply_btn.isDefault() if hasattr(panel._apply_btn, "isDefault") else True
    for button in panel.findChildren(QAbstractButton):
        if hasattr(button, "isDefault") and button.isDefault():
            assert t("meta_delete_menu") not in visible_text(button)


# ── TE-RTL: direction follows language; technical values stay LTR ───────────


def test_hebrew_sets_rtl_and_english_sets_ltr():
    from ui.direction import apply_app_direction
    application = app()
    try:
        apply_app_direction(application, "he")
        assert application.layoutDirection() == Qt.RightToLeft
        apply_app_direction(application, "en")
        assert application.layoutDirection() == Qt.LeftToRight
    finally:
        apply_app_direction(application, "en")


def test_technical_values_are_direction_isolated():
    from ui.direction import isolate_latin, isolate_ltr
    for value in ("C:/music/song.mp3", "audio/mpeg", "USZZ11700001", "44100 Hz",
                  "mp3", "1411 kbps", "3000x3000"):
        isolated = isolate_ltr(value)
        assert isolated.startswith("⁦") and isolated.endswith("⁩")
        assert value in isolated
        # The value-aware helper must reach the same conclusion on its own.
        assert isolate_latin(value) == isolated


def test_hebrew_values_are_never_forced_left_to_right():
    """Isolating unconditionally would mangle a Hebrew title into LTR."""
    from ui.direction import isolate_latin

    for value in ("אור", "שיר בעברית", "אלבום 2024"):
        assert isolate_latin(value) == value, "Hebrew must keep its own direction"
    assert isolate_latin("") == ""


def test_review_dialog_keeps_hebrew_field_values_in_their_own_direction(tmp_path):
    from core.change_sets import FileIdentity
    from core.file_refresh_service import FieldDifference, StaleFileConflict
    from ui.dialogs.external_change_dialog import ExternalChangeReviewDialog
    from PySide6.QtWidgets import QTableWidget

    app()
    path = tmp_path / "song.mp3"
    differences = (
        FieldDifference("title", "אור", "אור חדש", "אור שלי", True, True, True),
        FieldDifference("filename", "song.mp3", "song.mp3", "new.mp3", False, True, False),
    )
    conflict = StaleFileConflict(
        "cid", 1, path, path, ExternalChangeState.CONFLICT,
        FileIdentity(str(path), 1, 2, 1, 1), FileIdentity(str(path), 3, 4, 1, 2),
        None, differences, False, 1, 1, 1, "external_change", "session")
    dialog = ExternalChangeReviewDialog(conflict)
    try:
        table = dialog.findChild(QTableWidget)
        assert table.item(0, 1).text() == "אור", "a Hebrew title must not be isolated"
        assert table.item(1, 1).text().startswith("⁦"), "a filename must be isolated"
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_review_dialog_isolates_a_hebrew_filename_as_a_technical_value(tmp_path):
    """The proven gap: a value's alphabet cannot say what direction it needs.
    ``isolate_latin`` isolates only Latin-with-no-Hebrew content, so a Hebrew
    filename like ``שיר.mp3`` used to pass through untouched even though it is
    a technical value, not prose, and must stay one LTR unit in the Hebrew UI."""
    from core.change_sets import FileIdentity
    from core.file_refresh_service import FieldDifference, StaleFileConflict
    from ui.dialogs.external_change_dialog import ExternalChangeReviewDialog
    from PySide6.QtWidgets import QTableWidget

    app()
    path = tmp_path / "שיר.mp3"
    differences = (
        FieldDifference("filename", "שיר.mp3", "שיר.mp3", "שיר חדש.mp3",
                        False, True, False),
    )
    conflict = StaleFileConflict(
        "cid", 1, path, path, ExternalChangeState.CONFLICT,
        FileIdentity(str(path), 1, 2, 1, 1), FileIdentity(str(path), 3, 4, 1, 2),
        None, differences, False, 1, 1, 1, "external_change", "session")
    dialog = ExternalChangeReviewDialog(conflict)
    try:
        table = dialog.findChild(QTableWidget)
        for column in (1, 2, 3):
            cell = table.item(0, column).text()
            assert cell.startswith("⁦") and cell.endswith("⁩"), (
                f"Hebrew filename in column {column} was not isolated: {cell!r}")
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_isolate_value_for_field_uses_field_identity_not_alphabet():
    from ui.direction import isolate_value_for_field

    technical_hebrew = {
        "filename": "שיר.mp3",
        "relative_path": "אלבום 01\\רצועה 03.flac",
        "absolute_path": "C:\\מוזיקה\\שיר.mp3",
    }
    for field_id, value in technical_hebrew.items():
        isolated = isolate_value_for_field(field_id, value)
        assert isolated.startswith("⁦") and isolated.endswith("⁩"), (
            f"{field_id}={value!r} must be isolated as one technical unit")
        assert value in isolated

    natural_language = {
        "title": "שיר 2024",
        "artist": "Artist ישראלי",
        "album": "אלבום",
    }
    for field_id, value in natural_language.items():
        assert isolate_value_for_field(field_id, value) == value, (
            f"{field_id}={value!r} must keep its own natural direction")

    always_technical_ascii = {
        "filename": "song.mp3",
        "isrc": "USZZ11700001",
        "url": "https://example.com/track",
        "mime_type": "audio/mpeg",
        "codec": "mp3",
    }
    for field_id, value in always_technical_ascii.items():
        isolated = isolate_value_for_field(field_id, value)
        assert isolated.startswith("⁦") and isolated.endswith("⁩")

    assert isolate_value_for_field("title", None) == ""
    assert isolate_value_for_field("title", "") == ""
    assert isolate_value_for_field("filename", None) == ""
    isolated_tuple = isolate_value_for_field("filename", ("שיר.mp3", "שיר2.mp3"))
    assert isolated_tuple.startswith("⁦") and "שיר.mp3; שיר2.mp3" in isolated_tuple
    # An unknown field name falls back to the existing content-based heuristic.
    assert isolate_value_for_field("some_future_field", "אור") == "אור"
    assert isolate_value_for_field("some_future_field", "C:/x.txt").startswith("⁦")


def test_navigation_arrows_stay_logical_in_hebrew(panel):
    # Back must remain Back in RTL: the label is translated, not mirrored onto
    # the forward action.
    set_language("he")
    try:
        assert t("meta_nav_back") != t("meta_nav_forward")
    finally:
        set_language("en")


def test_core_modules_carry_no_hebrew_layout_assumption():
    root = Path(__file__).resolve().parent.parent
    for module in ("core/filesystem_monitoring.py", "core/file_refresh_service.py",
                   "core/change_sets.py", "core/metadata_models.py"):
        source = (root / module).read_text(encoding="utf-8")
        assert "RightToLeft" not in source
        assert not any("֐" <= ch <= "׿" for ch in source), (
            f"{module} contains Hebrew text; direction is a UI concern")


def test_english_and_hebrew_expose_the_same_phase14_keys():
    for key in ("meta_a11y_excluded_filter_desc", "meta_a11y_external_filter_desc",
                "meta_a11y_clear_named_field", "meta_a11y_about_action",
                "meta_a11y_configure_action", "meta_a11y_scan_progress",
                "meta_external_state_root_unavailable"):
        assert key in TRANSLATIONS["en"], f"{key} missing from English"
        assert key in TRANSLATIONS["he"], f"{key} missing from Hebrew"


# ── Phase 13 conflict dialog: names, safe default, no private path ─────────


def review_dialog(tmp_path):
    from core.change_sets import FileIdentity
    from core.file_refresh_service import FieldDifference, StaleFileConflict

    observed = tmp_path / "private album" / "song.mp3"
    differences = (
        FieldDifference("title", "Old", "Disk", "Local", True, True, True),
        FieldDifference("filename", "song.mp3", "song.mp3", "new.mp3", False, True, False),
    )
    conflict = StaleFileConflict(
        "cid", 1, observed, observed, ExternalChangeState.CONFLICT,
        FileIdentity(str(observed), 1, 2, 1, 1), FileIdentity(str(observed), 3, 4, 1, 2),
        None, differences, False, 1, 1, 1, "external_change", "session")
    from ui.dialogs.external_change_dialog import ExternalChangeReviewDialog
    return ExternalChangeReviewDialog(conflict)


def test_review_dialog_names_the_file_without_leaking_its_absolute_path(tmp_path):
    from PySide6.QtWidgets import QLabel

    app()
    dialog = review_dialog(tmp_path)
    try:
        texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "song.mp3" in texts
        assert str(tmp_path) not in texts
        assert "private album" not in texts
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_review_dialog_defaults_to_cancel_and_never_to_a_resolution(tmp_path):
    app()
    dialog = review_dialog(tmp_path)
    try:
        from core.file_refresh_service import ConflictResolutionAction

        assert dialog.selected_action is ConflictResolutionAction.CANCEL
        defaults = [b for b in dialog.findChildren(QAbstractButton)
                    if getattr(b, "isDefault", lambda: False)()]
        assert defaults and all(visible_text(b) == t("cancel") for b in defaults), (
            "Enter must not silently pick a resolution")
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_review_dialog_explains_why_keep_local_is_disabled(tmp_path):
    app()
    dialog = review_dialog(tmp_path)
    try:
        keep = next(b for b in dialog.findChildren(QAbstractButton)
                    if visible_text(b) == t("meta_external_keep_local"))
        assert not keep.isEnabled(), "an overlapping field must block Keep Local"
        assert (keep.accessibleDescription() or "").strip(), (
            "a disabled button with no reason is a dead end")
        assert accessible_name(keep)
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_review_dialog_isolates_technical_values_for_rtl(tmp_path):
    app()
    dialog = review_dialog(tmp_path)
    try:
        from PySide6.QtWidgets import QTableWidget

        table = dialog.findChild(QTableWidget)
        assert table is not None and accessible_name(table)
        assert (table.accessibleDescription() or "").strip()
        # Column 3 holds the pending value, which may be a filename.
        cell = table.item(1, 3)
        assert cell.text().startswith("⁦") and cell.text().endswith("⁩")
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


# ── TE-DPI: logical geometry is not double-scaled; saved geometry is sane ──


def test_a11y_has_no_generic_widget_scaling_helper():
    """A generic ``scale()`` invites double-scaling QWidget geometry that Qt 6
    already maps through its own High-DPI handling.  It must not come back."""
    assert not hasattr(a11y, "scale")
    assert not hasattr(a11y, "scale_factor")


def test_panel_preserves_a_legitimate_collapsed_profile(panel):
    """Tree and inspector collapsed to their rails is a real, common layout;
    restoring it must not be mistaken for corrupt/degenerate saved data."""
    class Collapsed:
        tag_editor_splitter_sizes = [
            panel._TREE_RAIL_WIDTH, 900, panel._INSPECTOR_RAIL_WIDTH]

    panel._cfg = Collapsed()
    restored = panel._restore_body_sizes()
    assert restored[0] == panel._TREE_RAIL_WIDTH
    assert restored[2] == panel._INSPECTOR_RAIL_WIDTH
    assert restored[1] >= panel._TABLE_OPEN_MIN


def test_panel_restores_pane_widths_as_one_allocation(panel):
    """The production restore path fits the saved profile as one vector: it
    must never hand back three panes that each independently claim the full
    screen width, and stale/malformed data must fall back deterministically."""
    class Degenerate:
        tag_editor_splitter_sizes = [2, 3, 1]  # far below any pane's floor

    panel._cfg = Degenerate()
    restored = panel._restore_body_sizes()
    assert len(restored) == 3
    assert restored[0] >= panel._TREE_RAIL_WIDTH
    assert restored[1] >= panel._TABLE_OPEN_MIN
    assert restored[2] >= panel._INSPECTOR_RAIL_WIDTH

    class Absurd:
        tag_editor_splitter_sizes = [99999, 99999, 99999]

    panel._cfg = Absurd()
    available = max(
        QApplication.primaryScreen().availableGeometry().width(),
        panel._TABLE_OPEN_MIN)
    restored = panel._restore_body_sizes()
    for value in restored:
        assert value <= available
    # The whole allocation, not each pane independently: three panes each
    # claiming the full screen width would still pass a per-pane check.
    assert sum(restored) <= available + panel._TABLE_OPEN_MIN
    # The table keeps its declared minimum whenever mathematically possible:
    # both side panes can always collapse to their rail and still leave
    # enough width for it on any real or offscreen screen.
    assert restored[1] >= panel._TABLE_OPEN_MIN

    # Malformed, negative and missing saved data must all fall back through
    # the exact same path as "no saved profile at all" -- compared against
    # that path rather than a hardcoded number, since the available screen
    # width (and therefore the fitted result) is environment-dependent.
    panel._cfg = None
    default_result = panel._restore_body_sizes()
    assert len(default_result) == 3

    class Broken:
        tag_editor_splitter_sizes = ["wide", None]

    panel._cfg = Broken()
    assert panel._restore_body_sizes() == default_result

    class Negative:
        tag_editor_splitter_sizes = [-10, 0, 5]

    panel._cfg = Negative()
    assert panel._restore_body_sizes() == default_result

    class WrongLength:
        tag_editor_splitter_sizes = [300, 300]

    panel._cfg = WrongLength()
    assert panel._restore_body_sizes() == default_result


def test_saved_pane_widths_are_sanitized_not_trusted_verbatim():
    assert a11y.sanitize_saved_geometry(400, minimum=120, maximum=600, default=200) == 400
    assert a11y.sanitize_saved_geometry(5, minimum=120, maximum=600, default=200) == 120
    assert a11y.sanitize_saved_geometry(5000, minimum=120, maximum=600, default=200) == 600
    for bad in (None, 0, -10, "wide"):
        assert a11y.sanitize_saved_geometry(bad, minimum=120, maximum=600, default=200) == 200


def test_toolbar_actions_can_grow_for_long_labels_instead_of_clipping(panel):
    # A fixed size clips a translated label; a minimum size lets it grow.
    assert panel._apply_btn.maximumWidth() > panel._apply_btn.minimumWidth()
    assert panel._apply_btn.minimumWidth() >= 92


def test_long_hebrew_toolbar_labels_fit_their_buttons(panel):
    set_language("he")
    try:
        for attribute in ("_apply_btn", "_revert_btn", "_review_btn"):
            button = getattr(panel, attribute)
            hint = button.sizeHint()
            assert hint.width() <= button.maximumWidth()
            assert hint.height() >= 1
    finally:
        set_language("en")
