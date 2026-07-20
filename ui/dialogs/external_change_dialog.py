"""Field-level review and explicit resolution for an external file change."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from core.file_refresh_service import ConflictResolutionAction
from ui import a11y
from ui.direction import isolate_ltr, isolate_value_for_field
from ui.i18n import t

#: Columns holding a raw field value.  What they contain depends on the field:
#: a filename or codec must read left-to-right inside Hebrew, but a Hebrew
#: title must not — so each value decides for itself rather than the column.
_VALUE_COLUMNS = frozenset({1, 2, 3})


class ExternalChangeReviewDialog(QDialog):
    def __init__(self, conflict, parent=None) -> None:
        super().__init__(parent)
        self.conflict = conflict
        self.selected_action = ConflictResolutionAction.CANCEL
        self.setWindowTitle(t("meta_external_review_title"))
        self.setMinimumSize(820, 420)
        layout = QVBoxLayout(self)
        # Name the file, never its absolute path, and isolate it so the
        # surrounding Hebrew sentence keeps its own direction — forcing the
        # whole label LTR would reverse the sentence around the filename.
        description = QLabel(t(
            "meta_external_review_body",
            path=isolate_ltr(conflict.observed_path.name)))
        description.setWordWrap(True)
        description.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(description)

        classification = QLabel(t(
            "meta_external_safe_rebase" if conflict.safe_rebase
            else "meta_external_unsafe_rebase"))
        classification.setWordWrap(True)
        layout.addWidget(classification)

        table = QTableWidget(0, 5, self)
        a11y.describe(table, t("meta_external_differences_table"),
                      description=t("meta_external_differences_table_desc"))
        table.setHorizontalHeaderLabels([
            t("meta_external_field"), t("meta_external_previous"),
            t("meta_external_disk"), t("meta_external_local"),
            t("meta_external_overlap"),
        ])
        for difference in conflict.differences:
            row = table.rowCount(); table.insertRow(row)
            values = (
                self._field_label(difference.field),
                self._format_value(difference.baseline_value),
                self._format_value(difference.disk_value),
                self._format_value(difference.local_value),
                t("yes") if difference.overlap else t("no"),
            )
            for column, value in enumerate(values):
                is_value = column in _VALUE_COLUMNS
                cell = QTableWidgetItem(
                    isolate_value_for_field(difference.field, value) if is_value else value)
                if is_value:
                    cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                # Overlap is the whole point of this table, so it must not be a
                # colour: it is stated as a word in the row itself.
                cell.setToolTip(cell.text())
                table.setItem(row, column, cell)
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)

        buttons = QHBoxLayout(); buttons.addStretch()
        options = [
            (ConflictResolutionAction.RELOAD, "meta_external_reload"),
            (ConflictResolutionAction.KEEP_LOCAL, "meta_external_keep_local"),
        ]
        if conflict.state.value == "missing":
            options.append((ConflictResolutionAction.REMOVE_MISSING,
                            "meta_external_remove_missing"))
            options.append((ConflictResolutionAction.LOCATE_MOVED,
                            "meta_external_locate_moved"))
        for action, key in options:
            button = QPushButton(t(key), self)
            safe = (action is not ConflictResolutionAction.KEEP_LOCAL
                    or conflict.safe_rebase)
            a11y.describe(
                button, t(key),
                # A disabled Keep Local otherwise says only "unavailable"; the
                # user needs to know an overlapping field is the reason.
                description=("" if safe else t("meta_external_unsafe_rebase")))
            button.setEnabled(safe)
            button.clicked.connect(lambda _=False, value=action: self._choose(value))
            buttons.addWidget(button)
        cancel = QPushButton(t("cancel"), self)
        a11y.describe(cancel, t("cancel"))
        # Cancel is the safe default: Enter must never pick a resolution the
        # user has not read, and Escape must always leave without mutating.
        cancel.setDefault(True)
        cancel.setAutoDefault(True)
        cancel.clicked.connect(self.reject); buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _choose(self, action: ConflictResolutionAction) -> None:
        self.selected_action = action
        self.accept()

    @staticmethod
    def _format_value(value) -> str:
        if value is None or value == "":
            return t("meta_inspector_empty_value")
        if isinstance(value, (tuple, list)):
            return "; ".join(str(part) for part in value)
        return str(value)

    @staticmethod
    def _field_label(field: str) -> str:
        keys = {
            "filename": "mt_col_filename", "title": "mt_col_title",
            "artist": "mt_col_artist", "album": "mt_col_album",
            "album_artist": "meta_field_album_artist", "track_num": "mt_col_track",
            "genre": "mt_col_genre", "comment": "mt_col_comment",
            "lyrics": "meta_report_field_lyrics", "artwork": "meta_report_field_artwork",
        }
        key = keys.get(field, f"meta_report_field_{field}")
        value = t(key)
        return field if value == key else value
