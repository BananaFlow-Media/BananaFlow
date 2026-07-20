"""Additive filtering layer for the Tag Editor Details model (Phase 2)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt

from core.filesystem_monitoring import is_external_change
from core.metadata_models import AudioTrackItem
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


class MetadataFilterProxyModel(QSortFilterProxyModel):
    """Visibility only.  Apply scope always remains in ``workspace``.

    Folder/search UI is intentionally deferred to Phase 3.  The allowed-path
    API is enough to prove now that a hidden item still remains an Apply
    candidate, and leaves the proxy ready for those later predicates.
    """

    def __init__(self, workspace: TagEditorWorkspaceState, parent=None) -> None:
        super().__init__(parent)
        self._workspace = workspace
        # Internal visibility is item-ID based so a Phase 1 rename does not
        # turn an allowed item into a hidden one (or vice versa).
        self._allowed_ids: set[int] | None = None
        self._show_excluded_only = False
        self._show_stale_only = False
        self._folder: Path | None = None
        self._search_text = ""
        self.setDynamicSortFilter(True)
        # Selection-only workspace changes do not affect visibility. Avoid an
        # O(n) proxy pass for every click in a 10k library.
        # Both slots are bound methods so Qt owns the receiver context and
        # disconnects them when this proxy is destroyed; a lambda would keep
        # the workspace calling into a deleted C++ object.
        workspace.apply_scope_changed.connect(self._on_workspace_changed)
        workspace.content_changed.connect(self._on_content_changed)
        self.invalidation_count = 0

    @property
    def workspace(self) -> TagEditorWorkspaceState:
        return self._workspace

    def set_allowed_paths(self, paths: set[Path] | list[Path] | None) -> None:
        new_ids = None if paths is None else {
            self._workspace.item_id(item)
            for path in paths
            if (item := self._workspace.track_for_path(path)) is not None
        }
        if new_ids == self._allowed_ids:
            return
        self._allowed_ids = new_ids
        self._refilter()
        self._sync_visible_paths()

    def set_path_visible(self, path: Path, visible: bool) -> None:
        item = self._workspace.track_for_path(path)
        if item is None:
            return
        if self._allowed_ids is None:
            self._allowed_ids = {self._workspace.item_id(track) for track in self._workspace.tracks}
        identity = self._workspace.item_id(item)
        if visible:
            self._allowed_ids.add(identity)
        else:
            self._allowed_ids.discard(identity)
        self._refilter()
        self._sync_visible_paths()

    def set_all_visible(self, visible: bool) -> None:
        self._allowed_ids = None if visible else set()
        self._refilter()
        self._sync_visible_paths()

    def set_show_excluded_only(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._show_excluded_only == enabled:
            return
        self._show_excluded_only = enabled
        self._refilter()
        self._sync_visible_paths()

    def show_excluded_only(self) -> bool:
        return self._show_excluded_only

    def set_show_stale_only(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._show_stale_only == enabled:
            return
        self._show_stale_only = enabled
        self._refilter()
        self._sync_visible_paths()

    def show_stale_only(self) -> bool:
        return self._show_stale_only

    def set_folder(self, folder: Path | None) -> None:
        """Restrict rows to one folder; ``None`` restores the complete workspace."""
        folder = Path(folder) if folder is not None else None
        if folder == self._folder:
            return
        self._folder = folder
        self._refilter()
        self._sync_visible_paths()

    def folder(self) -> Path | None:
        return self._folder

    def set_search_text(self, text: str) -> None:
        normalized = text.casefold().strip()
        if normalized == self._search_text:
            return
        self._search_text = normalized
        self._refilter()
        self._sync_visible_paths()

    def search_text(self) -> str:
        return self._search_text

    def track_at_row(self, row: int) -> AudioTrackItem | None:
        index = self.index(row, 0)
        if not index.isValid():
            return None
        source = self.mapToSource(index)
        model = self.sourceModel()
        return model.track_at_row(source.row()) if model is not None and source.isValid() else None

    def visible_tracks(self) -> list[AudioTrackItem]:
        return [self.track_at_row(row) for row in range(self.rowCount()) if self.track_at_row(row)]

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        item = model.track_at_row(source_row) if model is not None else None
        if item is None:
            return False
        if self._allowed_ids is not None and self._workspace.item_id(item) not in self._allowed_ids:
            return False
        if self._show_excluded_only:
            if not (item.has_changes and item.excluded_from_apply):
                return False
        # The same predicate the chip counts with, so the number the user
        # clicks always matches the rows they then see.
        if self._show_stale_only and not is_external_change(
                getattr(item, "external_state", "current")):
            return False
        if self._folder is not None and item.folder != self._folder:
            return False
        if self._search_text:
            haystack = " ".join((
                item.path.name, item.original.title, item.original.artist,
                item.original.album, item.proposed.title or "", item.proposed.artist or "",
                item.proposed.album or "",
            )).casefold()
            if self._search_text not in haystack:
                return False
        return True

    def _on_workspace_changed(self) -> None:
        self._refilter()
        self._sync_visible_paths()

    def _on_content_changed(self, _revision: int) -> None:
        self._on_workspace_changed()

    def _refilter(self) -> None:
        self.invalidation_count += 1
        self.beginFilterChange()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def _sync_visible_paths(self) -> None:
        # QModel reset/layout notifications are delivered synchronously by Qt
        # here; use proxy rows, never source row numbers, as the view truth.
        self._workspace.set_visible_items(self.visible_tracks())
