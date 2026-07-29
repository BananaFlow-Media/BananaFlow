"""
ui/panels/metadata_editor/table_layout.py  –  Tag Editor
==============================================================================
Table header geometry: column order, widths, the trailing filler
column, sorting, visibility persistence and the header menu.

Extracted from panel.py unchanged; MetadataEditorPanel mixes this in,
so every attribute reference resolves exactly as before.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QFileInfo,
    QItemSelection,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QComboBox,
    QFileDialog,
    QFileIconProvider,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSplitterHandle,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from ui.i18n import t
from ui.models.metadata_table_model import (
    COL_CHECK, COL_FILENAME, COL_TITLE_NEW,
    COL_ARTIST_NEW, COL_ALBUM_NEW,
    COL_TRACK_NEW,
    COL_FILENAME_NEW, COL_GENRE_CUR, COL_GENRE_NEW,
    COL_COMMENT_CUR, COL_COMMENT_NEW,
    COLUMN_COUNT, MetadataTableModel, _HEADER_KEYS,
)
from .dialogs import AutoArrangeSettingsDialog, CleanSettingsDialog, MoreColumnsDialog


class TableLayoutMixin:
    """Table header geometry: column order, widths, the trailing filler"""

    def _on_section_moved(self, logical: int, old_visual: int, new_visual: int) -> None:
        """Keep Name pinned first and the fixed empty gutter pinned last."""
        hdr = self._table.horizontalHeader()

        target_gutter_visual = COLUMN_COUNT - 1
        if hdr.visualIndex(COL_CHECK) != target_gutter_visual:
            hdr.blockSignals(True)
            try:
                hdr.moveSection(hdr.visualIndex(COL_CHECK), target_gutter_visual)
            finally:
                hdr.blockSignals(False)

        if logical == COL_FILENAME and new_visual != 0:
            hdr.blockSignals(True)
            try:
                hdr.moveSection(hdr.visualIndex(COL_FILENAME), 0)
            finally:
                hdr.blockSignals(False)
        elif new_visual == 0 and logical != COL_FILENAME:
            hdr.blockSignals(True)
            try:
                hdr.moveSection(hdr.visualIndex(COL_FILENAME), 0)
            finally:
                hdr.blockSignals(False)

        self._save_column_order()
        self._fill_leftover_space()

    def _fill_leftover_space(self) -> None:
        """Grow the visually-last real content column to absorb any leftover
        viewport width, so there's never blank space with no column in it.

        COL_CHECK is pinned to the trailing edge as a deliberate thin gutter
        (mirrors the empty-area margin on the leading edge). Deliberately a
        *one-time* width top-up via `resizeSection` rather than Qt's
        `Stretch` resize mode — Stretch permanently locks that column so the
        user can no longer drag it narrower, which just traded "dead space"
        for "a column you can't resize". This recomputes (and can grow OR
        shrink the filler column) whenever the viewport, column set, or
        column order changes, but a manual drag on any column in between
        those events is left alone.
        """
        hdr = self._table.horizontalHeader()
        best_col = self._visual_filler_column()
        if best_col is None:
            return

        others_total = sum(
            hdr.sectionSize(c) for c in range(COLUMN_COUNT)
            if c != best_col and not self._table.isColumnHidden(c)
        )
        target_width = max(40, self._table.viewport().width() - others_total)
        if target_width == hdr.sectionSize(best_col):
            return

        prev_ignore = self._ignore_header_resize
        self._ignore_header_resize = True
        try:
            hdr.resizeSection(best_col, target_width)
        finally:
            self._ignore_header_resize = prev_ignore

    def _visual_filler_column(self) -> int | None:
        """The visually-last visible content column — the one that absorbs
        leftover viewport width in _fill_leftover_space()."""
        hdr = self._table.horizontalHeader()
        best_col, best_visual = None, -1
        for col in range(COLUMN_COUNT):
            if col == COL_CHECK or self._table.isColumnHidden(col):
                continue
            visual = hdr.visualIndex(col)
            if visual > best_visual:
                best_visual, best_col = visual, col
        return best_col

    def _refresh_table_geometry_after_column_resize(self) -> None:
        if not hasattr(self, "_table"):
            return
        hdr = self._table.horizontalHeader()
        sb = self._table.horizontalScrollBar()
        self._table.updateGeometries()
        hdr.viewport().repaint()
        self._table.viewport().repaint()
        sb.update()

    def _refresh_after_manual_column_resize(self) -> None:
        self._refresh_table_geometry_after_column_resize()

    def _flush_geometry_save(self) -> None:
        """Debounce target — cfg fields were updated in-memory by the geometry
        handlers; this writes them to disk once the burst has settled."""
        if self._cfg:
            self._cfg.save()

    def _save_column_order(self) -> None:
        if self._cfg:
            hdr = self._table.horizontalHeader()
            order = []
            for visual_idx in range(COLUMN_COUNT):
                logical_idx = hdr.logicalIndex(visual_idx)
                order.append(logical_idx)
            self._cfg.tag_editor_column_order = order
            self._cfg.save()

    def _on_section_resized(self, logical: int, old_size: int, new_size: int) -> None:
        if getattr(self, "_ignore_header_resize", False):
            return
        if logical == COL_CHECK:
            return

        factor = self._zoom_level / 100.0
        if factor <= 0:
            factor = 1.0

        base_width = int(new_size / factor)
        if self._cfg:
            widths = dict(self._cfg.tag_editor_column_widths)
            widths[str(logical)] = base_width
            self._cfg.tag_editor_column_widths = widths
            self._geometry_save_timer.start()

        self._refresh_after_manual_column_resize()

    def _on_sort_indicator_changed(self, column: int, order: Qt.SortOrder) -> None:
        hdr = self._table.horizontalHeader()
        if column == COL_CHECK:
            hdr.setSortIndicatorShown(False)
            if self._cfg:
                self._cfg.tag_editor_sort_column = -1
                self._cfg.save()
            return

        hdr.setSortIndicatorShown(column != -1)
        if self._cfg:
            self._cfg.tag_editor_sort_column = column
            order_val = order.value if hasattr(order, 'value') else int(order)
            self._cfg.tag_editor_sort_order = int(order_val)
            self._cfg.save()

    def _save_column_visibility(self) -> None:
        if self._cfg:
            hidden_cols = []
            for col in range(COLUMN_COUNT):
                if self._table.isColumnHidden(col):
                    hidden_cols.append(col)
            self._cfg.tag_editor_column_visibility = hidden_cols
            self._cfg.save()

    def _set_column_hidden(self, col: int, hide: bool) -> None:
        self._table.setColumnHidden(col, hide)
        self._save_column_visibility()
        self._fill_leftover_space()

    def _on_header_context_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        # Short "common" column list — mirrors the Windows Explorer header
        # right-click menu (5-7 items, not the full attribute sheet).
        COMMON_COLS = [
            COL_FILENAME,      # Name  — always visible
            COL_TITLE_NEW,     # Title (new)
            COL_ARTIST_NEW,    # Artist (new)
            COL_ALBUM_NEW,     # Album (new)
            COL_TRACK_NEW,     # Track (new)
            COL_FILENAME_NEW,  # Filename (new)
        ]
        ALWAYS_VISIBLE = {COL_FILENAME}

        menu = QMenu(self)
        for col in COMMON_COLS:
            key = _HEADER_KEYS[col] if col < len(_HEADER_KEYS) else ""
            lbl = t(key) if key else ""
            if not lbl:
                continue
            action = menu.addAction(lbl)
            action.setCheckable(True)
            action.setChecked(not self._table.isColumnHidden(col))
            if col in ALWAYS_VISIBLE:
                action.setEnabled(False)
            else:
                action.triggered.connect(
                    lambda checked, c=col: self._set_column_hidden(c, not checked)
                )

        menu.addSeparator()
        menu.addAction(t("mt_size_all_to_fit")).triggered.connect(
            self._size_all_columns_to_fit
        )
        menu.addSeparator()
        menu.addAction(t("mt_more_columns")).triggered.connect(self._on_more_columns)

        menu.exec(self._table.horizontalHeader().mapToGlobal(pos))

    def _size_all_columns_to_fit(self) -> None:
        """Resize every visible column to its content width (Win11 'Best fit')."""
        for col in range(COLUMN_COUNT):
            if not self._table.isColumnHidden(col):
                self._table.resizeColumnToContents(col)
        # Best-fit can leave a gap at the trailing edge — re-settle the filler.
        self._fill_leftover_space()

    def _size_column_to_fit(self, col: int) -> None:
        """Resize one visible content column to fit its header/cells."""
        if col == COL_CHECK or col < 0 or col >= COLUMN_COUNT:
            return
        if self._table.isColumnHidden(col):
            return

        self._table.resizeColumnToContents(col)
        self._fill_leftover_space()

    def _on_more_columns(self) -> None:
        dlg = MoreColumnsDialog(self._table, self)
        if dlg.exec() == QDialog.Accepted:
            self._save_column_visibility()
            self._fill_leftover_space()
