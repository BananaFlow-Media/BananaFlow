"""
ui/models/metadata_table_model.py  –  Qt model for the Tag Editor preview table
================================================================================
QAbstractTableModel subclass that displays AudioTrackItem objects in a
before/after layout.  Changed cells are highlighted in accent colour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QApplication

from core.metadata_models import AudioTrackItem, TrackStatus
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.i18n import t
from ui.theme_manager import ERROR_COLOR, WARNING_COLOR, get_colors

# ── Column constants ──────────────────────────────────────────────────────────

COL_CHECK        = 0
COL_FILENAME     = 1
COL_TITLE_CUR    = 2
COL_TITLE_NEW    = 3
COL_ARTIST_CUR   = 4
COL_ARTIST_NEW   = 5
COL_ALBUM_CUR    = 6
COL_ALBUM_NEW    = 7
COL_TRACK_CUR    = 8
COL_TRACK_NEW    = 9
# Extended columns (hidden by default, user-toggleable)
COL_FILENAME_NEW = 10
COL_GENRE_CUR    = 11
COL_GENRE_NEW    = 12
COL_COMMENT_CUR  = 13
COL_COMMENT_NEW  = 14
# Row state (changed on disk / read-only / N pending changes). Previously this
# only ever reached the user as a row tint and a tooltip, which cannot be
# sorted, filtered or read by a screen reader.
COL_STATUS       = 15
# Fixed, intentionally blank click-to-clear strip.  Kept separate from the
# selection checkbox column so neither can stretch or borrow Filename space.
COL_GUTTER       = 16
# Matching click-to-clear strip at the opposite edge, intentionally narrower.
COL_END_GUTTER   = 17
COLUMN_COUNT     = 18
EXTERNAL_STATE_ROLE = int(Qt.UserRole) + 13


@dataclass
class ModelOperationCounters:
    resets: int = 0
    inserted_rows: int = 0
    removed_rows: int = 0
    refreshed_rows: int = 0
    layout_changes: int = 0

# Header *translation keys*, looked up via t() in headerData() so the
# headers reflect the active language each time the view repaints.
_HEADER_KEYS: list[str] = [
    "",
    "mt_col_filename",
    "mt_col_title",        "mt_col_title_new",
    "mt_col_artist",       "mt_col_artist_new",
    "mt_col_album",        "mt_col_album_new",
    "mt_col_track",        "mt_col_track_new",
    "mt_col_filename_new",
    "mt_col_genre",        "mt_col_genre_new",
    "mt_col_comment",      "mt_col_comment_new",
    "mt_col_status",
    "",
    "",
]


def _headers() -> list[str]:
    return [t(k) if k else "" for k in _HEADER_KEYS]


# Backwards-compat alias for any external importer that reads `_HEADERS`
# at import time. Captures the English headers at module load — UI code
# should use ``_headers()`` (or ``model.headerData()``) for live values.
_HEADERS = _headers()

# Alpha-blended accent / warning brush colours
_ERROR_BG   = QColor(ERROR_COLOR)
_ERROR_BG.setAlpha(35)
_WARN_BG    = QColor(WARNING_COLOR)
_WARN_BG.setAlpha(35)

_ERROR_BRUSH  = QBrush(_ERROR_BG)
_WARN_BRUSH   = QBrush(_WARN_BG)


def _accent_brush() -> QBrush:
    bg = QColor(get_colors().accent)
    bg.setAlpha(55)
    return QBrush(bg)


# Per-column sort key extractors. The model's sort() looks up the column
# in this table; columns not listed (e.g. CHECK with no meaningful order)
# are no-ops. Strings are case-folded so sorting is locale-friendly.
def _safe_int(v) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0

_NATURAL_PARTS = re.compile(r"(\d+)")


def _fold(s) -> tuple:
    """Case-insensitive natural-sort key (track 2 before track 10)."""
    if not isinstance(s, str):
        return ()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PARTS.split(s)
    )


def _file_type_label(item: AudioTrackItem) -> str:
    ext = (item.ext or item.path.suffix).lstrip(".").upper()
    if ext:
        return t("mt_file_type_audio", ext=ext)
    return t("mt_file_type_unknown")


def _file_status_label(item: AudioTrackItem) -> str:
    external = getattr(item, "external_state", "current")
    if external != "current":
        key = f"meta_external_state_{external}"
        value = t(key)
        if value != key:
            return value
    if item.status == TrackStatus.UNSUPPORTED:
        return item.error_msg or t("meta_unsupported_format_tooltip")
    if item.status == TrackStatus.ERROR:
        return item.error_msg or t("mt_status_error")
    return ""


def _pending_change_count(item: AudioTrackItem) -> int:
    count = len(item.proposed.changed_fields(item.original))
    return count + 1 if item.proposed_filename else count


def _row_status_label(item: AudioTrackItem) -> str:
    """One phrase for the row's state, in order of what blocks the user first.

    A file that changed on disk matters more than the edits queued against it,
    and a read-only format matters more than having nothing queued at all.
    """
    external = _file_status_label(item)
    if external:
        return external
    pending = _pending_change_count(item)
    if pending:
        return t("mt_status_pending_changes", n=pending)
    if not item.metadata_editable:
        return t("mt_status_read_only")
    return ""


def _row_status_rank(item: AudioTrackItem) -> tuple:
    """Sort order: what needs attention first, then alphabetically."""
    if _file_status_label(item):
        rank = 0
    elif _pending_change_count(item):
        rank = 1
    elif not item.metadata_editable:
        rank = 2
    else:
        rank = 3
    return (rank, -_pending_change_count(item), _fold(_row_status_label(item)))


def _file_tooltip(item: AudioTrackItem) -> str:
    lines = [
        t("mt_file_tooltip_path", path=str(item.path)),
        t("mt_file_tooltip_type", type=_file_type_label(item)),
    ]
    status = _file_status_label(item)
    if status:
        lines.append(t("mt_file_tooltip_status", status=status))
    if item.proposed_filename:
        lines.append(t("mt_file_tooltip_new_name", name=item.proposed_filename))
    return "\n".join(lines)

_SORT_KEYS = {
    COL_CHECK:        lambda t: 0,
    COL_FILENAME:     lambda t: _fold(getattr(t, "display_name", "") or t.path.name),
    COL_FILENAME_NEW: lambda t: _fold(getattr(t, "proposed_filename", "") or ""),
    COL_TITLE_CUR:    lambda t: _fold(t.original.title or ""),
    COL_TITLE_NEW:    lambda t: _fold((t.proposed.title  if t.proposed.title  is not None else t.original.title)  or ""),
    COL_ARTIST_CUR:   lambda t: _fold(t.original.artist or ""),
    COL_ARTIST_NEW:   lambda t: _fold((t.proposed.artist if t.proposed.artist is not None else t.original.artist) or ""),
    COL_ALBUM_CUR:    lambda t: _fold(t.original.album  or ""),
    COL_ALBUM_NEW:    lambda t: _fold((t.proposed.album  if t.proposed.album  is not None else t.original.album)  or ""),
    COL_TRACK_CUR:    lambda t: _safe_int(t.original.track_num),
    COL_TRACK_NEW:    lambda t: _safe_int(t.proposed.track_num if t.proposed.track_num is not None else t.original.track_num),
    COL_GENRE_CUR:    lambda t: _fold(t.original.genre   or ""),
    COL_GENRE_NEW:    lambda t: _fold((t.proposed.genre   if t.proposed.genre   is not None else t.original.genre)   or ""),
    COL_COMMENT_CUR:  lambda t: _fold(t.original.comment or ""),
    COL_COMMENT_NEW:  lambda t: _fold((t.proposed.comment if t.proposed.comment is not None else t.original.comment) or ""),
    COL_STATUS:       _row_status_rank,
}


class MetadataTableModel(QAbstractTableModel):
    """Source model for loaded tracks; scope semantics live in the workspace."""

    def __init__(self, parent=None, workspace: TagEditorWorkspaceState | None = None) -> None:
        super().__init__(parent)
        self._workspace = workspace or TagEditorWorkspaceState(self)
        self._tracks:      list[AudioTrackItem] = []
        self._visible_pos: dict[int, int] = {}
        self._path_to_idx: dict[Path, int] = {}      # fast path → master-idx lookup

        # Source rows are all loaded tracks; filtering belongs to the proxy.
        self._visible: list[int] = []
        self._sort_column: int | None = None
        self._sort_order: Qt.SortOrder = Qt.AscendingOrder
        self.operation_counters = ModelOperationCounters()

    # ── QAbstractTableModel interface ─────────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent=QModelIndex()) -> int:
        return COLUMN_COUNT

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                if section < len(_HEADER_KEYS):
                    key = _HEADER_KEYS[section]
                    text = t(key) if key else ""
                    return text
                return ""
            elif role == Qt.TextAlignmentRole:
                if orientation == Qt.Orientation.Horizontal:
                    if QApplication.layoutDirection() == Qt.RightToLeft:
                        return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter
                    else:
                        return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()
        if row >= len(self._visible):
            return None

        track_idx = self._visible[row]
        item = self._tracks[track_idx]
        p = item.proposed
        o = item.original

        if role == EXTERNAL_STATE_ROLE:
            return getattr(item, "external_state", "current")

        # ── CheckState ────────────────────────────────────────────────────────
        if role == Qt.CheckStateRole and col == COL_CHECK:
            return Qt.Unchecked

        # ── TextAlignmentRole ─────────────────────────────────────────────────
        if role == Qt.TextAlignmentRole:
            if QApplication.layoutDirection() == Qt.RightToLeft:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter
            else:
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter

        # ── DisplayRole ───────────────────────────────────────────────────────
        if role == Qt.DisplayRole:
            if col == COL_FILENAME:
                return item.display_name
            if col == COL_TITLE_CUR:
                return o.title
            if col == COL_TITLE_NEW:
                return p.title if p.title is not None else ""
            if col == COL_ARTIST_CUR:
                return o.artist
            if col == COL_ARTIST_NEW:
                return p.artist if p.artist is not None else ""
            if col == COL_ALBUM_CUR:
                return o.album
            if col == COL_ALBUM_NEW:
                return p.album if p.album is not None else ""
            if col == COL_TRACK_CUR:
                return str(o.track_num) if o.track_num is not None else ""
            if col == COL_TRACK_NEW:
                if p.track_num is not None:
                    return "" if p.track_num == -1 else str(p.track_num)
            if col == COL_FILENAME_NEW:
                return item.proposed_filename or ""
            if col == COL_GENRE_CUR:
                return o.genre
            if col == COL_GENRE_NEW:
                return p.genre if p.genre is not None else ""
            if col == COL_COMMENT_CUR:
                return o.comment
            if col == COL_COMMENT_NEW:
                return p.comment if p.comment is not None else ""
            if col == COL_STATUS:
                return _row_status_label(item)
            return None

        # ── BackgroundRole ────────────────────────────────────────────────────
        if role == Qt.BackgroundRole:
            if item.status == TrackStatus.ERROR:
                return _ERROR_BRUSH
            if item.status == TrackStatus.UNSUPPORTED:
                return _WARN_BRUSH
            # Highlight "New" cells that differ from original
            if col == COL_TITLE_NEW  and p.title  is not None and p.title  != o.title:
                return _accent_brush()
            if col == COL_ARTIST_NEW and p.artist is not None and p.artist != o.artist:
                return _accent_brush()
            if col == COL_ALBUM_NEW  and p.album  is not None and p.album  != o.album:
                return _accent_brush()
            if col == COL_TRACK_NEW     and p.track_num is not None and p.track_num != o.track_num:
                return _accent_brush()
            if col == COL_FILENAME_NEW  and item.proposed_filename:
                return _accent_brush()
            if col == COL_GENRE_NEW     and p.genre    is not None and p.genre    != o.genre:
                return _accent_brush()
            if col == COL_COMMENT_NEW   and p.comment  is not None and p.comment  != o.comment:
                return _accent_brush()
            return None

        # ── ForegroundRole ────────────────────────────────────────────────────
        if role == Qt.ForegroundRole:
            if item.status in (TrackStatus.UNSUPPORTED, TrackStatus.ERROR):
                from ui.theme_manager import get_colors
                return QBrush(QColor(get_colors().text_tertiary))
            return None

        # ── FontRole ─────────────────────────────────────────────────────────
        if role == Qt.FontRole:
            _new_cols = (
                COL_TITLE_NEW, COL_ARTIST_NEW, COL_ALBUM_NEW, COL_TRACK_NEW,
                COL_FILENAME_NEW, COL_GENRE_NEW, COL_COMMENT_NEW,
            )
            if col in _new_cols and item.has_changes:
                f = QFont()
                f.setBold(True)
                return f
            return None

        # ── ToolTipRole ───────────────────────────────────────────────────────
        if role == Qt.ToolTipRole:
            if col in (COL_FILENAME, COL_FILENAME_NEW):
                return _file_tooltip(item)
            if item.error_msg:
                return _file_status_label(item) or item.error_msg
            return None

        if role == Qt.AccessibleTextRole:
            value = self.data(index, Qt.DisplayRole)
            return "" if value is None else str(value)

        if role == Qt.AccessibleDescriptionRole:
            if col in (COL_FILENAME, COL_FILENAME_NEW):
                return _file_tooltip(item)
            if item.error_msg:
                return _file_status_label(item) or item.error_msg
            return None

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or role not in (Qt.EditRole, Qt.CheckStateRole):
            return False

        row = index.row()
        col = index.column()
        if row >= len(self._visible):
            return False

        track_idx = self._visible[row]
        item = self._tracks[track_idx]

        if role == Qt.CheckStateRole and col == COL_CHECK:
            return False

        if role == Qt.EditRole:
            val = str(value).strip()
            if col == COL_TITLE_NEW:
                item.proposed.title = val
            elif col == COL_ARTIST_NEW:
                item.proposed.artist = val
            elif col == COL_ALBUM_NEW:
                item.proposed.album = val
            elif col == COL_TRACK_NEW:
                try:
                    item.proposed.track_num = int(val) if val else None
                except ValueError:
                    return False
            elif col == COL_FILENAME_NEW:
                item.proposed_filename = val if val else None
            elif col == COL_GENRE_NEW:
                item.proposed.genre = val
            elif col == COL_COMMENT_NEW:
                item.proposed.comment = val
            else:
                return False
            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.BackgroundRole, Qt.FontRole])
            self._workspace.reconcile()
            return True

        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags
        col = index.column()
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if col in (
            COL_TITLE_NEW, COL_ARTIST_NEW, COL_ALBUM_NEW, COL_TRACK_NEW,
            COL_FILENAME_NEW, COL_GENRE_NEW, COL_COMMENT_NEW,
        ):
            return base | Qt.ItemIsEditable
        return base

    # ── Public API ─────────────────────────────────────────────────────────────

    def load_tracks(self, tracks: list[AudioTrackItem]) -> None:
        """Replace entire dataset and rebuild view."""
        self.operation_counters.resets += 1
        self.beginResetModel()
        self._tracks = list(tracks)
        loaded = self._workspace.tracks
        workspace_changed = len(loaded) != len(self._tracks) or any(
            left is not right for left, right in zip(loaded, self._tracks))
        self._path_to_idx = {t.path: i for i, t in enumerate(self._tracks)}
        self._rebuild_visible()
        self.endResetModel()
        # set_tracks emits content_changed, which makes the proxy invalidate
        # and synchronously query this model.  Emitting it between
        # beginResetModel/endResetModel re-entered QSortFilterProxyModel while
        # its source rows were transient, causing a native Qt/PySide crash
        # when replacing the current folder.  Publish the workspace only once
        # the source reset is complete and its rows are safe to inspect.
        if workspace_changed:
            self._workspace.set_tracks(self._tracks)

    def add_track(self, item: AudioTrackItem) -> None:
        """Incrementally insert one source track."""
        self.add_tracks([item])

    def add_tracks(self, items: list[AudioTrackItem]) -> None:
        """Incrementally insert many tracks with one model notification."""
        if not items:
            return

        start_idx = len(self._tracks)
        new_indices = list(range(start_idx, start_idx + len(items)))

        if self._sort_column is not None:
            keyfn = _SORT_KEYS[self._sort_column]
            # One workspace registration for the whole batch, exactly as the
            # unsorted branch does. A per-item call re-emits the workspace
            # signals, and every emission costs the proxy a full pass over
            # each existing row: a 100-file folder became O(items x rows).
            self._workspace.add_tracks(list(items))
            for item in items:
                master_idx = len(self._tracks)
                row = self._sorted_insert_row(item, keyfn)
                self.beginInsertRows(QModelIndex(), row, row)
                self._tracks.append(item)
                self._path_to_idx[item.path] = master_idx
                self._visible.insert(row, master_idx)
                self._reindex_visible(row)
                self.endInsertRows()
                self.operation_counters.inserted_rows += 1
            return

        first_row = len(self._visible)
        last_row = first_row + len(items) - 1
        self.beginInsertRows(QModelIndex(), first_row, last_row)
        self._append_tracks(items, new_indices)
        self._visible.extend(new_indices)
        self._reindex_visible(first_row)
        self.endInsertRows()
        self.operation_counters.inserted_rows += len(items)

    def set_folder_filter(self, folder) -> None:
        """Compatibility no-op; Phase 2 filtering is implemented by the proxy."""

    def get_visible_tracks(self) -> list[AudioTrackItem]:
        return [self._tracks[i] for i in self._visible]

    def track_at_row(self, row: int) -> AudioTrackItem | None:
        """Track shown at visible row ``row``, or None when out of range.

        The supported way for views/delegates to resolve a row — keeps
        ``_visible``/``_tracks`` private to the model.
        """
        if 0 <= row < len(self._visible):
            return self._tracks[self._visible[row]]
        return None

    def visible_count(self) -> int:
        return len(self._visible)

    def total_count(self) -> int:
        return len(self._tracks)

    def get_checked_tracks(self) -> list[AudioTrackItem]:
        """Compatibility alias for the workspace edit scope, never Apply scope."""
        return self._workspace.edit_scope()

    def get_all_tracks(self) -> list[AudioTrackItem]:
        return list(self._tracks)

    def track_for_path(self, path: Path) -> AudioTrackItem | None:
        idx = self._path_to_idx.get(path)
        if idx is None:
            return None
        return self._tracks[idx]

    def refresh_track(self, item: AudioTrackItem) -> None:
        """Emit dataChanged for the row containing item."""
        master_idx = self._path_to_idx.get(item.path)
        if master_idx is None:
            try:
                master_idx = self._tracks.index(item)
            except ValueError:
                return
        self._refresh_or_resort(master_idx)

    def refresh_path(self, path: Path) -> bool:
        """Emit dataChanged for a row by its original path."""
        master_idx = self._path_to_idx.get(path)
        if master_idx is None:
            return False

        item = self._tracks[master_idx]
        if item.path != path:
            self._path_to_idx.pop(path, None)
            self._path_to_idx[item.path] = master_idx

        return self._refresh_or_resort(master_idx)

    def update_file_path(self, item: AudioTrackItem, destination: Path) -> None:
        """Rebase one loaded item after a FileOperationService move/rename."""
        if item not in self._tracks:
            return
        identity = self._workspace.item_id(item)
        self._workspace.relocate_item(identity, destination,
                                      external_state=getattr(item, "external_state", "current"))
        self._path_to_idx = {track.path: i for i, track in enumerate(self._tracks)}
        self._workspace.reconcile()
        self.refresh_track(item)

    def reindex_track_path(self, item: AudioTrackItem, prior_path: Path) -> None:
        """Publish a workspace-owned relocation without mutating it a second time."""
        master_idx = self._path_to_idx.pop(Path(prior_path), None)
        if master_idx is None:
            try:
                master_idx = self._tracks.index(item)
            except ValueError:
                return
        self._path_to_idx[item.path] = master_idx
        self._refresh_or_resort(master_idx)

    def remove_paths(self, paths: list[Path] | set[Path]) -> None:
        """Remove recycled items while retaining stable identity for survivors."""
        path_set = {Path(path) for path in paths}
        if not path_set:
            return
        doomed = [item for item in self._tracks if item.path in path_set]
        if not doomed:
            return
        doomed_ids = [self._workspace.item_id(item) for item in doomed]
        rows = sorted(
            (self._visible_pos[index] for index, item in enumerate(self._tracks)
             if item.path in path_set), reverse=True)
        # Highest row first, so every lower row position stays valid. The
        # master-index rebuild is deferred to one pass after the loop rather
        # than repeated per row, which made a folder removal O(rows x tracks).
        for row in rows:
            master_idx = self._visible[row]
            self.beginRemoveRows(QModelIndex(), row, row)
            self._tracks.pop(master_idx)
            self._shift_visible_after_removal(row, master_idx)
            self.endRemoveRows()
            self.operation_counters.removed_rows += 1
        self._path_to_idx = {track.path: i for i, track in enumerate(self._tracks)}
        self._workspace.remove_item_ids(doomed_ids)

    def set_all_checked(self, checked: bool) -> None:
        """Deprecated compatibility shim; selection is now owned by the view."""

    def get_changed_count(self) -> int:
        return len(self._workspace.changed_tracks())

    @property
    def workspace(self) -> TagEditorWorkspaceState:
        return self._workspace

    def get_edit_scope(self) -> list[AudioTrackItem]:
        return self._workspace.edit_scope()

    def get_apply_candidates(self) -> list[AudioTrackItem]:
        return self._workspace.apply_candidates()

    def get_warning_count(self) -> int:
        return sum(
            1 for t in self._tracks
            if t.status in (TrackStatus.ERROR, TrackStatus.UNSUPPORTED)
        )

    def refresh_all(self) -> None:
        """Force full repaint (after bulk proposed-tag changes)."""
        self._workspace.reconcile()
        if self._visible:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._visible) - 1, COLUMN_COUNT - 1),
            )

    def set_path_checked(self, path: Path, checked: bool) -> None:
        """Deprecated: callers must use MetadataFilterProxyModel visibility APIs."""

    def get_visible_row_for_path(self, path: Path) -> int:
        """Return visible-row index for this path, or -1 if not found/not visible."""
        idx = self._path_to_idx.get(path)
        if idx is None:
            return -1
        return self._visible_pos.get(idx, -1)

    # ── Sorting (Win11 Explorer header-click-to-sort) ─────────────────────────

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        """Sort visible rows by the given column.

        Re-applied automatically inside ``_rebuild_visible`` so that scans,
        edits, and check-state changes preserve the user's chosen order
        (Qt's QTableView with ``setSortingEnabled(True)`` will also call
        this whenever its header indicator changes).
        """
        keyfn = _SORT_KEYS.get(column)
        if keyfn is None:
            return
        persistent = list(self.persistentIndexList())
        persistent_items = [
            (self.track_at_row(index.row()), index.column()) for index in persistent]
        self.layoutAboutToBeChanged.emit()
        self._sort_column = column
        self._sort_order = order
        self._visible.sort(
            key=lambda master_idx: keyfn(self._tracks[master_idx]),
            reverse=(order == Qt.DescendingOrder),
        )
        self._reindex_visible()
        self._restore_persistent_indexes(persistent, persistent_items)
        self.layoutChanged.emit()
        self.operation_counters.layout_changes += 1

    # ── Private ───────────────────────────────────────────────────────────────

    def _append_tracks(self, items: list[AudioTrackItem], indices: list[int]) -> None:
        self._tracks.extend(items)
        self._workspace.add_tracks(items)
        for idx, item in zip(indices, items):
            self._path_to_idx[item.path] = idx

    def _refresh_master_idx(self, master_idx: int) -> bool:
        row = self._visible_pos.get(master_idx)
        if row is None:
            return False

        top_left = self.index(row, 0)
        bot_right = self.index(row, COLUMN_COUNT - 1)
        self.dataChanged.emit(top_left, bot_right)
        self.operation_counters.refreshed_rows += 1
        return True

    def _refresh_or_resort(self, master_idx: int) -> bool:
        if self._sort_column is None:
            return self._refresh_master_idx(master_idx)
        old_row = self._visible_pos.get(master_idx)
        if old_row is None:
            return False
        # Only this row's key can have changed, so locate its new position
        # directly instead of re-sorting (and re-keying) every row: a burst of
        # single-file refreshes otherwise costs one full sort each.
        keyfn = _SORT_KEYS[self._sort_column]
        target = self._resorted_row(old_row, master_idx, keyfn)
        if target == old_row:
            return self._refresh_master_idx(master_idx)
        persistent = list(self.persistentIndexList())
        persistent_items = [
            (self.track_at_row(index.row()), index.column()) for index in persistent]
        self.layoutAboutToBeChanged.emit()
        self._visible.pop(old_row)
        self._visible.insert(target, master_idx)
        self._reindex_visible()
        self._restore_persistent_indexes(persistent, persistent_items)
        self.layoutChanged.emit()
        self.operation_counters.layout_changes += 1
        self.operation_counters.refreshed_rows += 1
        return True

    def _resorted_row(self, old_row: int, master_idx: int, keyfn) -> int:
        """Return the row this item belongs at, ignoring its current position."""
        self._visible.pop(old_row)
        try:
            return self._sorted_insert_row(self._tracks[master_idx], keyfn)
        finally:
            self._visible.insert(old_row, master_idx)

    def _sorted_insert_row(self, item: AudioTrackItem, keyfn) -> int:
        target = keyfn(item)
        low, high = 0, len(self._visible)
        descending = self._sort_order == Qt.DescendingOrder
        while low < high:
            middle = (low + high) // 2
            current = keyfn(self._tracks[self._visible[middle]])
            if (current < target) ^ descending or current == target:
                low = middle + 1
            else:
                high = middle
        return low

    def _restore_persistent_indexes(self, old_indexes, item_columns) -> None:
        if not old_indexes:
            return
        new_indexes = []
        for item, column in item_columns:
            try:
                master_idx = self._tracks.index(item)
            except ValueError:
                new_indexes.append(QModelIndex())
                continue
            row = self._visible_pos.get(master_idx)
            new_indexes.append(
                self.index(row, column) if row is not None else QModelIndex())
        self.changePersistentIndexList(old_indexes, new_indexes)

    def _reindex_visible(self, start: int = 0) -> None:
        if start <= 0:
            self._visible_pos = {}
            start = 0
        for row in range(start, len(self._visible)):
            self._visible_pos[self._visible[row]] = row

    def _shift_visible_after_removal(self, row: int, master_idx: int) -> None:
        """Drop one visible row and rebase the master indexes above it.

        Sort order is unaffected by a removal, so this deliberately avoids the
        re-sort (and its key computation) that a full rebuild would perform.
        """
        self._visible.pop(row)
        self._visible = [index - 1 if index > master_idx else index
                         for index in self._visible]
        self._reindex_visible()

    def _rebuild_visible(self) -> None:
        self._visible = list(range(len(self._tracks)))
        if self._sort_column is not None:
            keyfn = _SORT_KEYS.get(self._sort_column)
            if keyfn is not None:
                self._visible.sort(
                    key=lambda master_idx: keyfn(self._tracks[master_idx]),
                    reverse=(self._sort_order == Qt.DescendingOrder),
                )
        self._reindex_visible()
