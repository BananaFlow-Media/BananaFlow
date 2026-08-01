"""
ui/panels/metadata_editor/explorer_view.py  –  Win11-Explorer-style table
===========================================================================
The Details-View mimicry widgets for the Tag Editor:
  ExplorerFileListDelegate — plain cells (row background painted by view)
  FilenameDelegate         — LTR filename cells with their media icon
  SelectionCheckDelegate   — fixed standalone row-selection checkboxes
  MetadataHeaderView       — header with 'Select All' check + resize grips
  ExplorerTableStyle       — suppresses Qt's flat selection rectangle
  ExplorerFileListView     — row-capsule painting, rubber band, empty-area
                             deselect, Delete→Recycle-Bin
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLineEdit,
    QProxyStyle,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
)

from ui.models.metadata_table_model import (
    COL_CHECK,
    COL_END_GUTTER,
    COL_FILENAME,
    COL_FILENAME_NEW,
    COL_GUTTER,
)
from .shared import CB_SIZE, paint_check_mark, tag_editor_colors


def _accent_color() -> QColor:
    accent = QColor(tag_editor_colors().accent)
    if not accent.isValid():
        return QColor(0, 120, 212)
    return accent


def _with_alpha(color: QColor, alpha: int) -> QColor:
    result = QColor(color)
    result.setAlpha(alpha)
    return result


class ExplorerFileListDelegate(QStyledItemDelegate):
    """
    Draw only the item contents.  Row backgrounds are painted by
    ExplorerFileListView so selected rows stay one continuous strip.

    Text cells use theme-aware foreground colors and leave all row background
    painting to the view so the selected row remains one continuous strip.
    """
    _PADDING_X = 12        # Win11 horizontal cell inset
    _ICON_SIZE = 16
    _ICON_TEXT_GAP = 8

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if index.column() in (COL_CHECK, COL_GUTTER, COL_END_GUTTER):
            return  # Dedicated fixed-column delegates own these cells.

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        cell_rect = QRect(option.rect)

        painter.save()

        # Row background (hover/selection capsule) is painted once by drawRow —
        # do NOT repaint it here or the border clips to this cell's rect only.

        if not (opt.state & QStyle.State_Selected) and opt.backgroundBrush.style() != Qt.NoBrush:
            painter.fillRect(cell_rect.adjusted(0, 1, 0, -1), opt.backgroundBrush)

        opt.state &= ~QStyle.State_Selected
        opt.state &= ~QStyle.State_HasFocus
        opt.backgroundBrush = QBrush(Qt.NoBrush)
        text_color = QColor(tag_editor_colors().text_primary)
        opt.palette.setColor(QPalette.Text, text_color)
        opt.palette.setColor(QPalette.HighlightedText, text_color)

        text_rect = cell_rect.adjusted(self._PADDING_X, 1, -self._PADDING_X, -1)
        opt.rect = text_rect
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.restore()


class FilenameDelegate(QStyledItemDelegate):
    """Delegate for the filename columns (COL_FILENAME and COL_FILENAME_NEW).

    Enforces LTR direction, left alignment and middle elision so file paths
    read correctly even when the app runs RTL. Also draws:
      • a 16-px file-type icon on the leading edge
      • an elegant check-mark indicator when show_checkbox=True,
        visible only on hover or selection so it stays hidden at rest.
    """

    _CB_SIZE   = CB_SIZE   # rounded checkbox size, px (shared painter geometry)
    _CB_INSET  = 4    # leading padding before the mark
    _CB_GAP    = 4    # gap between mark right-edge and icon/text
    _ICON_SIZE = 16
    _ICON_GAP  = 6
    _PAD_X     = 12   # trailing cell padding
    _RTL_FILENAME_GUTTER = 44

    def __init__(
        self,
        parent=None,
        *,
        icon_provider: Callable[[object], QIcon] | None = None,
        show_checkbox: bool = False,
    ) -> None:
        super().__init__(parent)
        self._icon_provider = icon_provider
        self._show_checkbox = show_checkbox

    # ── geometry helpers ──────────────────────────────────────────────────────

    @property
    def _checkbox_width(self) -> int:
        return (self._CB_INSET + self._CB_SIZE + self._CB_GAP) if self._show_checkbox else 0

    def checkbox_hit_rect(self, cell_rect: QRect) -> QRect:
        """Return the checkbox hit area within cell_rect.

        In RTL mode the checkbox sits on the RIGHT (trailing) edge so it
        appears at the screen-right of the Name column, matching Win11."""
        if not self._show_checkbox:
            return QRect()
        if QApplication.layoutDirection() == Qt.RightToLeft:
            return QRect(
                cell_rect.right() - self._RTL_FILENAME_GUTTER - self._checkbox_width,
                cell_rect.top(),
                self._checkbox_width,
                cell_rect.height(),
            )
        return QRect(cell_rect.left() + 8, cell_rect.top(),
                     self._checkbox_width, cell_rect.height())

    # ── QStyledItemDelegate interface ─────────────────────────────────────────

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.direction = Qt.LayoutDirection.LeftToRight
        if QApplication.layoutDirection() == Qt.RightToLeft:
            option.displayAlignment = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter
        else:
            option.displayAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter
        option.textElideMode   = Qt.TextElideMode.ElideMiddle

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        cell_rect = QRect(option.rect)
        painter.save()

        # Suppress Qt's default selection fill — the view's drawRow handles it.
        opt.state &= ~QStyle.State_Selected
        opt.state &= ~QStyle.State_HasFocus
        opt.backgroundBrush = QBrush(Qt.NoBrush)

        text_color = QColor(tag_editor_colors().text_primary)
        opt.palette.setColor(QPalette.Text, text_color)
        opt.palette.setColor(QPalette.HighlightedText, text_color)

        # Model-level background (error / changed-highlight brush).
        if option.backgroundBrush.style() != Qt.NoBrush:
            painter.fillRect(cell_rect.adjusted(0, 1, 0, -1), option.backgroundBrush)

        table = self.parent()
        row   = index.row()
        model = index.model()
        track = model.track_at_row(row) if hasattr(model, "track_at_row") else None

        # ── Should we show the checkbox? ──────────────────────────────────────
        show_cb    = False
        is_checked = False
        if self._show_checkbox and track is not None and table is not None:
            sel_model   = table.selectionModel()
            is_selected = bool(sel_model and sel_model.isRowSelected(row, QModelIndex()))
            is_checked  = is_selected
            is_hover    = getattr(table, "_hovered_row", -1) == row
            show_cb     = is_selected or is_hover

        # ── Resolve file icon ─────────────────────────────────────────────────
        icon = None
        if self._icon_provider is not None and track is not None:
            icon = self._icon_provider(track)

        # ── Layout ─────────────────────────────────────────────────────────────
        # Win11 RTL order (right→left on screen):
        #   [CB_INSET][mark][CB_GAP] | [icon][icon_gap] | [text extends left]
        # Win11 LTR order (left→right on screen):
        #   [CB_INSET][mark][CB_GAP] | [icon][icon_gap] | [text extends right]
        # ──────────────────────────────────────────────────────────────────────
        is_rtl = QApplication.layoutDirection() == Qt.RightToLeft
        iy = cell_rect.top() + (cell_rect.height() - self._ICON_SIZE) // 2
        margin_x = self._RTL_FILENAME_GUTTER if (is_rtl and self._show_checkbox) else 8

        if is_rtl:
            # ── RTL path ──────────────────────────────────────────────────────
            # Checkbox on the RIGHT (leading edge in RTL)
            if self._show_checkbox:
                cb_zone = QRect(
                    cell_rect.right() - margin_x - self._checkbox_width,
                    cell_rect.top(), self._checkbox_width, cell_rect.height(),
                )
                if show_cb:
                    self._draw_checkbox(painter, cb_zone, is_checked)

            # Icon immediately left of the checkbox (or right edge if no checkbox)
            icon_right = (
                cell_rect.right() - margin_x - self._checkbox_width - self._PAD_X
                if self._show_checkbox
                else cell_rect.right() - margin_x - self._PAD_X
            )
            icon_left = icon_right - self._ICON_SIZE
            if icon is not None and not icon.isNull():
                icon.paint(painter, QRect(icon_left, iy, self._ICON_SIZE, self._ICON_SIZE))
                text_right = icon_left - self._ICON_GAP
            else:
                text_right = icon_right

            text_rect = QRect(
                cell_rect.left() + self._PAD_X,
                cell_rect.top() + 1,
                max(0, text_right - cell_rect.left() - self._PAD_X),
                cell_rect.height() - 2,
            )
        else:
            # ── LTR path ──────────────────────────────────────────────────────
            x = cell_rect.left() + margin_x

            # Checkbox on the LEFT (leading edge in LTR)
            if self._show_checkbox:
                cb_zone = QRect(x, cell_rect.top(), self._checkbox_width, cell_rect.height())
                if show_cb:
                    self._draw_checkbox(painter, cb_zone, is_checked)
                x += self._checkbox_width

            # Icon immediately after checkbox (or left pad if no checkbox)
            if icon is not None and not icon.isNull():
                ix = x + self._PAD_X
                icon.paint(painter, QRect(ix, iy, self._ICON_SIZE, self._ICON_SIZE))
                x = ix + self._ICON_SIZE + self._ICON_GAP
            else:
                x += self._PAD_X

            text_rect = QRect(
                x, cell_rect.top() + 1,
                max(0, cell_rect.right() - self._PAD_X - x),
                cell_rect.height() - 2,
            )

        opt.rect = text_rect
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        painter.restore()

    # ── Inline rename: exclude extension from initial selection ───────────────

    def setEditorData(self, editor, index) -> None:
        super().setEditorData(editor, index)
        if isinstance(editor, QLineEdit):
            editor.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            editor.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            text = editor.text()
            # Select everything except the file extension so renaming is natural.
            dot = text.rfind(".")
            if dot > 0:
                editor.setSelection(0, dot)
            else:
                editor.selectAll()

    # ── Private ───────────────────────────────────────────────────────────────

    def _draw_checkbox(self, painter: QPainter, zone: QRect, is_checked: bool) -> None:
        """Draw the shared Win11 check mark centered in zone (RTL-aware inset)."""
        # In RTL the inset is on the right end of the zone; in LTR on the left.
        if QApplication.layoutDirection() == Qt.RightToLeft:
            cx = zone.right() - self._CB_INSET - self._CB_SIZE // 2
        else:
            cx = zone.left() + self._CB_INSET + self._CB_SIZE // 2
        cy = zone.top() + zone.height() // 2
        paint_check_mark(painter, cx, cy, is_checked)


class SelectionCheckDelegate(QStyledItemDelegate):
    """Paint a persistent checkbox that directly mirrors row selection."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        table = self.parent()
        selection_model = table.selectionModel() if table is not None else None
        checked = bool(
            selection_model
            and selection_model.isRowSelected(index.row(), QModelIndex())
        )
        painter.save()
        painter.setClipping(False)
        paint_check_mark(
            painter, option.rect.center().x(), option.rect.center().y(), checked
        )
        painter.restore()


