import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTableWidget

from core.change_sets import FileIdentity
from core.file_refresh_service import FieldDifference, StaleFileConflict
from core.filesystem_monitoring import ExternalChangeState, MonitoringState
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.dialogs.external_change_dialog import ExternalChangeReviewDialog
from ui.i18n import set_language, t
from ui.panels.metadata_editor.panel import MetadataEditorPanel


def app():
    return QApplication.instance() or QApplication([])


def conflict(*, safe=True, state=ExternalChangeState.CONFLICT):
    path = Path("C:/music/song.mp3")
    difference = FieldDifference(
        "title", "Old", "Disk", "Local", True, True, not safe)
    return StaleFileConflict(
        "conflict", 1, path, path, state,
        FileIdentity(str(path), 1, 1, 1, 1),
        FileIdentity(str(path), 2, 2, 1, 1), None,
        (difference,), safe, 1, 2, 3, session_id="session")


def track(path, external_state="current", external_conflict=None):
    return AudioTrackItem(
        path, path.parent, ".mp3", original=OriginalTags(title="Title"),
        format_id="mp3", metadata_editable=True,
        baseline_identity=FileIdentity(str(path), 1, 1, 1, 1),
        external_state=external_state, external_conflict=external_conflict)


def test_panel_hides_monitor_status_but_keeps_refresh_and_stale_filter_accessible():
    app(); set_language("en"); panel = MetadataEditorPanel()
    assert not hasattr(panel, "_monitoring_status")
    assert panel._manual_refresh_btn.accessibleName() == t("meta_manual_refresh")
    assert panel._stale_chip.isCheckable()
    panel._root_folder = Path("C:/music")
    panel.on_monitoring_state_changed(MonitoringState.DEGRADED, "watch_limit")
    assert panel._manual_refresh_btn.isEnabled()
    value = track(Path("C:/music/song.mp3"), "conflict", conflict())
    panel._model.load_tracks([value])
    panel.on_external_changes_updated(1)
    assert "1" in panel._stale_chip.text()
    panel.deleteLater()


def test_inspector_shows_external_state_with_text_and_review_control():
    app(); set_language("en"); panel = MetadataEditorPanel()
    value = track(Path("C:/music/song.mp3"), "conflict", conflict())
    panel._model.load_tracks([value])
    panel._workspace.set_selected_items([value])
    panel._populate_track_inspector([value])
    assert t("meta_external_state_conflict") in panel._insp_external_status.text()
    assert panel._insp_external_review_btn.isVisibleTo(panel._insp_external_review_btn.parent())
    panel.deleteLater()


def cell_value(table, row, column) -> str:
    """The cell's value without the invisible direction-isolation marks."""
    return table.item(row, column).text().strip("⁦⁩")


def test_review_dialog_has_field_level_three_way_values_and_explicit_actions():
    app(); set_language("en"); dialog = ExternalChangeReviewDialog(conflict(safe=False))
    table = dialog.findChild(QTableWidget)
    assert table.rowCount() == 1 and table.columnCount() == 5
    assert cell_value(table, 0, 1) == "Old"
    assert cell_value(table, 0, 2) == "Disk"
    assert cell_value(table, 0, 3) == "Local"
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
    assert t("meta_external_reload") in buttons
    assert t("meta_external_keep_local") in buttons
    assert not buttons[t("meta_external_keep_local")].isEnabled()
    dialog.reject()


def test_missing_review_offers_remove_and_locate_actions():
    app(); set_language("en")
    dialog = ExternalChangeReviewDialog(conflict(
        safe=False, state=ExternalChangeState.MISSING))
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert t("meta_external_remove_missing") in labels
    assert t("meta_external_locate_moved") in labels
    dialog.reject()


def test_hebrew_rtl_strings_have_exact_monitoring_key_parity():
    app()
    keys = (
        "meta_monitoring_active", "meta_monitoring_paused",
        "meta_monitoring_degraded", "meta_external_review_title",
        "meta_external_state_replaced", "meta_external_apply_blocked",
    )
    set_language("en"); english = {key: t(key, n=1) for key in keys}
    set_language("he"); hebrew = {key: t(key, n=1) for key in keys}
    assert all(english[key] != key and hebrew[key] != key for key in keys)
    assert all(english[key] != hebrew[key] for key in keys)
    set_language("en")
