"""Review-first Online Metadata dialog for the existing Inspector."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from core.metadata_lookup import (
    AcceptedFieldSelection, FieldDifference, LookupState,
)
from ui.i18n import t


class OnlineMetadataDialog(QDialog):
    """Renders provider values but never performs HTTP or proposal mutation."""
    def __init__(self, workspace, item_ids, *, search, cancel, preview, artwork, accept, parent=None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self.item_ids = tuple(sorted(item_ids))
        self._search = search; self._cancel = cancel; self._preview = preview
        self._artwork = artwork; self._accept = accept
        self._result = None; self._preview_value = None; self._artwork_entry = None
        self.setWindowTitle(t("meta_online_title")); self.setMinimumSize(880, 650)
        self.setAccessibleName(t("meta_online_title"))
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self.scope_label = QLabel(t("meta_online_scope", n=len(self.item_ids)))
        layout.addWidget(self.scope_label)
        search_row = QHBoxLayout()
        self.mode = QComboBox(); self.mode.addItem(t("meta_online_single_track"), "track"); self.mode.addItem(t("meta_online_selected_album"), "album")
        if len(self.item_ids) > 1: self.mode.setCurrentIndex(1)
        self.title_edit = QLineEdit(); self.title_edit.setPlaceholderText(t("meta_online_search_title"))
        self.artist_edit = QLineEdit(); self.artist_edit.setPlaceholderText(t("meta_online_search_artist"))
        self.album_edit = QLineEdit(); self.album_edit.setPlaceholderText(t("meta_online_search_album"))
        for widget, name in ((self.mode, "meta_online_scope_label"), (self.title_edit, "meta_online_search_title"),
                             (self.artist_edit, "meta_online_search_artist"), (self.album_edit, "meta_online_search_album")):
            widget.setAccessibleName(t(name))
        for widget in (self.title_edit, self.artist_edit, self.album_edit):
            widget.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._populate_terms()
        self.search_button = QPushButton(t("meta_online_search_musicbrainz")); self.search_button.setAccessibleName(t("meta_online_search_musicbrainz"))
        self.cancel_button = QPushButton(t("meta_online_cancel_lookup")); self.cancel_button.setAccessibleName(t("meta_online_cancel_lookup")); self.cancel_button.setEnabled(False)
        self.search_button.clicked.connect(self._start_search); self.cancel_button.clicked.connect(self._cancel_lookup)
        for widget in (self.mode, self.title_edit, self.artist_edit, self.album_edit, self.search_button, self.cancel_button): search_row.addWidget(widget)
        layout.addLayout(search_row)
        self.progress = QProgressBar(); self.progress.setRange(0, 1); self.progress.setValue(0); self.progress.setVisible(False)
        self.state = QLabel(t("meta_online_explicit_search_hint")); self.state.setWordWrap(True)
        layout.addWidget(self.progress); layout.addWidget(self.state)
        middle = QHBoxLayout()
        self.candidates = QListWidget(); self.candidates.setAccessibleName(t("meta_online_candidates")); self.candidates.currentRowChanged.connect(self._candidate_changed)
        middle.addWidget(self.candidates, 1)
        self.comparison = QTableWidget(0, 5); self.comparison.setAccessibleName(t("meta_online_comparison"))
        self.comparison.setHorizontalHeaderLabels([t("meta_online_use_online"), t("meta_online_field"), t("meta_online_local_value"), t("meta_online_online_value"), t("meta_online_status")])
        self.comparison.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.comparison.horizontalHeader().setStretchLastSection(True)
        self.comparison.itemChanged.connect(self._update_add_enabled)
        middle.addWidget(self.comparison, 2); layout.addLayout(middle, 1)
        self.attribution = QLabel(""); self.attribution.setWordWrap(True); self.attribution.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.attribution.setAccessibleName(t("meta_online_attribution")); layout.addWidget(self.attribution)
        self.artwork_preview = QLabel(t("meta_online_artwork_not_selected")); self.artwork_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.artwork_preview.setMinimumHeight(96); self.artwork_preview.setAccessibleName(t("meta_online_artwork_preview")); layout.addWidget(self.artwork_preview)
        actions = QHBoxLayout()
        self.recommended = QPushButton(t("meta_online_select_recommended")); self.recommended.setAccessibleName(t("meta_online_select_recommended")); self.recommended.clicked.connect(self._select_recommended)
        self.clear_selection = QPushButton(t("meta_online_clear_selection")); self.clear_selection.setAccessibleName(t("meta_online_clear_selection")); self.clear_selection.clicked.connect(self._clear_fields)
        self.artwork_button = QPushButton(t("meta_online_artwork_preview")); self.artwork_button.setAccessibleName(t("meta_online_artwork_preview")); self.artwork_button.clicked.connect(self._request_artwork); self.artwork_button.setEnabled(False)
        self.artwork_use = QPushButton(t("meta_online_use_artwork")); self.artwork_use.setCheckable(True); self.artwork_use.setEnabled(False); self.artwork_use.setAccessibleName(t("meta_online_use_artwork"))
        self.artwork_use.toggled.connect(self._update_add_enabled)
        self.add_button = QPushButton(t("meta_online_add_pending")); self.add_button.setAccessibleName(t("meta_online_add_pending")); self.add_button.clicked.connect(self._add_pending); self.add_button.setEnabled(False)
        close = QPushButton(t("cancel_btn")); close.clicked.connect(self.reject)
        for widget in (self.recommended, self.clear_selection, self.artwork_button, self.artwork_use, self.add_button, close): actions.addWidget(widget)
        layout.addLayout(actions)

    def _populate_terms(self) -> None:
        tracks = [self.workspace.track_for_id(identity) for identity in self.item_ids]
        effective = [item.proposed.effective_tags(item.original) for item in tracks if item is not None]
        def common(name):
            values = [getattr(tags, name) for tags in effective]
            return str(values[0]) if values and all(value == values[0] for value in values) else ""
        self.title_edit.setText(common("title") if len(effective) == 1 else "")
        self.artist_edit.setText(common("artist")); self.album_edit.setText(common("album"))

    def _start_search(self) -> None:
        self._preview_value = None; self._artwork_entry = None; self.artwork_use.setChecked(False); self.artwork_use.setEnabled(False)
        self.artwork_button.setEnabled(False); self.artwork_preview.clear(); self.artwork_preview.setText(t("meta_online_artwork_not_selected"))
        self.comparison.setRowCount(0); self.candidates.clear(); self.progress.setVisible(True); self.progress.setRange(0, 0)
        self.search_button.setEnabled(False); self.cancel_button.setEnabled(True); self.add_button.setEnabled(False)
        self.search_button.setText(t("meta_online_search_musicbrainz"))
        self.state.setText(t("meta_online_searching"))
        self._search({"item_ids": self.item_ids, "mode": self.mode.currentData(), "title": self.title_edit.text(),
                      "artist": self.artist_edit.text(), "album": self.album_edit.text()})

    def _cancel_lookup(self) -> None:
        self._cancel(); self.state.setText(t("meta_online_cancelled")); self._finish_progress()

    def on_lookup_result(self, result) -> None:
        self._result = result; self._finish_progress(); self.candidates.clear()
        state_keys = {
            LookupState.NO_RESULTS: "meta_online_no_results", LookupState.CANCELLED: "meta_online_cancelled",
            LookupState.OFFLINE: "meta_online_offline", LookupState.RATE_LIMITED: "meta_online_rate_limited",
            LookupState.ERROR: "meta_online_provider_error", LookupState.PARTIAL: "meta_online_partial_results",
        }
        if result.state is not LookupState.READY:
            key = result.error.message_key if result.error is not None else state_keys.get(result.state, "meta_online_provider_error")
            self.state.setText(t(key)); self.search_button.setText(t("meta_online_retry")); return
        self.state.setText(t("meta_online_candidates_count", n=len(result.candidates)))
        for candidate in result.candidates:
            label = t("meta_online_candidate_label", title=candidate.title or candidate.album,
                      artist=candidate.artist or candidate.album_artist, score=f"{candidate.score:.0f}")
            item = QListWidgetItem(label); item.setData(Qt.ItemDataRole.UserRole, candidate); self.candidates.addItem(item)
        if self.candidates.count(): self.candidates.setCurrentRow(0)

    def _candidate_changed(self, row: int) -> None:
        item = self.candidates.item(row)
        if item is None: return
        # Candidate changes invalidate every visual and final-art selection.
        self._preview_value = None; self._artwork_entry = None
        self.artwork_use.setChecked(False); self.artwork_use.setEnabled(False)
        self.artwork_preview.clear(); self.artwork_preview.setText(t("meta_online_artwork_not_selected"))
        self.comparison.setRowCount(0); self.add_button.setEnabled(False)
        self.artwork_button.setEnabled(False)
        candidate = item.data(Qt.ItemDataRole.UserRole)
        self.attribution.setText(t("meta_online_attribution_value", provider=(candidate.attribution.text if candidate.attribution else ""), url=candidate.source_url))
        evidence = ", ".join(t("meta_online_evidence_component", component=row.component, score=f"{row.similarity * 100:.0f}") for row in candidate.evidence)
        self.state.setText(t("meta_online_confidence_evidence", score=f"{candidate.score:.0f}", evidence=evidence or t("meta_online_evidence_unavailable")))
        self._preview(candidate)

    def on_match_preview(self, preview) -> None:
        self._preview_value = preview; self.comparison.setRowCount(len(preview.comparisons))
        self.artwork_button.setEnabled(bool(preview.candidate.release_id and preview.artwork_supported_item_ids))
        for row, comparison in enumerate(preview.comparisons):
            check = QTableWidgetItem(); check.setCheckState(Qt.CheckState.Unchecked)
            if comparison.difference is not FieldDifference.UNSUPPORTED:
                check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            else:
                check.setFlags(check.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsUserCheckable)
            check.setToolTip(t("meta_online_keep_local"))
            check.setData(Qt.ItemDataRole.UserRole, (comparison.item_id, comparison.field, comparison.recommended))
            values = [check, QTableWidgetItem(t(f"meta_online_field_{comparison.field}")),
                      QTableWidgetItem(self._value(comparison.local_value)), QTableWidgetItem(self._value(comparison.online_value)),
                      QTableWidgetItem(t(f"meta_online_difference_{comparison.difference.value}"))]
            for col, value in enumerate(values):
                if col in {2, 3}:
                    value.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.comparison.setItem(row, col, value)
        self._update_add_enabled()
        mappings = preview.album_mappings
        if mappings and any(mapping.state.value != "matched" for mapping in mappings):
            unmatched = sum(mapping.state.value == "unmatched" for mapping in mappings); ambiguous = sum(mapping.state.value == "ambiguous" for mapping in mappings)
            self.state.setText(t("meta_online_album_mapping_state", unmatched=unmatched, ambiguous=ambiguous))

    def _select_recommended(self) -> None:
        for row in range(self.comparison.rowCount()):
            item = self.comparison.item(row, 0); data = item.data(Qt.ItemDataRole.UserRole) if item else None
            if item: item.setCheckState(Qt.CheckState.Checked if data and data[2] else Qt.CheckState.Unchecked)

    def _clear_fields(self) -> None:
        for row in range(self.comparison.rowCount()):
            item = self.comparison.item(row, 0)
            if item: item.setCheckState(Qt.CheckState.Unchecked)
        self.artwork_use.setChecked(False)

    def _request_artwork(self) -> None:
        if self._preview_value is not None:
            self.state.setText(t("meta_online_artwork_loading")); self._artwork(self._preview_value.candidate)

    def on_artwork_ready(self, candidates, selected, entry) -> None:
        self._artwork_entry = entry; self.artwork_use.setEnabled(entry is not None)
        if entry is not None:
            pixmap = QPixmap(); pixmap.loadFromData(entry.data)
            self.artwork_preview.setPixmap(pixmap.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.artwork_preview.setText(t("meta_online_artwork_unavailable"))
        self.state.setText(t("meta_online_artwork_ready") if entry is not None else t("meta_online_artwork_unavailable"))
        self._update_add_enabled()

    def on_artwork_error(self, message_key: str) -> None:
        self._artwork_entry = None; self.artwork_use.setChecked(False); self.artwork_use.setEnabled(False)
        self.artwork_preview.setText(t(message_key)); self.state.setText(t(message_key)); self._update_add_enabled()

    def on_acceptance_error(self, message_key: str) -> None:
        self.state.setText(t(message_key)); self._update_add_enabled()

    def on_release_detail_result(self, result) -> None:
        if result is None:
            self.progress.setVisible(True); self.progress.setRange(0, 0)
            self.state.setText(t("meta_online_release_detail_loading")); return
        self._finish_progress()
        if result.state is not LookupState.READY:
            key = result.error.message_key if result.error else "meta_online_provider_error"
            self.state.setText(t(key)); self.add_button.setEnabled(False)

    def _update_add_enabled(self, *_args) -> None:
        any_field = any(
            (item := self.comparison.item(row, 0)) is not None
            and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)
            and item.checkState() == Qt.CheckState.Checked
            for row in range(self.comparison.rowCount())
        )
        self.add_button.setEnabled(bool(self._preview_value and (any_field or (
            self.artwork_use.isEnabled() and self.artwork_use.isChecked()))))

    def _add_pending(self) -> None:
        if self._preview_value is None: return
        selected = set()
        for row in range(self.comparison.rowCount()):
            item = self.comparison.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                data = item.data(Qt.ItemDataRole.UserRole); selected.add((int(data[0]), str(data[1])))
        artwork_ids = (frozenset(self._preview_value.artwork_supported_item_ids)
                       if self.artwork_use.isChecked() and self._artwork_entry is not None else frozenset())
        if not selected and not artwork_ids:
            self._update_add_enabled(); return
        self.add_button.setEnabled(False)
        if artwork_ids: self.state.setText(t("meta_online_artwork_final_loading"))
        self._accept((self._preview_value, AcceptedFieldSelection(frozenset(selected), artwork_ids)))

    def on_acceptance_complete(self, accepted: bool) -> None:
        if accepted: self.accept()
        else: self.state.setText(t("meta_online_stale_result"))

    def _finish_progress(self) -> None:
        self.progress.setVisible(False); self.progress.setRange(0, 1); self.search_button.setEnabled(True); self.cancel_button.setEnabled(False)

    def reject(self) -> None:
        self._cancel(); super().reject()

    @staticmethod
    def _value(value) -> str:
        if value is None: return ""
        if isinstance(value, (tuple, list)): return "; ".join(str(part) for part in value)
        return str(value)