class MetadataHeaderView(QHeaderView):
    """Draw Select All in the fixed standalone checkbox column."""
    
    toggled = Signal(bool)
    sectionAutoSizeRequested = Signal(int)
    _RESIZE_HANDLE_HIT_PAD = 5

    def __init__(self, table):
        super().__init__(Qt.Orientation.Horizontal, table)
        self._table = table
        self._is_checked = False
        self.setMouseTracking(True)
        if QApplication.layoutDirection() == Qt.RightToLeft:
            self.setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter)
        else:
            self.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter)

    def _section_resize_handle_at(self, pos: QPoint) -> int:
        if pos.y() < 0 or pos.y() > self.height():
            return -1

        for logical in range(self.count()):
            if self.isSectionHidden(logical):
                continue
            if self.sectionResizeMode(logical) == QHeaderView.Fixed:
                continue

            x = self.sectionViewportPosition(logical)
            width = self.sectionSize(logical)
            if width <= 0:
                continue

            edge = x if self.isRightToLeft() else x + width - 1
            hit_rect = QRect(
                edge - self._RESIZE_HANDLE_HIT_PAD,
                0,
                self._RESIZE_HANDLE_HIT_PAD * 2 + 1,
                self.height(),
            )
            if hit_rect.contains(pos):
                return logical

        return -1

    def setChecked(self, checked: bool):
        if self._is_checked != checked:
            self._is_checked = checked
            self.viewport().update()

    def _get_cb_rect(self, logicalIndex, rect):
        if logicalIndex != COL_CHECK:
            return QRect()
        return QRect(rect)

    def _draw_resize_grip(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:
        """Draw the subtle Windows-like separator that marks resize handles."""
        if logicalIndex in (COL_CHECK, COL_GUTTER, COL_END_GUTTER) or self.isSectionHidden(logicalIndex) or rect.width() <= 0:
            return

        is_rtl = self.isRightToLeft()
        colors = tag_editor_colors()
        line = QColor(colors.border)
        line.setAlpha(185)
        
        x = rect.left() if is_rtl else rect.right()
        top = rect.top() + 7
        bottom = rect.bottom() - 7
        if bottom <= top:
            return

        painter.save()
        painter.setClipping(False)
        painter.setPen(QPen(line, 1))
        painter.drawLine(x, top, x, bottom)
        painter.restore()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)

        painter = QPainter(self.viewport())
        try:
            for logical in range(self.count()):
                if self.isSectionHidden(logical):
                    continue
                x = self.sectionViewportPosition(logical)
                rect = QRect(x, 0, self.sectionSize(logical), self.height())
                if rect.intersects(self.viewport().rect()):
                    self._draw_resize_grip(painter, rect, logical)
        finally:
            painter.end()

    def paintSection(self, painter, rect, logicalIndex):
        if logicalIndex in (COL_CHECK, COL_GUTTER, COL_END_GUTTER):
            colors = tag_editor_colors()
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors.bg))
            painter.drawRect(rect)
            line = QColor(colors.border)
            line.setAlpha(185)
            painter.setPen(QPen(line, 1))
            if logicalIndex in (COL_GUTTER, COL_END_GUTTER):
                # Each empty strip draws its divider toward the table content.
                if logicalIndex == COL_GUTTER:
                    x = rect.left() if self.isRightToLeft() else rect.right()
                else:
                    x = rect.right() if self.isRightToLeft() else rect.left()
                painter.drawLine(x, rect.top() + 7, x, rect.bottom() - 7)
            else:
                paint_check_mark(
                    painter, rect.center().x(), rect.center().y(), self._is_checked
                )
            painter.restore()
            return

        super().paintSection(painter, rect, logicalIndex)

    def mousePressEvent(self, e):
        logicalIndex = self.logicalIndexAt(e.position().toPoint().x())
        if logicalIndex >= 0:
            x = self.sectionViewportPosition(logicalIndex)
            w = self.sectionSize(logicalIndex)
            rect = QRect(x, 0, w, self.height())
            
            cb_rect = self._get_cb_rect(logicalIndex, rect)
            hit_rect = cb_rect.adjusted(-4, -4, 4, 4)
            
            if hit_rect.contains(e.position().toPoint()):
                self.setChecked(not self._is_checked)
                self.toggled.emit(self._is_checked)
                return

            if logicalIndex in (COL_CHECK, COL_GUTTER, COL_END_GUTTER):
                e.accept()
                return
                
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        logicalIndex = self.logicalIndexAt(e.position().toPoint().x())
        if logicalIndex in (COL_CHECK, COL_GUTTER, COL_END_GUTTER):
            e.accept()
            return

        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:
        logical = self._section_resize_handle_at(e.position().toPoint())
        if logical >= 0:
            self.sectionAutoSizeRequested.emit(logical)
            e.accept()
            return

        super().mouseDoubleClickEvent(e)

    def mouseMoveEvent(self, e) -> None:
        super().mouseMoveEvent(e)
        if self._section_resize_handle_at(e.position().toPoint()) >= 0:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.unsetCursor()

    def leaveEvent(self, e) -> None:
        self.unsetCursor()
        super().leaveEvent(e)


