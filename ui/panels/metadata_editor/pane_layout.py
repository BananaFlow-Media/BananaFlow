"""
ui/panels/metadata_editor/pane_layout.py  –  Tag Editor
==============================================================================
Three-pane splitter geometry: collapse/expand, width snapping, and
the cascade that keeps the centre table above its minimum when a
side pane is dragged.

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


class PaneLayoutMixin:
    """Three-pane splitter geometry: collapse/expand, width snapping, and"""

    def _set_pane_collapsed(self, pane: int, collapsed: bool) -> None:
        """Collapse/expand a side pane (0 = tree, 2 = inspector) to/from its rail.

        Expanding takes width from the table first (down to its minimum) and
        then, if still short of the pane's open minimum, from the other side
        pane. Collapsing hands the freed width to the table.
        """
        if not hasattr(self, "_body_splitter"):
            return
        other = 2 if pane == 0 else 0
        rail, open_min = (
            (self._TREE_RAIL_WIDTH, self._TREE_OPEN_MIN)
            if pane == 0
            else (self._INSPECTOR_RAIL_WIDTH, self._INSPECTOR_OPEN_MIN)
        )
        other_rail = self._INSPECTOR_RAIL_WIDTH if pane == 0 else self._TREE_RAIL_WIDTH

        sizes = self._body_splitter.sizes()
        if len(sizes) != 3:
            sizes = list(self._DEFAULT_SPLITTER_SIZES)

        if collapsed:
            if sizes[pane] > rail + 4:
                last = max(open_min, sizes[pane])
                if pane == 0:
                    self._last_tree_width = last
                else:
                    self._last_inspector_width = last
            sizes[1] += max(0, sizes[pane] - rail)
            sizes[pane] = rail
        else:
            last = self._last_tree_width if pane == 0 else self._last_inspector_width
            want = max(open_min, last)
            available = max(0, sizes[1] - self._TABLE_OPEN_MIN)
            take = min(available, max(0, want - sizes[pane]))
            sizes[pane] += take
            sizes[1] -= take
            if sizes[pane] < open_min and sizes[other] > other_rail:
                extra = min(sizes[other] - other_rail, open_min - sizes[pane])
                sizes[pane] += extra
                sizes[other] -= extra
        self._apply_body_sizes(sizes, save=True)

    def _set_tree_collapsed(self, collapsed: bool) -> None:
        self._set_pane_collapsed(0, collapsed)

    def _toggle_tree_pane(self) -> None:
        self._set_tree_collapsed(not self._left_collapsed)

    def _set_inspector_collapsed(self, collapsed: bool) -> None:
        self._set_pane_collapsed(2, collapsed)

    def _apply_body_sizes(self, sizes: list[int], save: bool) -> None:
        if not hasattr(self, "_body_splitter"):
            return
        sizes = self._normalize_body_sizes(list(sizes))
        left_collapsed = sizes[0] <= self._TREE_RAIL_WIDTH + 4
        right_collapsed = sizes[2] <= self._INSPECTOR_RAIL_WIDTH + 4
        self._sync_collapsed_visuals(left_collapsed, right_collapsed)

        self._ignore_splitter_save = True
        try:
            self._body_splitter.setSizes(sizes)
        finally:
            self._ignore_splitter_save = False
        if save:
            self._save_splitter_sizes(self._body_splitter)

    @staticmethod
    def _snap_side_size(size: int, rail: int, open_min: int) -> int:
        """Sanitize a side-pane width: a pane is either collapsed to its rail
        or open at >= its open minimum — never squished in between (stale
        saved sizes, old configs)."""
        if 0 < size < open_min:
            return rail if size <= rail + 4 else open_min
        return size

    def _normalize_body_sizes(self, sizes: list[int]) -> list[int]:
        """Clamp programmatic sizes (config restore, collapse toggles) to the
        pane invariants. Native drags never pass through here — Qt enforces
        the same floors via the pane widgets' minimumWidth."""
        if len(sizes) != 3:
            sizes = list(self._DEFAULT_SPLITTER_SIZES)
        sizes = [max(0, int(v)) for v in sizes]
        total = sum(sizes) or sum(self._DEFAULT_SPLITTER_SIZES)

        sizes[0] = self._snap_side_size(sizes[0], self._TREE_RAIL_WIDTH, self._TREE_OPEN_MIN)
        sizes[2] = self._snap_side_size(sizes[2], self._INSPECTOR_RAIL_WIDTH, self._INSPECTOR_OPEN_MIN)

        # Keep the table at its minimum by collapsing side panes fully to
        # their rail if needed (inspector first, then tree). A *partial* cut
        # would leave a pane squished between its rail and open minimum --
        # a state _snap_side_size does not allow to persist, so re-snapping
        # a partial cut can jump it back up to its open minimum and silently
        # undo the cut. Collapsing to rail outright is the only move that
        # stays valid without a second snap pass.
        side_total = sizes[0] + sizes[2]
        if total - side_total < self._TABLE_OPEN_MIN:
            deficit = self._TABLE_OPEN_MIN - (total - side_total)
            if deficit > 0 and sizes[2] > self._INSPECTOR_RAIL_WIDTH:
                deficit -= sizes[2] - self._INSPECTOR_RAIL_WIDTH
                sizes[2] = self._INSPECTOR_RAIL_WIDTH
            if deficit > 0 and sizes[0] > self._TREE_RAIL_WIDTH:
                deficit -= sizes[0] - self._TREE_RAIL_WIDTH
                sizes[0] = self._TREE_RAIL_WIDTH

        sizes[1] = max(self._TABLE_OPEN_MIN, total - sizes[0] - sizes[2])
        overflow = sum(sizes) - total
        if overflow > 0:
            sizes[1] = max(0, sizes[1] - overflow)
        return sizes

    def _sync_collapsed_visuals(self, left_collapsed: bool, right_collapsed: bool) -> None:
        self._left_collapsed = left_collapsed
        self._right_collapsed = right_collapsed
        if hasattr(self, "_tree_rail"):
            self._tree_rail.setVisible(left_collapsed)
        if hasattr(self, "_tree_body"):
            self._tree_body.setVisible(not left_collapsed)
        if hasattr(self, "_tree_frame"):
            self._tree_frame.setMaximumWidth(self._TREE_RAIL_WIDTH if left_collapsed else 16777215)
        if hasattr(self, "_inspector_content"):
            self._inspector_content.setVisible(not right_collapsed)
        if hasattr(self, "_inspector_rail"):
            self._inspector_rail.setVisible(right_collapsed)
        if hasattr(self, "_inspector_shell"):
            self._inspector_shell.setMaximumWidth(self._INSPECTOR_RAIL_WIDTH if right_collapsed else 16777215)
        self._refresh_tool_button_states()

    def _pane_after_shrink(self, current: int, shrink: int, open_min: int, collapsed_width: int) -> int:
        candidate = current - shrink
        if candidate >= open_min:
            return candidate
        if candidate >= open_min - self._COLLAPSE_DRAG_MARGIN:
            return open_min
        return collapsed_width

    def _visual_splitter_delta(self, delta: int) -> int:
        if self._body_splitter.layoutDirection() == Qt.LayoutDirection.RightToLeft:
            return -delta
        return delta

    def _cascade_splitter_sizes(self, handle_index: int, start_sizes: list[int], delta: int) -> list[int]:
        delta = self._visual_splitter_delta(delta)
        left, center, right = self._normalize_body_sizes(start_sizes)
        total = left + center + right

        if handle_index == 1:
            if delta < 0:
                left = self._pane_after_shrink(left, -delta, self._TREE_OPEN_MIN, self._TREE_RAIL_WIDTH)
                center = total - left - right
            else:
                center_candidate = center - delta
                if center_candidate >= self._TABLE_OPEN_MIN:
                    center = center_candidate
                elif center_candidate >= self._TABLE_OPEN_MIN - self._COLLAPSE_DRAG_MARGIN:
                    center = self._TABLE_OPEN_MIN
                else:
                    extra = self._TABLE_OPEN_MIN - center_candidate
                    center = self._TABLE_OPEN_MIN
                    right = self._pane_after_shrink(right, extra, self._INSPECTOR_OPEN_MIN, self._INSPECTOR_RAIL_WIDTH)
                left = total - center - right
        else:
            if delta > 0:
                right = self._pane_after_shrink(right, delta, self._INSPECTOR_OPEN_MIN, self._INSPECTOR_RAIL_WIDTH)
                center = total - left - right
            else:
                grow = -delta
                center_candidate = center - grow
                if center_candidate >= self._TABLE_OPEN_MIN:
                    center = center_candidate
                elif center_candidate >= self._TABLE_OPEN_MIN - self._COLLAPSE_DRAG_MARGIN:
                    center = self._TABLE_OPEN_MIN
                else:
                    extra = self._TABLE_OPEN_MIN - center_candidate
                    center = self._TABLE_OPEN_MIN
                    left = self._pane_after_shrink(left, extra, self._TREE_OPEN_MIN, self._TREE_RAIL_WIDTH)
                right = total - left - center

        return self._normalize_body_sizes([left, center, right])

    def eventFilter(self, obj, event) -> bool:
        from PySide6.QtCore import QEvent
        if hasattr(self, "_body_splitter") and obj in (self._body_splitter.handle(1), self._body_splitter.handle(2)):
            event_type = event.type()
            if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._splitter_drag = {
                    "handle": int(obj.property("metadata_handle_index")),
                    "x": int(event.globalPosition().x()),
                    "sizes": self._body_splitter.sizes(),
                }
                return True
            if event_type == QEvent.Type.MouseMove and self._splitter_drag is not None:
                delta = int(event.globalPosition().x()) - int(self._splitter_drag["x"])
                sizes = self._cascade_splitter_sizes(
                    int(self._splitter_drag["handle"]),
                    list(self._splitter_drag["sizes"]),
                    delta,
                )
                if sizes[0] > self._TREE_RAIL_WIDTH + 4:
                    self._last_tree_width = sizes[0]
                if sizes[2] > self._INSPECTOR_RAIL_WIDTH + 4:
                    self._last_inspector_width = sizes[2]
                self._apply_body_sizes(sizes, save=False)
                return True
            if event_type == QEvent.Type.MouseButtonRelease and self._splitter_drag is not None:
                self._save_splitter_sizes(self._body_splitter)
                self._splitter_drag = None
                return True
        return super().eventFilter(obj, event)