class ExplorerTableStyle(QProxyStyle):
    """QProxyStyle applied to the table view.

    Qt's default QAbstractItemView.drawRow() calls
    QStyle.PE_PanelItemViewRow which draws a FLAT selection rectangle on top
    of our capsule.  This proxy intercepts that primitive and suppresses it so
    ExplorerFileListView.drawRow() is the sole painter of row backgrounds.

    It also clears State_Selected from CE_ItemViewItem calls so that
    qfluentwidgets (and any other style engine) cannot add per-cell selection
    borders or blue left-edge indicators on top of our capsule fill.
    """

    def drawPrimitive(self, element, option, painter, widget=None):
        if element in (QStyle.PE_PanelItemViewRow, QStyle.PE_PanelItemViewItem):
            return   # drawRow capsule is sole row-background painter
        super().drawPrimitive(element, option, painter, widget)

    def drawControl(self, element, option, painter, widget=None):
        if (element == QStyle.CE_ItemViewItem
                and isinstance(option, QStyleOptionViewItem)
                and (option.state & QStyle.State_Selected)):
            opt = QStyleOptionViewItem(option)
            opt.state &= ~QStyle.State_Selected
            opt.state &= ~QStyle.State_HasFocus
            super().drawControl(element, opt, painter, widget)
            return
        super().drawControl(element, option, painter, widget)


class ExplorerDetailsView(QTableView):
    """QTableView with Explorer-like empty-area deselect and rubber-band rows."""

    deleteRequested = Signal(list)       # list[Path]
    openRequested = Signal(list)
    renameRequested = Signal(list)
    keyboardContextMenuRequested = Signal(QPoint)
    viewportResized = Signal()
    selectedItemsChanged = Signal(list)  # items resolved through model.track_at_row()

    _CHECK_COLUMN_WIDTH = 24
    # 60% of the original 28 px gutter, rounded to the nearest pixel.
    _SIDE_EMPTY_GUTTER = 17
    # Opposite-edge clear area: about half the leading gutter.
    _END_EMPTY_GUTTER = 9

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rubber_origin = QPoint()
        self._rubber_active = False
        self._rubber_dragging = False
        self._rubber_modifiers = Qt.NoModifier
        self._rubber_base_selection = QItemSelection()
        self._pending_cb_row = -1   # row whose checkbox toggle is deferred to mouse-release
        self._hovered_row = -1
        # Rubber-band geometry tracked as a plain QRect; drawn directly in
        # paintEvent so SourceOver alpha works without WA_TranslucentBackground.
        self._rubber_rect = QRect()
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        # Win11 Details View row geometry — 40 px tall, no grid.
        vh = self.verticalHeader()
        vh.setDefaultSectionSize(42)
        vh.setMinimumSectionSize(42)

        # Win11-style inset: small margins around the content area so the row
        # capsule visually floats inside the panel (left/right/bottom gutter).
        self.setViewportMargins(0, 0, 0, 0)

        # Make the empty area follow the theme by default
        bg = QColor(tag_editor_colors().surface)
        pal = self.viewport().palette()
        pal.setColor(QPalette.Base, bg)
        pal.setColor(QPalette.Window, bg)
        self.viewport().setPalette(pal)
        self.viewport().setAutoFillBackground(True)

        # Listen for theme changes and refresh
        from ui.theme_manager import ThemeManager as _TM
        _tm = _TM.instance()
        if _tm is not None:
            _tm.theme_changed.connect(self._refresh_viewport_palette)

    def _refresh_viewport_palette(self) -> None:
        bg = QColor(tag_editor_colors().surface)
        pal = self.viewport().palette()
        pal.setColor(QPalette.Base, bg)
        pal.setColor(QPalette.Window, bg)
        self.viewport().setPalette(pal)
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # A real viewport resize (window resize, splitter drag) — as opposed
        # to a user dragging a column boundary — is the only time the filler
        # column should be recomputed, so a manual column resize is never
        # immediately fought/undone by this.
        self.viewportResized.emit()

    def paintEvent(self, event) -> None:
        # Fill the entire viewport with the theme background first
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor(tag_editor_colors().surface))
        
        # Draw custom selection/hover row backgrounds before drawing cells on top
        model = self.model()
        if model is not None:
            for row in self._visible_row_range(model):
                row_y = self.rowViewportPosition(row)
                row_h = self.rowHeight(row)
                row_rect = QRect(0, row_y, self.viewport().width(), row_h)
                self._paint_explorer_row_background(painter, row_rect, row)
                self._paint_explorer_row_separator(painter, row_rect)
        painter.end()

        # Temporarily make QPalette.Base transparent so super().paintEvent won't clear our drawing
        pal = self.viewport().palette()
        old_base = pal.brush(QPalette.Base)
        pal.setColor(QPalette.Base, Qt.transparent)
        self.viewport().setPalette(pal)

        try:
            # Let Qt draw the cells/grid on top (transparent background)
            super().paintEvent(event)
        finally:
            # Restore the palette
            pal.setBrush(QPalette.Base, old_base)
            self.viewport().setPalette(pal)

        # Draw rubber-band AFTER all cells
        if self._rubber_dragging and not self._rubber_rect.isEmpty():
            accent = _accent_color()
            painter = QPainter(self.viewport())
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.fillRect(self._rubber_rect, _with_alpha(accent, 40))
            painter.setPen(QPen(_with_alpha(accent, 180), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self._rubber_rect.adjusted(0, 0, -1, -1))
            painter.end()

    def mousePressEvent(self, event) -> None:
        pos = self._event_pos(event)

        if event.button() == Qt.LeftButton:
            # ── Checkbox hit-test in its dedicated fixed column ──────────────
            # Toggle is DEFERRED to mouseReleaseEvent so that dragging from the
            # checkbox zone can still start a rubber-band selection.
            idx = self.indexAt(pos)
            if idx.isValid() and idx.column() == COL_CHECK:
                self._pending_cb_row = idx.row()
                self._rubber_origin = pos
                self._rubber_active = True
                self._rubber_dragging = False
                self._rubber_modifiers = event.modifiers()
                self._rubber_rect = QRect()
                selection_model = self.selectionModel()
                self._rubber_base_selection = (
                    selection_model.selection()
                    if selection_model is not None else QItemSelection()
                )
                self._empty_area_pressed = False
                event.accept()
                return

            # ── Rubber-band tracking: start on any left-button press ─────────
            self._rubber_origin = pos
            self._rubber_active = True
            self._rubber_dragging = False
            self._rubber_modifiers = event.modifiers()
            self._rubber_rect = QRect()

            if self._is_empty_viewport_area(pos):
                # Empty area: record base, deselect, handle internally
                selection_model = self.selectionModel()
                self._rubber_base_selection = (
                    selection_model.selection() if selection_model is not None else QItemSelection()
                )
                self.clearSelection()
                self.setCurrentIndex(QModelIndex())
                self._empty_area_pressed = True
                event.accept()
                return
        else:
            self._cancel_rubber_band()
            self._empty_area_pressed = False

        self._empty_area_pressed = False
        super().mousePressEvent(event)
        # After Qt selects the clicked row, snapshot it as the rubber band baseline
        if event.button() == Qt.LeftButton and self._rubber_active:
            selection_model = self.selectionModel()
            self._rubber_base_selection = (
                selection_model.selection() if selection_model is not None else QItemSelection()
            )

    def mouseMoveEvent(self, event) -> None:
        if self._rubber_active and event.buttons() & Qt.LeftButton:
            self._update_empty_area_drag(self._event_pos(event))
            event.accept()
            return

        self._update_hover_row(self._event_pos(event))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._rubber_active:
            was_dragging = self._rubber_dragging
            pending_row = self._pending_cb_row
            empty_pressed = getattr(self, "_empty_area_pressed", False)
            self._empty_area_pressed = False
            self._finish_empty_area_interaction(self._event_pos(event))

            # Fire deferred checkbox toggle (only when no rubber-band drag occurred)
            if not was_dragging and pending_row >= 0:
                row = pending_row
                selection_model = self.selectionModel()
                if selection_model is not None:
                    model = self.model()
                    index = model.index(row, COL_CHECK)
                    selection_model.setCurrentIndex(index, QItemSelectionModel.NoUpdate)
                    selection_model.select(
                        index,
                        QItemSelectionModel.Toggle | QItemSelectionModel.Rows,
                    )
                self.viewport().update()
                event.accept()
                return

            if empty_pressed or was_dragging:
                event.accept()
                return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        pos = self._event_pos(event)
        if self._is_empty_viewport_area(pos):
            event.accept()
            return
        index = self.indexAt(pos)
        if index.isValid() and index.column() == COL_FILENAME:
            model = self.model()
            track = model.track_at_row(index.row()) if model is not None and hasattr(model, "track_at_row") else None
            if track is not None:
                self.openRequested.emit([track])
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:
        self._update_hover_row(QPoint(-1, -1))
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.SelectAll):
            self.selectAll()
            event.accept()
            return

        if event.key() == Qt.Key_Escape and event.modifiers() == Qt.NoModifier:
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
            event.accept()
            return

        if event.key() == Qt.Key_F2 and event.modifiers() == Qt.NoModifier:
            items = self._current_or_selected_items()
            if len(items) == 1:
                self.renameRequested.emit(items)
                event.accept()
                return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.NoModifier:
            items = self._current_or_selected_items()
            if items:
                self.openRequested.emit(items)
                event.accept()
                return

        if event.key() == Qt.Key_Space and event.modifiers() == Qt.NoModifier:
            if self._toggle_current_row_selection():
                event.accept()
                return

        if (
            event.key() == Qt.Key_Menu
            or (event.key() == Qt.Key_F10 and event.modifiers() == Qt.ShiftModifier)
        ):
            self.keyboardContextMenuRequested.emit(self._current_context_menu_pos())
            event.accept()
            return

        # Win11 Explorer: Delete (and Shift+Delete) sends selection to the
        # Recycle Bin via the panel's confirm-then-trash flow. The panel
        # owns the dialog + the controller signal — the view just collects
        # paths from the current selection.
        if (
            event.key() == Qt.Key_Delete
            and event.modifiers() in (Qt.NoModifier, Qt.ShiftModifier)
        ):
            model = self.model()
            selection_model = self.selectionModel()
            if model is None or selection_model is None or not hasattr(model, "track_at_row"):
                super().keyPressEvent(event)
                return
            paths: list[Path] = []
            for idx in selection_model.selectedRows():
                track = model.track_at_row(idx.row())
                if track is not None:
                    paths.append(track.path)
            if paths:
                self.deleteRequested.emit(paths)
            event.accept()
            return
        super().keyPressEvent(event)

    def _selected_items(self) -> list:
        model = self.model()
        selection_model = self.selectionModel()
        if model is None or selection_model is None or not hasattr(model, "track_at_row"):
            return []

        items = []
        for idx in selection_model.selectedRows():
            track = model.track_at_row(idx.row())
            if track is not None:
                items.append(track)
        return items

    def _current_or_selected_items(self) -> list:
        selected = self._selected_items()
        if selected:
            return selected

        model = self.model()
        current = self.currentIndex()
        if model is None or not current.isValid() or not hasattr(model, "track_at_row"):
            return []
        track = model.track_at_row(current.row())
        return [track] if track is not None else []

    def _begin_keyboard_rename(self) -> bool:
        model = self.model()
        if model is None:
            return False

        current = self.currentIndex()
        row = current.row() if current.isValid() else -1
        if row < 0:
            selection_model = self.selectionModel()
            rows = selection_model.selectedRows() if selection_model is not None else []
            row = rows[0].row() if rows else -1
        if row < 0:
            return False

        target_col = (
            COL_FILENAME_NEW
            if model.columnCount() > COL_FILENAME_NEW and not self.isColumnHidden(COL_FILENAME_NEW)
            else (current.column() if current.isValid() else COL_FILENAME)
        )
        target = model.index(row, target_col)
        if not target.isValid() or not (target.flags() & Qt.ItemIsEditable):
            return False

        self.setCurrentIndex(target)
        return self.edit(target)

    def _toggle_current_row_selection(self) -> bool:
        model = self.model()
        selection_model = self.selectionModel()
        current = self.currentIndex()
        if model is None or selection_model is None or not current.isValid():
            return False

        selection_model.select(current, QItemSelectionModel.Toggle | QItemSelectionModel.Rows)
        self.viewport().update()
        return True

    def _current_context_menu_pos(self) -> QPoint:
        current = self.currentIndex()
        if current.isValid():
            rect = self.visualRect(current)
            if rect.isValid():
                return rect.center()
        return self.viewport().rect().center()

    def selectionChanged(self, selected: QItemSelection, deselected: QItemSelection) -> None:
        super().selectionChanged(selected, deselected)
        self.selectedItemsChanged.emit(self._selected_items())

    @staticmethod
    def _event_pos(event) -> QPoint:
        if hasattr(event, "position"):
            return event.position().toPoint()
        return event.pos()

    def _visible_row_range(self, model) -> range:
        """Return the rows that can contribute pixels to the current viewport."""
        row_count = model.rowCount()
        if row_count <= 0:
            return range(0)

        first = self.rowAt(0)
        if first < 0:
            first = 0

        last = self.rowAt(max(0, self.viewport().height() - 1))
        if last < first:
            row_height = self.rowHeight(first)
            if row_height <= 0:
                row_height = self.verticalHeader().defaultSectionSize()
            visible_capacity = max(1, self.viewport().height() // max(1, row_height) + 2)
            last = min(row_count - 1, first + visible_capacity)

        first = max(0, min(first, row_count - 1))
        last = max(first, min(last, row_count - 1))
        return range(first, last + 1)

    def _cancel_rubber_band(self) -> None:
        self._rubber_active = False
        self._rubber_dragging = False
        self._pending_cb_row = -1
        if not self._rubber_rect.isEmpty():
            self._rubber_rect = QRect()
            self.viewport().update()

    def _update_empty_area_drag(self, pos: QPoint) -> None:
        if (
            self._rubber_dragging
            or (pos - self._rubber_origin).manhattanLength() >= QApplication.startDragDistance()
        ):
            self._rubber_dragging = True
            self._scroll_for_rubber(pos)
            old_rect = QRect(self._rubber_rect)
            self._rubber_rect = QRect(self._rubber_origin, pos).normalized()
            self._update_rubber_rect(old_rect, self._rubber_rect)
            self._select_rows_in_rubber_band()

    def _finish_empty_area_interaction(self, pos: QPoint) -> None:
        if self._rubber_dragging:
            self._rubber_rect = QRect(self._rubber_origin, pos).normalized()
            self._select_rows_in_rubber_band()
        self._cancel_rubber_band()

    def _explorer_palette(self) -> dict[str, QColor]:
        """Win11 Details-View palette in Microsoft system-accent blue.

        Keys ``base`` / ``row_alt`` track the theme background. ``separator`` is transparent — Win11 has
        no inter-row separator lines. ``hover_border`` is transparent too —
        Win11 hover has fill only, no outline.
        """
        colors = tag_editor_colors()
        is_dark = QColor(colors.surface).lightness() < 128
        bg = QColor(colors.surface)
        transparent = QColor(0, 0, 0, 0)
        accent = _accent_color()
        if is_dark:
            # Selected fill at ~50 % opacity over dark bg gives clearly visible blue.
            sel_fill = _with_alpha(accent, 32)
            sel_fill_ia = _with_alpha(accent, 24)
            sel_border = transparent
            sel_border_ia = transparent
            hover_fill = QColor(255, 255, 255, 18)
            return {
                "base": bg, "row_alt": bg,
                "hover": hover_fill, "hover_border": transparent,
                "selected": sel_fill, "selected_inactive": sel_fill_ia,
                "selected_border": sel_border,
                "selected_inactive_border": sel_border_ia,
                "separator": QColor(colors.border),
            }
        # Light mode
        sel_fill = _with_alpha(accent, 19)
        sel_fill_ia = _with_alpha(accent, 16)
        sel_border = transparent
        sel_border_ia = transparent
        hover_fill = QColor("#F8FAF9")
        return {
            "base": bg, "row_alt": bg,
            "hover": hover_fill, "hover_border": transparent,
            "selected": sel_fill, "selected_inactive": sel_fill_ia,
            "selected_border": sel_border,
            "selected_inactive_border": sel_border_ia,
            "separator": QColor("#EEF2EF"),
        }

    def _content_row_rect(self, row_rect: QRect, row: int) -> QRect:
        model = self.model()
        if model is None or not (0 <= row < model.rowCount()):
            return QRect(8, row_rect.top(), max(0, self.viewport().width() - 16), row_rect.height())

        left: int | None = None
        right: int | None = None
        for logical in range(model.columnCount()):
            if logical in (COL_GUTTER, COL_END_GUTTER) or self.isColumnHidden(logical):
                continue
            width = self.columnWidth(logical)
            if width <= 0:
                continue
            x = self.columnViewportPosition(logical)
            left = x if left is None else min(left, x)
            right = x + width - 1 if right is None else max(right, x + width - 1)

        if left is None or right is None:
            return QRect()

        # The fixed empty gutter is excluded above.  Do not derive the row
        # bounds from Filename: that column is user-movable now.
        return QRect(left, row_rect.top(), right - left + 1, row_rect.height())

    def _should_paint_row_background(self, index) -> bool:
        header = self.horizontalHeader()
        for visual in range(header.count()):
            logical = header.logicalIndex(visual)
            if self.isColumnHidden(logical):
                continue
            cell_rect = self.visualRect(index.siblingAtColumn(logical))
            if cell_rect.isValid() and cell_rect.intersects(self.viewport().rect()):
                return index.column() == logical
        return False

    # Prototype details-table geometry: full-width flat rows.
    _CAPSULE_INSET_X = 0
    _CAPSULE_INSET_Y = 2
    _CAPSULE_RADIUS  = 0

    def _paint_explorer_row_background(self, painter: QPainter, row_rect: QRect, row: int) -> None:
        colors = self._explorer_palette()
        selection_model = self.selectionModel()
        is_selected = bool(
            selection_model and selection_model.rowIntersectsSelection(row, QModelIndex())
        )
        is_hover = (row == self._hovered_row)
        if not (is_selected or is_hover):
            return

        if is_selected:
            fill_key   = "selected"       if self.hasFocus() else "selected_inactive"
            border_key = "selected_border" if self.hasFocus() else "selected_inactive_border"
        else:
            fill_key   = "hover"
            border_key = "hover_border"

        capsule = self._content_row_rect(row_rect, row).adjusted(
            self._CAPSULE_INSET_X,  self._CAPSULE_INSET_Y,
            -self._CAPSULE_INSET_X, -self._CAPSULE_INSET_Y,
        )
        if capsule.width() <= 0 or capsule.height() <= 0:
            return

        painter.save()
        painter.setClipRect(
            QRect(0, row_rect.top(), self.viewport().width(), row_rect.height()),
            Qt.ReplaceClip,
        )
        painter.setRenderHint(QPainter.Antialiasing, False)
        border = colors[border_key]
        if border.alpha() > 0:
            painter.setPen(QPen(border, 1))
        else:
            painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(colors[fill_key]))
        painter.drawRect(capsule)
        if is_selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_accent_color())
            if QApplication.layoutDirection() == Qt.RightToLeft:
                painter.drawRect(capsule.right() - 3, capsule.top(), 4, capsule.height())
            else:
                painter.drawRect(capsule.left(), capsule.top(), 4, capsule.height())
        painter.restore()

    def _paint_explorer_row_separator(self, painter: QPainter, row_rect: QRect) -> None:
        color = self._explorer_palette()["separator"]
        if color.alpha() <= 0:
            return
        painter.save()
        painter.setPen(QPen(color, 1))
        painter.drawLine(row_rect.left(), row_rect.bottom(), row_rect.right(), row_rect.bottom())
        painter.restore()

    def _is_empty_viewport_area(self, pos: QPoint) -> bool:
        if not self.viewport().rect().contains(pos):
            return True

        is_rtl = QApplication.layoutDirection() == Qt.RightToLeft
        if is_rtl:
            if pos.x() >= self.viewport().width() - self._SIDE_EMPTY_GUTTER:
                return True
            if pos.x() < self._END_EMPTY_GUTTER:
                return True
        else:
            if pos.x() < self._SIDE_EMPTY_GUTTER:
                return True
            if pos.x() >= self.viewport().width() - self._END_EMPTY_GUTTER:
                return True

        idx = self.indexAt(pos)
        if not idx.isValid():
            return True

        if idx.column() in (COL_GUTTER, COL_END_GUTTER):
            return True

        model = self.model()
        if model is None or model.rowCount() == 0:
            return True
        last_row = model.rowCount() - 1
        last_bottom = self.rowViewportPosition(last_row) + self.rowHeight(last_row)
        return pos.y() >= last_bottom

    def _update_hover_row(self, pos: QPoint) -> None:
        row = self.indexAt(pos).row() if self.viewport().rect().contains(pos) else -1
        if row == self._hovered_row:
            return
        old_row = self._hovered_row
        self._hovered_row = row
        for changed_row in (old_row, row):
            if changed_row >= 0:
                self.viewport().update(
                    QRect(0, self.rowViewportPosition(changed_row), self.viewport().width(), self.rowHeight(changed_row))
                )

    def _scroll_for_rubber(self, pos: QPoint) -> None:
        margin = 24
        bar = self.verticalScrollBar()
        if pos.y() < margin:
            bar.setValue(bar.value() - 1)
        elif pos.y() > self.viewport().height() - margin:
            bar.setValue(bar.value() + 1)

    def _select_rows_in_rubber_band(self) -> None:
        model = self.model()
        selection_model = self.selectionModel()
        if model is None or selection_model is None:
            return

        rubber_rect = self._rubber_rect.normalized()
        rubber_selection = QItemSelection()
        last_col = model.columnCount() - 1
        if last_col < 0:
            selection_model.clearSelection()
            return

        for row in self._visible_row_range(model):
            row_rect = QRect(
                0,
                self.rowViewportPosition(row),
                self.viewport().width(),
                self.rowHeight(row),
            )
            if rubber_rect.intersects(row_rect):
                rubber_selection.select(model.index(row, 0), model.index(row, last_col))

        final_selection = rubber_selection
        if self._rubber_modifiers & (Qt.ControlModifier | Qt.ShiftModifier):
            final_selection = QItemSelection()
            final_selection.merge(self._rubber_base_selection, QItemSelectionModel.Select)
            final_selection.merge(rubber_selection, QItemSelectionModel.Select)

        if final_selection.isEmpty():
            selection_model.clearSelection()
        else:
            selection_model.select(
                final_selection,
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
            )

    def _update_rubber_rect(self, old_rect: QRect, new_rect: QRect) -> None:
        update_rect = QRect(old_rect).united(new_rect).adjusted(-2, -2, 2, 2)
        if update_rect.isEmpty():
            self.viewport().update()
        else:
            self.viewport().update(update_rect)


# Backwards-compatible name for older imports. New code should use
# ExplorerDetailsView.
ExplorerFileListView = ExplorerDetailsView
