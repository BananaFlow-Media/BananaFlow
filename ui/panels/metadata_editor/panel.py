"""
ui/panels/metadata_editor/panel.py  –  Tag Editor Tab
======================================================
Visual structure:
  Top toolbar  — folder picker, scan, auto-arrange, apply, revert, summary
  QSplitter (native drag — panes enforce their own minimum widths):
    Left:   ExplorerTreeWidget — nested folder/file hierarchy with checkboxes
    Centre: ExplorerFileListView — before/after preview (MetadataTableModel)
    Right:  QStackedWidget — context-aware inspector behind a tool rail

Zero direct controller calls — all operations emitted as signals and wired
by AppWindow._connect_metadata_signals().  The Explorer-mimicry widgets live
in explorer_view.py; dialogs in dialogs.py; shared styling in shared.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from config import AppConfig

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
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
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
from qfluentwidgets import FluentIcon

from core.change_sets import ChangeOrigin
from core.filesystem_monitoring import external_state_blocks_apply, is_external_change
from core.tag_actions import ActionResultStatus
from core.metadata_inspector import CapabilityCoverage, MetadataInspectorState, ValueState
from ui.direction import isolate_number
from utils.time_format import display_timestamp
from utils.paths import get_tag_backup_dir
from core.metadata_models import (
    ARTWORK_FIELD,
    AudioTrackItem,
    LyricsEntry,
    ScanResult,
    TrackStatus,
    LYRICS_FIELD,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_FIELDS,
    REPLAYGAIN_REFERENCE_LOUDNESS,
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
)
from ui import a11y
from ui.components.empty_state_icon import EmptyStateIcon
from ui.dialogs.styled_dialog import confirm, get_text, show_info, show_warning
from ui.i18n import t
from ui.models.metadata_table_model import (
    COL_CHECK, COL_FILENAME, COL_TITLE_NEW,
    COL_ARTIST_NEW, COL_ALBUM_NEW,
    COL_TRACK_NEW,
    COL_FILENAME_NEW, COL_GENRE_CUR, COL_GENRE_NEW,
    COL_COMMENT_CUR, COL_COMMENT_NEW,
    COLUMN_COUNT, MetadataTableModel, _HEADER_KEYS,
)
from ui.models.metadata_filter_proxy_model import MetadataFilterProxyModel
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.controllers.tag_editor_navigation_state import TagEditorNavigationState
from ui.services.file_operation_service import FileOperationError, FileOperationService
from ui.theme_manager import get_colors
from ui.workers.artwork_thumbnail_worker import ArtworkThumbnailCache, ArtworkThumbnailWorker

from .dialogs import AutoArrangeSettingsDialog, CleanSettingsDialog, MoreColumnsDialog
from .action_dialog import TagActionDialog
from .action_diagnostics import format_action_diagnostic
from .online_metadata_dialog import OnlineMetadataDialog
from .explorer_view import (
    ExplorerDetailsView,
    ExplorerFileListDelegate,
    ExplorerTableStyle,
    FilenameDelegate,
    MetadataHeaderView,
)
from .shared import (
    DEFAULT_AUTO_OPS,
    DEFAULT_COL_WIDTHS,
    MAGIC_OP_DEFS,
    PAGE_EMPTY,
    PAGE_FOLDER,
    PAGE_TRACKS,
    bold_font,
    btn_style,
    dim_hex,
    op_row_qss,
    primary_btn_style,
)
from .tree import ExplorerTreeWidget
from .widgets import OpRow

logger = logging.getLogger(__name__)

#: Stable FileOperationService codes mapped to localized, path-free messages.
#: The service's English text is a developer diagnostic, not user-facing copy.
_FILE_OPERATION_ERROR_KEYS = {
    "missing": "meta_file_op_missing",
    "destination_exists": "meta_file_op_destination_exists",
    "root_escape": "meta_file_op_root_escape",
    "root_operation": "meta_file_op_root_operation",
    "invalid_name": "meta_file_op_invalid_name",
    "invalid_root": "meta_file_op_invalid_root",
    "cloud_placeholder": "meta_file_op_cloud_placeholder",
    "recursive_move": "meta_file_op_recursive_move",
    "missing_parent": "meta_file_op_missing_parent",
    "not_a_folder": "meta_file_op_not_a_folder",
    "not_a_file": "meta_file_op_not_a_file",
    "unsupported_platform": "meta_file_op_unsupported_platform",
    "rename_failed": "meta_file_op_rename_failed",
    "move_failed": "meta_file_op_move_failed",
    "recycle_failed": "meta_file_op_recycle_failed",
    "create_folder_failed": "meta_file_op_create_folder_failed",
    "properties_failed": "meta_file_op_properties_failed",
}


class ArtworkDropPreview(QLabel):
    """Dedicated, narrow image drop target; never intercepts table drops."""
    def __init__(self, callback, parent=None) -> None:
        super().__init__(parent)
        self._callback = callback
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).suffix.lower() in {".jpg", ".jpeg", ".png"}:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self._callback(Path(urls[0].toLocalFile()))
            event.acceptProposedAction()
        else:
            event.ignore()


class MetadataEditorPanel(QWidget):
    """
    Full Tag Editor tab widget.

    Signals (wired by AppWindow to MetadataController)
    ---------------------------------------------------
    scan_requested(Path, bool)              — folder, recursive
    auto_requested(list)                    — all tracks in current session
    apply_requested(Path, list)             — backup_dir, checked_tracks
    revert_requested(list)                  — all tracks
    restore_requested(list)                 — parsed, user-confirmed backup records
    artist_to_scope(str, list)              — artist text, target tracks
    album_to_scope(str, list)               — album text, checked tracks
    title_from_filename(list, bool)         — tracks, strip_numbering
    track_from_filename(list)               — tracks
    rename_from_title(list)                 — tracks
    clear_comments(list)                    — tracks
    album_artist_from_artist(list)          — tracks
    title_case(list)                        — tracks
    split_artist_title(list)               — tracks
    clear_year(list)                        — tracks
    strip_web_junk(list)                    — tracks
    renumber_sequentially(list)             — tracks (ordered)
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    scan_requested           = Signal(object, bool)
    auto_requested           = Signal(list)
    auto_sequence_requested  = Signal(list, list)  # tracks, stable legacy operation IDs
    apply_requested          = Signal(object, list)     # (backup_dir, checked_tracks)
    revert_requested         = Signal(list)
    undo_requested           = Signal()
    redo_requested           = Signal()
    review_include_requested = Signal(list, bool)       # stable item IDs, excluded
    review_revert_records_requested = Signal(list)      # [(stable item ID, field)]
    review_revert_files_requested = Signal(list)        # stable item IDs
    review_opened            = Signal()
    unsaved_choice_requested = Signal(str)
    restore_requested        = Signal(object)           # {records, backup_path}
    undo_applied_requested   = Signal(object, bool)     # manifest path, explicit physical approval
    draft_restore_requested  = Signal(object)            # validated draft info
    draft_discard_requested  = Signal()
    recover_requested        = Signal(object)           # recovery summary dict
    keep_recovery_requested  = Signal(object)           # 'Not now' — keep journal
    forget_recovery_requested = Signal(object)          # 'Forget' — delete journal
    artist_to_scope          = Signal(str, list)
    album_to_scope           = Signal(str, list)
    title_from_filename      = Signal(list, bool)
    track_from_filename      = Signal(list)
    rename_from_title        = Signal(list)
    clear_comments           = Signal(list)
    album_artist_from_artist = Signal(list)
    split_artist_title       = Signal(list)
    clear_track_num          = Signal(list)
    clear_year               = Signal(list)
    clear_genre              = Signal(list)
    clear_title              = Signal(list)
    clear_artist             = Signal(list)
    clear_album              = Signal(list)
    clear_album_artist       = Signal(list)
    normalize_title_spaces   = Signal(list)
    strip_web_junk           = Signal(list)
    clean_filename           = Signal(list)
    strip_filename_numbering = Signal(list)
    replaygain_track_requested = Signal(list)
    replaygain_album_requested = Signal(list)
    replaygain_cancel_requested = Signal()
    incompatible_disk_action_requested = Signal(str, object)

    # Duplicate file detector signals
    find_duplicates_requested   = Signal(object, bool)  # (Path, recursive)
    delete_duplicates_requested = Signal(list)           # list[Path]
    revalidate_problems_requested = Signal()
    problem_fix_requested = Signal(object, str)
    problem_fix_preview_requested = Signal(object, str)
    problem_fix_accept_requested = Signal(object)
    online_search_requested = Signal(object)
    online_cancel_requested = Signal()
    online_preview_requested = Signal(object)
    online_artwork_requested = Signal(object)
    online_accept_requested = Signal(object)
    delete_files_requested      = Signal(list)           # list[Path] (Delete key)
    manual_refresh_requested    = Signal()
    conflict_resolution_requested = Signal(object, str, object)

    _TREE_RAIL_WIDTH = 38
    _INSPECTOR_RAIL_WIDTH = 40
    _TREE_OPEN_MIN = 210
    _TABLE_OPEN_MIN = 340
    _INSPECTOR_OPEN_MIN = 240
    _COLLAPSE_DRAG_MARGIN = 46

    _DEFAULT_SPLITTER_SIZES = [240, 720, 280]

    def __init__(self, config: Optional[AppConfig] = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("metadataEditorPage")
        self._cfg          = config

        self._workspace    = TagEditorWorkspaceState(self)
        self._inspector_state = MetadataInspectorState()
        self._model        = MetadataTableModel(self, workspace=self._workspace)
        self._proxy        = MetadataFilterProxyModel(self._workspace, self)
        self._proxy.setSourceModel(self._model)
        self._root_folder: Optional[Path] = None
        self._navigation = TagEditorNavigationState()
        self._is_filtering_view = False
        self._file_operations = FileOperationService(self._root_folder)
        # Every physical mutation of a loaded path belongs to the controller,
        # which is the only owner able to pause monitoring, register the
        # evidence own-operation suppression needs, and reconcile afterwards.
        # Read-only inspection (open/reveal/copy path/properties) stays local.
        self._file_operation_owner = None
        self._tag_action_preview_acceptor = None
        self._metadata_io_callbacks: dict[str, object] = {}
        self._metadata_io_dialog = None
        self._icon_provider = QFileIconProvider()
        self._audio_icon    = self._make_audio_icon()
        self._track_icon_cache: dict[str, QIcon] = {}
        self._artwork_thumbnail_cache = ArtworkThumbnailCache()
        self._artwork_thumbnail_workers: set[ArtworkThumbnailWorker] = set()
        self._artwork_thumbnail_generation = 0
        self._artwork_thumbnail_tokens: dict[str, tuple] = {}

        # Auto-arrange configurable ops
        if self._cfg:
            self._auto_ops = set(self._cfg.magic_auto_ops)
            self._zoom_level = self._cfg.tag_editor_zoom
        else:
            self._auto_ops = set(DEFAULT_AUTO_OPS)
            self._zoom_level = 100

        # Tree item lookup maps
        self._folder_items:       dict[Path, QTreeWidgetItem] = {}
        self._file_items:         dict[Path, QTreeWidgetItem] = {}
        self._ignore_tree_changes = False
        self._ignore_header_resize = True
        self._ignore_splitter_save = False
        self._splitter_drag: Optional[dict[str, object]] = None

        # One debounced config write for all rapid-fire geometry changes
        # (splitter drags, column drags) instead of one disk write per pixel.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(180)
        self._geometry_save_timer.timeout.connect(self._flush_geometry_save)
        self._left_collapsed = False
        self._right_collapsed = False
        self._last_tree_width = self._DEFAULT_SPLITTER_SIZES[0]
        self._last_inspector_width = self._DEFAULT_SPLITTER_SIZES[2]
        self._active_inspector_tool = 0
        self._apply_refresh_counter = 0
        self._is_scanning = False
        self._is_applying = False
        self._is_restoring = False
        self._op_rows: list[OpRow] = []
        self._checked_scope_buttons: list[QPushButton] = []
        self._selection_scope_buttons: list[QPushButton] = []
        self._insp_field_dirty: set[str] = set()
        self._insp_draft_item_ids: tuple[int, ...] | None = None
        self._insp_draft_values: dict[str, str] = {}
        self._insp_populating = False
        self._replaygain_analysis_running = False

        self._build()
        self._workspace.changed.connect(self._on_workspace_state_changed)

    def set_workspace_state(self, workspace: TagEditorWorkspaceState) -> None:
        """Bind to the controller-owned Phase 2 state before any scan starts."""
        if workspace is self._workspace:
            return
        self._discard_inspector_draft()
        try:
            self._workspace.changed.disconnect(self._on_workspace_state_changed)
        except (RuntimeError, TypeError):
            pass
        self._workspace = workspace
        self._model = MetadataTableModel(self, workspace=workspace)
        self._proxy = MetadataFilterProxyModel(workspace, self)
        self._proxy.setSourceModel(self._model)
        self._table.setModel(self._proxy)
        self._table.selectionModel().selectionChanged.connect(self._on_table_selection_changed)
        self._model.dataChanged.connect(self._on_model_data_changed)
        self._model.rowsInserted.connect(lambda *_: self._refresh_checked_scope_state())
        self._model.rowsRemoved.connect(lambda *_: self._refresh_checked_scope_state())
        self._model.modelReset.connect(self._refresh_checked_scope_state)
        self._workspace.changed.connect(self._on_workspace_state_changed)

    def incremental_workspace_updater(self):
        from ui.controllers.incremental_workspace_updater import IncrementalWorkspaceUpdater
        return IncrementalWorkspaceUpdater(
            self._workspace, self._model, self._navigation)

    def set_file_operation_owner(self, owner) -> None:
        """Install the controller that owns every physical path mutation."""
        self._file_operation_owner = owner

    def _run_file_operation(self, operation: str, *args):
        """Delegate one mutation to the controller and report its failures.

        Returns ``None`` when no owner is installed or the controller refused
        the operation, so a caller never mistakes a refusal for a success.
        """
        owner = self._file_operation_owner
        if owner is None:
            # A missing owner is a wiring defect, never a user error: refuse the
            # mutation rather than performing an unmonitored one, and do not
            # block the UI with a dialog the user cannot act on.
            logger.error("[MetadataEditorPanel] No file-operation owner for %s", operation)
            return None
        result = getattr(owner, operation)(*args)
        if result is None:
            return None
        if result.failed:
            show_warning(self, t("meta_error_title"),
                         "\n".join(self._file_operation_message(outcome)
                                   for outcome in result.failed))
        return result

    @staticmethod
    def _file_operation_message(outcome) -> str:
        """Localize a failure by its stable code.

        The service's own message is an untranslated developer string that
        often embeds an absolute path (or a raw OSError), neither of which
        belongs in front of an ordinary user.  The code is the stable contract;
        the file's name is enough to identify it.
        """
        key = _FILE_OPERATION_ERROR_KEYS.get(
            getattr(outcome, "error_code", ""), "meta_file_op_failed")
        return t(key, name=Path(outcome.source).name)

    def set_tag_action_preview_acceptor(self, acceptor) -> None:
        """Install the controller-owned Phase 9 proposal acceptance lifecycle."""
        self._tag_action_preview_acceptor = acceptor

    def set_metadata_io_callbacks(self, callbacks: dict[str, object]) -> None:
        """Install the controller-owned Phase 12 IO lifecycle."""
        self._metadata_io_callbacks = dict(callbacks)

    def _accept_tag_action_preview(self, preview) -> bool:
        """Accept only through the controller lifecycle installed by AppWindow."""
        if self._tag_action_preview_acceptor is None:
            return False
        return bool(self._tag_action_preview_acceptor(preview))

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_toolbar())

        self._toolbar_sep = QFrame()
        self._toolbar_sep.setFrameShape(QFrame.Shape.HLine)
        self._toolbar_sep.setFixedHeight(1)
        self._toolbar_sep.setStyleSheet(f"background: {get_colors().border}; border: none;")
        root_layout.addWidget(self._toolbar_sep)

        root_layout.addWidget(self._build_body(), stretch=1)

        from ui.theme_manager import ThemeManager as _TM
        _tm = _TM.instance()
        if _tm is not None:
            _tm.theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Re-apply theme-dependent styles for the toolbar, tree, table, buttons."""
        c = get_colors()
        accent = c.accent
        accent_dim = dim_hex(accent)
        self.setStyleSheet(
            "QWidget#metadataEditorPage { border: none; border-radius: 0px; }"
            "QWidget#metadataEditorPage QFrame,"
            "QWidget#metadataEditorPage QGroupBox,"
            "QWidget#metadataEditorPage QLineEdit,"
            "QWidget#metadataEditorPage QTableView,"
            "QWidget#metadataEditorPage QTreeWidget,"
            "QWidget#metadataEditorPage QScrollArea { border-radius: 0px; }"
            "QWidget#metadataEditorPage QPushButton { border-radius: 8px; }"
            "QWidget#metadataEditorPage QTreeWidget::item { border-radius: 0px; }"
            "QWidget#metadataEditorPage QCheckBox::indicator { border-radius: 0px; }"
        )

        # Toolbar
        if hasattr(self, "_toolbar_bar"):
            self._toolbar_bar.setStyleSheet(
                f"QFrame {{ background: {c.surface}; border-bottom: 1px solid {c.border}; border-radius: 0px; }}"
            )
        if hasattr(self, "_toolbar_sep"):
            self._toolbar_sep.setStyleSheet(f"background: {c.border}; border: none;")
        if hasattr(self, "_summary_lbl"):
            self._summary_lbl.setStyleSheet(f"color: {c.text_secondary}; font-size: 11px;")

        accent_color = QColor(accent)
        if accent_color.isValid():
            r, g, b, _ = accent_color.getRgb()
            accent_hover = accent_color.darker(110).name()
            accent_soft = f"rgba({r}, {g}, {b}, 0.16)"
            accent_magic_2 = f"rgba({r}, {g}, {b}, 0.28)"
            accent_glow_1 = f"rgba({r}, {g}, {b}, 0.12)"
            accent_glow_2 = f"rgba({r}, {g}, {b}, 0.18)"
            accent_glow_3 = f"rgba({r}, {g}, {b}, 0.42)"
            accent_glow_hover_1 = f"rgba({r}, {g}, {b}, 0.16)"
            accent_glow_hover_2 = f"rgba({r}, {g}, {b}, 0.28)"
            accent_glow_hover_3 = f"rgba({r}, {g}, {b}, 0.58)"
            accent_border = f"rgba({r}, {g}, {b}, 0.58)"
            primary_text = "#ffffff" if accent_color.lightness() < 170 else "#111827"
        else:
            r, g, b = 0, 0, 0
            accent_hover = accent_dim
            accent_soft = c.surface2
            accent_magic_2 = c.surface2
            accent_glow_1 = c.surface
            accent_glow_2 = c.surface2
            accent_glow_3 = c.border
            accent_glow_hover_1 = c.surface2
            accent_glow_hover_2 = c.border
            accent_glow_hover_3 = c.border
            accent_border = c.border
            primary_text = "#ffffff"

        toolbar_neutral = self._toolbar_button_style("neutral")
        for attr in ("_revert_btn", "_restore_btn", "_io_btn"):
            if hasattr(self, attr):
                getattr(self, attr).setStyleSheet(toolbar_neutral)
        if hasattr(self, "_backup_manager_btn"):
            self._backup_manager_btn.setStyleSheet(toolbar_neutral)
        self._refresh_toolbar_action_styles()

        auto_text = accent if accent_color.isValid() else c.text_primary
        if hasattr(self, "_auto_container"):
            self._auto_container.setStyleSheet(
                "QFrame#autoOrderSplitButton {"
                f" background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                f" stop:0 {accent_glow_1}, stop:0.46 {accent_glow_2}, stop:1 {accent_glow_3});"
                f" border: 1px solid {accent_border}; border-radius: 7px;"
                "}"
                "QFrame#autoOrderSplitButton:hover {"
                f" background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                f" stop:0 {accent_glow_hover_1}, stop:0.46 {accent_glow_hover_2}, stop:1 {accent_glow_hover_3});"
                "}"
                "QFrame#autoOrderSplitButton:disabled {"
                f" background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                f" stop:0 rgba({r}, {g}, {b}, 0.05), stop:0.46 rgba({r}, {g}, {b}, 0.08), stop:1 rgba({r}, {g}, {b}, 0.14));"
                f" border: 1px solid rgba({r}, {g}, {b}, 0.24); border-radius: 7px;"
                "}"
            )
        if hasattr(self, "_auto_separator"):
            self._auto_separator.setStyleSheet(f"background: {accent_border}; border: none;")
        if hasattr(self, "_auto_btn") and hasattr(self, "_auto_cfg_btn"):
            self._auto_btn.setIcon(self._make_magic_wand_icon(color=auto_text))
            self._auto_cfg_btn.setIcon(FluentIcon.SETTING.icon(color=auto_text))
            self._auto_btn.setStyleSheet(
                f"QToolButton {{ background: transparent; border: none; color: {auto_text};"
                " font-size: 12px; font-weight: 700; padding: 3px 6px 4px 6px; }"
                f"QToolButton:hover {{ background: {accent_soft}; }}"
                f"QToolButton:pressed {{ background: {accent_magic_2}; }}"
                f"QToolButton:disabled {{ color: {auto_text}; }}"
            )
            self._auto_cfg_btn.setStyleSheet(
                f"QToolButton {{ background: transparent; border: none; color: {auto_text}; padding: 0; }}"
                f"QToolButton:hover {{ background: {accent_soft}; }}"
                f"QToolButton:pressed {{ background: {accent_magic_2}; }}"
            )
        if hasattr(self, "_dupes_btn"):
            self._dupes_btn.setStyleSheet(btn_style())

        # Splitter handle
        if hasattr(self, "_body_splitter"):
            self._body_splitter.setStyleSheet(
                f"QSplitter::handle {{ background: {c.border}; }}"
                f"QSplitter::handle:hover {{ background: {accent}; }}"
                f"QSplitter::handle:pressed {{ background: {accent_dim}; }}"
            )
        if hasattr(self, "_tree_rail"):
            rail_qss = f"QFrame {{ background: {c.surface}; border-right: 1px solid {c.border}; border-radius: 0px; }}"
            self._tree_rail.setStyleSheet(rail_qss)
        if hasattr(self, "_tree_frame"):
            self._tree_frame.setStyleSheet(
                f"QFrame {{ background: {c.bg}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_tree_body"):
            self._tree_body.setStyleSheet(
                f"QFrame {{ background: {c.bg}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_table_frame"):
            self._table_frame.setStyleSheet(
                f"QFrame {{ background: {c.bg}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_table_empty_page"):
            empty_qss = f"QWidget {{ background: {c.bg}; color: {c.text_primary}; }}"
            self._table_empty_page.setStyleSheet(empty_qss)
            self._table_loading_page.setStyleSheet(empty_qss)
        if hasattr(self, "_empty_state_card"):
            card_qss = (
                "QFrame { background: transparent; border: none; border-radius: 0px; }"
                "QLabel { background: transparent; border: none; }"
            )
            self._empty_state_card.setStyleSheet(card_qss)
            self._loading_state_card.setStyleSheet(card_qss)
            self._empty_title_lbl.setStyleSheet(f"color: {c.text_primary}; font-size: 15px; font-weight: bold;")
            self._loading_title_lbl.setStyleSheet(f"color: {c.text_primary}; font-size: 15px; font-weight: bold;")
            self._empty_body_lbl.setStyleSheet(f"color: {c.text_secondary}; font-size: 13px;")
            self._loading_detail_lbl.setStyleSheet(f"color: {c.text_secondary}; font-size: 13px;")
        if hasattr(self, "_center_progress"):
            self._center_progress.setStyleSheet(
                f"QProgressBar {{ background: {c.surface2}; border: none; border-radius: 4px; }}"
                f"QProgressBar::chunk {{ background: {c.accent}; border-radius: 4px; }}"
            )
        if hasattr(self, "_inspector_rail"):
            self._inspector_rail.setStyleSheet(
                f"QFrame {{ background: {c.surface}; border-left: 1px solid {c.border}; border-radius: 0px; }}"
            )
        if hasattr(self, "_inspector_shell"):
            self._inspector_shell.setStyleSheet(
                f"QFrame {{ background: {c.bg}; border-left: 1px solid {c.border}; border-radius: 0px; }}"
            )
        if hasattr(self, "_inspector_content"):
            self._inspector_content.setStyleSheet(
                f"QFrame {{ background: {c.bg}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_inspector_header"):
            self._inspector_header.setStyleSheet(
                f"QFrame {{ background: {c.surface}; border-bottom: 1px solid {c.border}; border-radius: 0px; }}"
            )
        self._refresh_tool_button_states()

        # Tree widget
        if hasattr(self, "_tree"):
            self._tree.setStyleSheet(
                f"QTreeWidget {{ border: none; border-radius: 0; background: {c.bg}; color: {c.text_primary}; }}"
                f"QTreeWidget::viewport {{ background: {c.bg}; }}"
                f"QTreeWidget::item {{ padding: 4px 3px; border-radius: 0px; }}"
                f"QTreeWidget::item:selected {{ background: {accent}33; color: {c.text_primary}; }}"
                f"QTreeWidget::item:hover {{ background: {c.surface2}; }}"
            )
            tree_pal = self._tree.viewport().palette()
            tree_pal.setColor(QPalette.Base, QColor(c.bg))
            tree_pal.setColor(QPalette.Window, QColor(c.bg))
            self._tree.viewport().setPalette(tree_pal)
            self._tree.viewport().setAutoFillBackground(True)

        # Zoom controls
        zoom_btn_qss = (
            f"QPushButton {{ background: {c.surface2}; color: {c.text_primary};"
            f"  border: 1px solid {c.border}; border-radius: 6px; padding: 0px;"
            f"  font-weight: bold; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {c.border}; }}"
        )
        if hasattr(self, "_zoom_minus_btn"):
            self._zoom_minus_btn.setStyleSheet(zoom_btn_qss)
            self._zoom_minus_btn.setIcon(FluentIcon.ZOOM_OUT.icon(color=c.text_primary))
        if hasattr(self, "_zoom_plus_btn"):
            self._zoom_plus_btn.setStyleSheet(zoom_btn_qss)
            self._zoom_plus_btn.setIcon(FluentIcon.ZOOM_IN.icon(color=c.text_primary))
        if hasattr(self, "_zoom_val_lbl"):
            self._zoom_val_lbl.setStyleSheet(
                f"QLineEdit {{ background: {c.surface}; color: {c.text_primary};"
                f"  border: 1px solid {c.border}; border-radius: 0px;"
                f"  font-size: 11px; padding: 0px 2px; }}"
            )
        if hasattr(self, "_table_info_lbl"):
            self._table_info_lbl.setStyleSheet(
                f"color: {c.text_secondary}; font-size: 11px;"
            )

        # Re-apply zoom (rebuilds the table stylesheet with current theme colors)
        if hasattr(self, "_zoom_level") and hasattr(self, "_table"):
            try:
                self._set_zoom(self._zoom_level)
            except Exception:
                pass

        # Also explicitly paint the table viewport so the empty area follows theme
        if hasattr(self, "_table"):
            tc = get_colors()
            self._table.viewport().setStyleSheet(f"background: {tc.bg};")
            pal = self._table.viewport().palette()
            pal.setColor(QPalette.Base, QColor(tc.bg))
            pal.setColor(QPalette.Window, QColor(tc.bg))
            self._table.viewport().setPalette(pal)
            self._table.viewport().setAutoFillBackground(True)
            self._table.viewport().update()

        # Inspector action rows
        self._refresh_op_rows_style()
        primary_qss = primary_btn_style()
        for btn in getattr(self, "_checked_scope_buttons", []):
            btn.setStyleSheet(primary_qss)
        for btn in getattr(self, "_selection_scope_buttons", []):
            btn.setStyleSheet(primary_qss if btn.property("accentRole") == "primary" else btn_style())
        if hasattr(self, "_table_info_lbl"):
            self._refresh_checked_scope_state()
            self._refresh_selection_scope_state()

    def _toolbar_button_style(self, role: str) -> str:
        c = get_colors()
        accent_dim = dim_hex(c.accent)
        accent_color = QColor(c.accent)
        primary_text = "#ffffff" if not accent_color.isValid() or accent_color.lightness() < 170 else "#111827"

        if role == "primary":
            return (
                f"QToolButton {{ background: {c.accent}; color: {primary_text};"
                f"  border: 1px solid {c.accent}; border-radius: 8px;"
                f"  padding: 5px 8px; font-weight: 800; }}"
                f"QToolButton:hover {{ background: {accent_dim}; border-color: {accent_dim}; }}"
                f"QToolButton:pressed {{ background: {accent_dim}; border-color: {accent_dim}; }}"
                f"QToolButton:disabled {{ background: {c.bg}; color: {c.text_tertiary};"
                f"  border: 1px solid {c.border}; }}"
            )

        if role == "secondary":
            if accent_color.isValid():
                r, g, b, _ = accent_color.getRgb()
                bg = f"rgba({r}, {g}, {b}, 0.09)"
                hover_bg = f"rgba({r}, {g}, {b}, 0.15)"
            else:
                bg = c.surface2
                hover_bg = c.border
            return (
                f"QToolButton {{ background: {bg}; color: {c.accent};"
                f"  border: 1px solid transparent; border-radius: 8px;"
                f"  padding: 5px 8px; font-weight: 700; }}"
                f"QToolButton:hover {{ background: {hover_bg}; border-color: {c.accent}; }}"
                f"QToolButton:pressed {{ background: {hover_bg}; }}"
                f"QToolButton:disabled {{ background: {c.bg}; color: {c.text_tertiary};"
                f"  border: 1px solid {c.border}; }}"
            )

        return (
            f"QToolButton {{ background: {c.surface}; color: {c.text_primary};"
            f"  border: 1px solid {c.border}; border-radius: 8px;"
            f"  padding: 5px 8px; font-weight: 600; }}"
            f"QToolButton:hover {{ background: {c.surface2}; border-color: {c.accent}; }}"
            f"QToolButton:disabled {{ background: {c.surface}; color: {c.text_tertiary};"
            f"  border-color: {c.border}; }}"
        )

    def _toolbar_icon_color(self, role: str, *, enabled: bool = True) -> str:
        c = get_colors()
        if not enabled:
            return c.text_tertiary
        if role == "primary":
            accent_color = QColor(c.accent)
            return "#ffffff" if not accent_color.isValid() or accent_color.lightness() < 170 else "#111827"
        if role == "secondary":
            return c.accent
        return c.text_primary

    def _refresh_toolbar_action_styles(self) -> None:
        if hasattr(self, "_browse_btn"):
            browse_role = "secondary" if self._root_folder else "primary"
            self._browse_btn.setStyleSheet(self._toolbar_button_style(browse_role))
            self._browse_btn.setIcon(FluentIcon.FOLDER.icon(color=self._toolbar_icon_color(browse_role)))

        if hasattr(self, "_apply_btn"):
            apply_enabled = self._apply_btn.isEnabled()
            apply_role = "primary" if apply_enabled else "neutral"
            self._apply_btn.setStyleSheet(self._toolbar_button_style(apply_role))
            self._apply_btn.setIcon(FluentIcon.SAVE.icon(
                color=self._toolbar_icon_color(apply_role, enabled=apply_enabled)
            ))

    def _toolbar_text(self, key: str) -> str:
        return t(key).strip()

    def _make_sparkle_icon(self, color: str = "#000000", size: int = 20) -> QIcon:
        """Draw a sparkle / 4-pointed star icon using QPainter — no SVG file needed."""
        import math
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF

        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))

        def star_polygon(cx, cy, r_out, r_in, points_count=4):
            pts = []
            for i in range(points_count * 2):
                angle = math.pi / points_count * i - math.pi / 2
                r = r_out if i % 2 == 0 else r_in
                pts.append(QPointF(cx + r * math.cos(angle), cy + r * math.sin(angle)))
            return QPolygonF(pts)

        # Large central 4-pointed star
        cx, cy = size / 2.0, size / 2.0
        painter.drawPolygon(star_polygon(cx, cy, size * 0.46, size * 0.11))

        # Small top-right sparkle
        painter.setOpacity(0.85)
        painter.drawPolygon(star_polygon(size * 0.80, size * 0.20, size * 0.15, size * 0.04))

        # Tiny bottom-left sparkle
        painter.setOpacity(0.65)
        painter.drawPolygon(star_polygon(size * 0.22, size * 0.78, size * 0.10, size * 0.03))

        painter.end()
        return QIcon(pixmap)

    def _make_magic_wand_icon(self, color: str = "#000000", size: int = 22) -> QIcon:
        from PySide6.QtCore import QPointF

        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(QColor(color), max(2, size // 9))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(int(size * 0.34), int(size * 0.78), int(size * 0.76), int(size * 0.24))

        sparkle_pen = QPen(QColor(color), max(1, size // 13))
        sparkle_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(sparkle_pen)
        for cx, cy, r in (
            (size * 0.24, size * 0.26, size * 0.10),
            (size * 0.55, size * 0.12, size * 0.08),
            (size * 0.80, size * 0.62, size * 0.07),
        ):
            painter.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
            painter.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))

        painter.end()
        return QIcon(pixmap)

    def _make_toolbar_action(self, text_key: str, icon, handler, *, enabled: bool = True) -> QToolButton:
        btn = QToolButton()
        btn.setText(self._toolbar_text(text_key))
        btn.setIcon(icon.icon())
        btn.setIconSize(QSize(20, 20))
        btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        # A minimum rather than a fixed size: the label is translated and must
        # be free to grow for a long Hebrew string or a 200% scale factor —
        # Qt's own High-DPI scaling already maps this logical size to the
        # physical screen, so it is not multiplied by DPI here.
        btn.setMinimumSize(92, 46)
        btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setEnabled(enabled)
        # The visible label is wrapped for the toolbar; the accessible name is
        # the unwrapped localized string a screen reader should announce.
        a11y.describe(btn, t(text_key))
        btn.clicked.connect(handler)
        return btn

    def _make_auto_toolbar_action(self) -> QWidget:
        container = QFrame()
        container.setObjectName("autoOrderSplitButton")
        container.setFixedSize(136, 46)
        self._auto_container = container

        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._auto_cfg_btn = QToolButton()
        self._auto_cfg_btn.setIcon(FluentIcon.SETTING.icon(color=get_colors().text_primary))
        self._auto_cfg_btn.setIconSize(QSize(20, 20))
        a11y.describe(self._auto_cfg_btn, t("meta_auto_cfg_tooltip"),
                      tooltip=t("meta_auto_cfg_tooltip"))
        self._auto_cfg_btn.setFixedSize(36, 46)
        self._auto_cfg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_cfg_btn.setEnabled(False)
        self._auto_cfg_btn.clicked.connect(self._on_auto_arrange_settings)

        self._auto_separator = QFrame()
        self._auto_separator.setFixedSize(2, 30)

        self._auto_btn = QToolButton()
        self._auto_btn.setText(self._toolbar_text("meta_auto_btn"))
        self._auto_btn.setIcon(self._make_magic_wand_icon(color=get_colors().text_primary))
        self._auto_btn.setIconSize(QSize(21, 21))
        self._auto_btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self._auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_btn.setEnabled(False)
        self._auto_btn.clicked.connect(self._on_auto_arrange)
        self._auto_btn.setFixedSize(99, 46)

        layout.addWidget(self._auto_cfg_btn)
        layout.addWidget(self._auto_separator, alignment=Qt.AlignVCenter)
        layout.addWidget(self._auto_btn)

        return container

    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(64)
        bar.setLayoutDirection(Qt.LeftToRight)
        self._toolbar_bar = bar

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self._io_btn = self._make_toolbar_action(
            "meta_io_toolbar", FluentIcon.DOCUMENT, self._on_metadata_io
        )
        self._io_btn.setToolTip(t("meta_io_subtitle"))
        self._io_btn.setAccessibleName(t("meta_io_title"))
        layout.addWidget(self._io_btn)

        self._restore_btn = self._make_toolbar_action(
            "meta_restore_btn", FluentIcon.HISTORY, self._on_restore_from_backup
        )
        self._restore_btn.setToolTip(t("meta_restore_tooltip"))
        layout.addWidget(self._restore_btn)

        self._backup_manager_btn = self._make_toolbar_action(
            "meta_backup_manager", FluentIcon.FOLDER, self._on_backup_manager
        )
        layout.addWidget(self._backup_manager_btn)

        self._undo_btn = self._make_toolbar_action(
            "meta_undo_changes", FluentIcon.RETURN, self.undo_requested.emit, enabled=False
        )
        self._undo_btn.setShortcut("Ctrl+Z")
        layout.addWidget(self._undo_btn)
        self._redo_btn = self._make_toolbar_action(
            "meta_redo_changes", FluentIcon.SYNC, self.redo_requested.emit, enabled=False
        )
        self._redo_btn.setShortcut("Ctrl+Y")
        layout.addWidget(self._redo_btn)

        self._review_btn = self._make_toolbar_action(
            "meta_review_changes", FluentIcon.VIEW, self._on_review_changes, enabled=False
        )
        self._review_btn.setShortcut("Ctrl+Shift+R")
        layout.addWidget(self._review_btn)

        self._revert_btn = self._make_toolbar_action(
            "meta_revert_changes", FluentIcon.LEFT_ARROW, self._on_revert, enabled=False
        )
        layout.addWidget(self._revert_btn)

        self._apply_btn = self._make_toolbar_action(
            "meta_apply_changes", FluentIcon.SAVE, self._on_apply, enabled=False
        )
        layout.addWidget(self._apply_btn)

        layout.addStretch()

        self._monitoring_status = QLabel(t("meta_monitoring_disabled"))
        self._monitoring_status.setAccessibleName(t("meta_monitoring_status"))
        self._monitoring_status.setToolTip(t("meta_monitoring_status_tooltip"))
        layout.addWidget(self._monitoring_status)
        self._manual_refresh_btn = self._make_toolbar_action(
            "meta_manual_refresh", FluentIcon.SYNC,
            self.manual_refresh_requested.emit, enabled=False)
        self._manual_refresh_btn.setAccessibleName(t("meta_manual_refresh"))
        layout.addWidget(self._manual_refresh_btn)

        layout.addWidget(self._make_auto_toolbar_action())

        self._browse_btn = self._make_toolbar_action(
            "meta_browse_folder", FluentIcon.FOLDER, self._on_browse
        )
        layout.addWidget(self._browse_btn)

        self._scan_progress = QProgressBar()
        self._scan_progress.setFixedWidth(150)
        self._scan_progress.setFixedHeight(8)
        # The bar is a thin decoration with no visible text, so its meaning has
        # to reach assistive technology through its name and description; the
        # summary label carries the same information visually.
        self._scan_progress.setTextVisible(False)
        a11y.describe(self._scan_progress, t("meta_a11y_scan_progress"),
                      description=t("meta_scanning"))
        self._scan_progress.setVisible(False)
        layout.addWidget(self._scan_progress)

        self._summary_lbl = QLabel("")
        self._summary_lbl.setVisible(False)

        return bar

    def _build_state_card(self, page: QWidget) -> tuple[QFrame, QVBoxLayout]:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("metadataStateCard")
        card.setMaximumWidth(430)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(12)
        outer.addWidget(card, alignment=Qt.AlignCenter)
        return card, layout

    def _build_table_empty_page(self) -> QWidget:
        page = QWidget()
        card, layout = self._build_state_card(page)
        self._empty_state_card = card

        layout.addWidget(EmptyStateIcon("tag", card), alignment=Qt.AlignCenter)

        title = QLabel(t("meta_empty_title"))
        title.setAlignment(Qt.AlignCenter)
        self._empty_title_lbl = title
        layout.addWidget(title)

        body = QLabel(t("meta_empty_body"))
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setMaximumWidth(330)
        self._empty_body_lbl = body
        layout.addWidget(body)

        return page

    def _build_table_loading_page(self) -> QWidget:
        page = QWidget()
        card, layout = self._build_state_card(page)
        self._loading_state_card = card

        layout.addWidget(EmptyStateIcon("sync", card), alignment=Qt.AlignCenter)

        self._loading_title_lbl = QLabel(t("meta_loading_scanning_title"))
        self._loading_title_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._loading_title_lbl)

        self._loading_detail_lbl = QLabel(t("meta_loading_scanning_body"))
        self._loading_detail_lbl.setAlignment(Qt.AlignCenter)
        self._loading_detail_lbl.setWordWrap(True)
        self._loading_detail_lbl.setMaximumWidth(330)
        layout.addWidget(self._loading_detail_lbl)
        layout.addSpacing(8)

        self._center_progress = QProgressBar()
        self._center_progress.setTextVisible(False)
        self._center_progress.setFixedSize(320, 8)
        layout.addWidget(self._center_progress, alignment=Qt.AlignCenter)

        return page

    def _show_table_content(self) -> None:
        if hasattr(self, "_table_stack"):
            self._table_stack.setCurrentWidget(self._table_content)

    def _show_table_empty(self) -> None:
        if hasattr(self, "_table_stack"):
            self._table_stack.setCurrentWidget(self._table_empty_page)

    def _show_table_loading(self, title: str, body: str, *, indeterminate: bool = True) -> None:
        if hasattr(self, "_loading_title_lbl"):
            self._loading_title_lbl.setText(title)
        if hasattr(self, "_loading_detail_lbl"):
            self._loading_detail_lbl.setText(body)
        if hasattr(self, "_center_progress"):
            if indeterminate:
                self._center_progress.setRange(0, 0)
            else:
                self._center_progress.setRange(0, 1)
                self._center_progress.setValue(0)
        if hasattr(self, "_table_stack"):
            self._table_stack.setCurrentWidget(self._table_loading_page)

    def _build_body(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setLayoutDirection(QApplication.layoutDirection())
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(6)
        self._body_splitter = splitter

        # ── Left: folder/file tree ────────────────────────────────────────────
        tree_frame = QFrame()
        tree_frame.setMinimumWidth(self._TREE_RAIL_WIDTH)
        self._tree_frame = tree_frame
        tree_shell_layout = QHBoxLayout(tree_frame)
        tree_shell_layout.setContentsMargins(0, 0, 0, 0)
        tree_shell_layout.setSpacing(0)

        self._tree_rail = QFrame()
        self._tree_rail.setFixedWidth(self._TREE_RAIL_WIDTH)
        tree_rail_layout = QVBoxLayout(self._tree_rail)
        tree_rail_layout.setContentsMargins(4, 6, 4, 6)
        tree_rail_layout.setSpacing(6)
        self._tree_toggle_btn = QPushButton()
        self._tree_toggle_btn.setFixedSize(28, 28)
        self._set_tool_button_icon(self._tree_toggle_btn, FluentIcon.FOLDER)
        a11y.describe(self._tree_toggle_btn, t("meta_files_folders_header"),
                      tooltip=t("meta_files_folders_header"))
        self._tree_toggle_btn.clicked.connect(self._toggle_tree_pane)
        tree_rail_layout.addWidget(self._tree_toggle_btn)
        tree_rail_layout.addStretch()
        tree_shell_layout.addWidget(self._tree_rail)

        self._tree_body = QFrame()
        self._tree_body.setMinimumWidth(self._TREE_OPEN_MIN)
        tree_layout = QVBoxLayout(self._tree_body)
        tree_layout.setContentsMargins(4, 4, 0, 4)
        tree_layout.setSpacing(4)

        tree_header = QLabel(t("meta_files_folders_header"))
        tree_header.setStyleSheet("font-weight: bold; font-size: 12px; padding: 2px 0;")
        tree_header_row = QHBoxLayout()
        tree_header_row.setContentsMargins(0, 0, 4, 0)
        tree_header_row.setSpacing(4)
        tree_header_row.addWidget(tree_header)
        tree_header_row.addStretch()
        tree_layout.addLayout(tree_header_row)

        self._tree = ExplorerTreeWidget()
        self._tree.setAccessibleName(t("meta_a11y_file_tree"))
        self._tree.setAccessibleDescription(t("meta_a11y_file_tree_desc"))
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setAnimated(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        self._tree.itemClicked.connect(self._on_tree_navigation_item_clicked)
        self._tree.item_moved.connect(self._on_tree_item_moved)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.keyboardContextMenuRequested.connect(self._on_tree_context_menu)
        tree_layout.addWidget(self._tree)
        tree_shell_layout.addWidget(self._tree_body, stretch=1)

        splitter.addWidget(tree_frame)
        splitter.setStretchFactor(0, 0)

        # ── Centre: table ─────────────────────────────────────────────────────
        table_frame = QFrame()
        self._table_frame = table_frame
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(0, 4, 0, 4)
        table_layout.setSpacing(4)

        self._table_stack = QStackedWidget()
        self._table_content = QWidget()
        table_content_layout = QVBoxLayout(self._table_content)
        table_content_layout.setContentsMargins(0, 0, 0, 0)
        table_content_layout.setSpacing(4)

        nav_bar = QHBoxLayout()
        nav_bar.setContentsMargins(12, 0, 12, 0)
        nav_bar.setSpacing(4)
        self._nav_back_btn = QToolButton()
        self._nav_back_btn.setToolTip(t("meta_nav_back"))
        self._nav_back_btn.setAccessibleName(t("meta_nav_back"))
        self._nav_back_btn.clicked.connect(self._on_navigate_back)
        nav_bar.addWidget(self._nav_back_btn)
        self._nav_forward_btn = QToolButton()
        self._nav_forward_btn.setToolTip(t("meta_nav_forward"))
        self._nav_forward_btn.setAccessibleName(t("meta_nav_forward"))
        self._nav_forward_btn.clicked.connect(self._on_navigate_forward)
        nav_bar.addWidget(self._nav_forward_btn)
        self._nav_up_btn = QToolButton()
        self._nav_up_btn.setText("↑")
        self._nav_up_btn.setToolTip(t("meta_nav_up"))
        self._nav_up_btn.setAccessibleName(t("meta_nav_up"))
        self._nav_up_btn.clicked.connect(self._on_navigate_up)
        nav_bar.addWidget(self._nav_up_btn)
        self._breadcrumbs_widget = QWidget()
        self._breadcrumbs_layout = QHBoxLayout(self._breadcrumbs_widget)
        self._breadcrumbs_layout.setContentsMargins(0, 0, 0, 0)
        self._breadcrumbs_layout.setSpacing(2)
        nav_bar.addWidget(self._breadcrumbs_widget, stretch=1)
        self._search_edit = QLineEdit()
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setPlaceholderText(t("meta_search_tracks"))
        self._search_edit.setAccessibleName(t("meta_search_tracks"))
        self._search_edit.setMaximumWidth(250)
        # Qt builds the inline clear button itself and leaves it unnamed, so a
        # screen reader reaches an anonymous button at the end of the field.
        for clear_button in self._search_edit.findChildren(QToolButton):
            a11y.describe(clear_button, t("meta_a11y_clear_search"),
                          tooltip=t("meta_a11y_clear_search"))
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        nav_bar.addWidget(self._search_edit)
        table_content_layout.addLayout(nav_bar)
        self._refresh_navigation_arrow_direction()

        tbl_head = QHBoxLayout()
        tbl_head.setContentsMargins(12, 0, 12, 0)
        
        # Zoom controls — the magnifying-glass +/- icons say "zoom" on their
        # own, so no separate leading icon is needed alongside them.
        self._zoom_minus_btn = QPushButton()
        self._zoom_minus_btn.setIcon(FluentIcon.ZOOM_OUT.icon())
        self._zoom_minus_btn.setIconSize(QSize(12, 12))
        self._zoom_minus_btn.setFixedSize(24, 24)
        self._zoom_minus_btn.setAccessibleName(t("meta_a11y_zoom_out"))
        self._zoom_minus_btn.clicked.connect(self._on_zoom_minus)
        tbl_head.addWidget(self._zoom_minus_btn)

        self._zoom_val_lbl = QLineEdit("100%")
        self._zoom_val_lbl.setFixedSize(48, 24)
        self._zoom_val_lbl.setAlignment(Qt.AlignCenter)
        self._zoom_val_lbl.setAccessibleName(t("meta_a11y_zoom_value"))
        self._zoom_val_lbl.editingFinished.connect(self._on_zoom_custom)
        tbl_head.addWidget(self._zoom_val_lbl)

        self._zoom_plus_btn = QPushButton()
        self._zoom_plus_btn.setIcon(FluentIcon.ZOOM_IN.icon())
        self._zoom_plus_btn.setIconSize(QSize(12, 12))
        self._zoom_plus_btn.setFixedSize(24, 24)
        self._zoom_plus_btn.setAccessibleName(t("meta_a11y_zoom_in"))
        self._zoom_plus_btn.clicked.connect(self._on_zoom_plus)
        tbl_head.addWidget(self._zoom_plus_btn)
        
        tbl_head.addSpacing(10)
        tbl_head.addStretch()

        self._table_info_lbl = QLabel("")
        tbl_head.addWidget(self._table_info_lbl)
        self._apply_scope_lbl = QLabel("")
        self._apply_scope_lbl.setAccessibleName(t("meta_apply_scope_label"))
        tbl_head.addWidget(self._apply_scope_lbl)
        self._exclude_apply_btn = QToolButton()
        self._exclude_apply_btn.clicked.connect(self._toggle_selected_apply_exclusion)
        tbl_head.addWidget(self._exclude_apply_btn)
        self._excluded_chip = QToolButton()
        self._excluded_chip.setCheckable(True)
        a11y.describe_filter_toggle(
            self._excluded_chip, t("meta_excluded_filter_chip", n=0),
            t("meta_a11y_excluded_filter_desc"))
        self._excluded_chip.toggled.connect(self._on_excluded_chip_toggled)
        tbl_head.addWidget(self._excluded_chip)
        self._stale_chip = QToolButton()
        self._stale_chip.setCheckable(True)
        self._stale_chip.setText(t("meta_external_filter", n=0))
        a11y.describe_filter_toggle(
            self._stale_chip, t("meta_external_filter", n=0),
            t("meta_a11y_external_filter_desc"))
        self._stale_chip.toggled.connect(self._on_stale_chip_toggled)
        tbl_head.addWidget(self._stale_chip)
        table_content_layout.addLayout(tbl_head)

        self._table = ExplorerDetailsView()
        self._table.setAccessibleName(t("meta_a11y_details_table"))
        self._table.setAccessibleDescription(t("meta_a11y_details_table_desc"))
        self._table.deleteRequested.connect(self._request_delete_files)
        self._table.openRequested.connect(self._open_tracks)
        self._table.renameRequested.connect(self._rename_tracks)
        self._table.keyboardContextMenuRequested.connect(self._on_table_context_menu)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        self._table.viewportResized.connect(self._fill_leftover_space)
        # Suppress Qt's built-in row selection fill so our drawRow capsule
        # is the sole selection visual (no qfluentwidgets per-cell borders).
        # setStyle() does NOT transfer ownership — store in instance var so
        # Python GC doesn't destroy the object and leave Qt with a dangling ptr.
        self._explorer_table_style = ExplorerTableStyle(self._table.style())
        self._table.setStyle(self._explorer_table_style)
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # EditKeyPressed = F2 on Windows (Qt platform edit key) — matches
        # Win11 Explorer rename behavior.
        self._table.setEditTriggers(
            QAbstractItemView.DoubleClicked |
            QAbstractItemView.SelectedClicked |
            QAbstractItemView.AnyKeyPressed  |
            QAbstractItemView.EditKeyPressed
        )
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._table.horizontalScrollBar().setSingleStep(24)
        # Layout direction inherits from the app (RTL for Hebrew, LTR for
        # English). The filename columns get a per-column LTR delegate below
        # so file paths read correctly in either mode.
        self._table.setItemDelegate(
            ExplorerFileListDelegate(self._table)
        )
        # Filename columns get their own delegate: LTR, ElideMiddle, icon, checkbox.
        self._table.setItemDelegateForColumn(
            COL_FILENAME,
            FilenameDelegate(self._table, icon_provider=self._track_icon, show_checkbox=True),
        )
        self._table.setItemDelegateForColumn(
            COL_FILENAME_NEW,
            FilenameDelegate(self._table, icon_provider=self._track_icon, show_checkbox=False),
        )

        hdr = MetadataHeaderView(self._table)
        hdr.setAccessibleName(t("meta_a11y_table_header"))
        self._table.setHorizontalHeader(hdr)
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        hdr.setContextMenuPolicy(Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._on_header_context_menu)
        hdr.toggled.connect(self._on_select_all_toggled)
        hdr.sectionAutoSizeRequested.connect(self._size_column_to_fit)

        # Restore or set default column visibility
        default_hidden = {COL_GENRE_CUR, COL_GENRE_NEW, COL_COMMENT_CUR, COL_COMMENT_NEW}
        saved_visibility = None
        if self._cfg:
            saved_visibility = self._cfg.tag_editor_column_visibility
        for col in range(COLUMN_COUNT):
            if col == COL_CHECK or col == COL_FILENAME:
                self._table.setColumnHidden(col, False)
            elif saved_visibility is not None:
                self._table.setColumnHidden(col, col in saved_visibility)
            else:
                self._table.setColumnHidden(col, col in default_hidden)

        # Allow drag reordering. Restore order from config or apply default.
        hdr.setSectionsMovable(True)
        hdr.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)

        saved_order = None
        if self._cfg:
            saved_order = self._cfg.tag_editor_column_order

        hdr.blockSignals(True)
        try:
            if saved_order and len(saved_order) == COLUMN_COUNT:
                for visual_idx, logical_idx in enumerate(saved_order):
                    current_visual = hdr.visualIndex(logical_idx)
                    if current_visual != visual_idx:
                        hdr.moveSection(current_visual, visual_idx)
            else:
                # Move new filename right next to original filename
                hdr.moveSection(hdr.visualIndex(COL_FILENAME_NEW), hdr.visualIndex(COL_FILENAME) + 1)
                hdr.moveSection(hdr.visualIndex(COL_CHECK), COLUMN_COUNT - 1)
        finally:
            hdr.blockSignals(False)

        hdr.sectionMoved.connect(self._on_section_moved)
        # _set_zoom() below restores per-column widths and settles the
        # filler column afterward, so no separate fill pass is needed here.

        # Win11 Explorer header-click-to-sort.
        self._table.setSortingEnabled(True)
        hdr.setSectionsClickable(True)

        saved_sort_col = -1
        saved_sort_order = Qt.SortOrder.AscendingOrder
        if self._cfg:
            saved_sort_col = self._cfg.tag_editor_sort_column
            saved_sort_order_val = self._cfg.tag_editor_sort_order
            saved_sort_order = Qt.SortOrder(saved_sort_order_val)

        if saved_sort_col == COL_CHECK:
            saved_sort_col = -1
            if self._cfg:
                self._cfg.tag_editor_sort_column = -1
                self._cfg.save()

        if saved_sort_col != -1:
            hdr.blockSignals(True)
            try:
                self._table.sortByColumn(saved_sort_col, saved_sort_order)
                hdr.setSortIndicatorShown(True)
            finally:
                hdr.blockSignals(False)
        else:
            hdr.setSortIndicatorShown(False)

        hdr.sortIndicatorChanged.connect(self._on_sort_indicator_changed)

        self._table.selectionModel().selectionChanged.connect(self._on_table_selection_changed)
        self._model.dataChanged.connect(self._on_model_data_changed)
        self._model.rowsInserted.connect(lambda *_: self._refresh_checked_scope_state())
        self._model.rowsRemoved.connect(lambda *_: self._refresh_checked_scope_state())
        self._model.modelReset.connect(self._refresh_checked_scope_state)

        table_content_layout.addWidget(self._table)
        self._table_stack.addWidget(self._table_content)
        self._table_empty_page = self._build_table_empty_page()
        self._table_loading_page = self._build_table_loading_page()
        self._table_stack.addWidget(self._table_empty_page)
        self._table_stack.addWidget(self._table_loading_page)
        table_layout.addWidget(self._table_stack)
        self._show_table_empty()
        splitter.addWidget(table_frame)
        splitter.setStretchFactor(1, 1)

        # ── Right: inspector ──────────────────────────────────────────────────
        inspector_shell = self._build_inspector_shell()
        splitter.addWidget(inspector_shell)
        splitter.setStretchFactor(2, 0)

        table_frame.setMinimumWidth(self._TABLE_OPEN_MIN)

        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        splitter.setCollapsible(2, True)

        for handle_index in (1, 2):
            handle = splitter.handle(handle_index)
            if handle:
                handle.setProperty("metadata_handle_index", handle_index)
                handle.installEventFilter(self)

        self._apply_body_sizes(self._restore_body_sizes(), save=False)

        # Set initial table zoom level
        self._set_zoom(self._zoom_level)

        # Connect resize signal and disable the initial resize-ignoring flag
        hdr.sectionResized.connect(self._on_section_resized)
        self._ignore_header_resize = False

        return splitter

    def _restore_body_sizes(self) -> list[int]:
        """Restore pane widths as one allocation, tolerating stale/malformed data.

        Persisted sizes are ordinary Qt logical pixels — Qt's own High-DPI
        scaling already maps them to the physical screen, so they are not
        rescaled here.  They are still sanitized as a whole: a saved total
        that no longer fits the available screen width is scaled down as one
        vector rather than clamping each pane independently, which previously
        let every pane claim the full screen width on its own.
        ``_normalize_body_sizes`` then restores the same rail/open-minimum
        invariants a live collapse toggle already enforces.
        """
        defaults = list(self._DEFAULT_SPLITTER_SIZES)
        minimums = (self._TREE_RAIL_WIDTH, self._TABLE_OPEN_MIN, self._INSPECTOR_RAIL_WIDTH)
        saved = getattr(self._cfg, "tag_editor_splitter_sizes", None) if self._cfg else None
        try:
            numbers = [int(value) for value in saved] if saved else None
        except (TypeError, ValueError):
            numbers = None
        if (numbers is None or len(numbers) != 3 or any(value <= 0 for value in numbers)
                or sum(numbers) < sum(minimums)):
            # A total below what the three panes need at their floor is not a
            # legitimate collapsed profile, it is corrupt data -- fall back
            # rather than trying to preserve it.
            numbers = list(defaults)

        screen = QApplication.primaryScreen()
        available = screen.availableGeometry().width() if screen else sum(defaults)
        total = sum(numbers)
        if available > 0 and total > available:
            ratio = available / total
            numbers = [max(1, round(value * ratio)) for value in numbers]

        return self._normalize_body_sizes(list(numbers))

    def _save_splitter_sizes(self, splitter: QSplitter) -> None:
        if self._ignore_splitter_save:
            return
        if self._cfg:
            self._cfg.tag_editor_splitter_sizes = splitter.sizes()
            self._cfg.save()

    # ── Inspector pages ───────────────────────────────────────────────────────

    def _set_tool_button_icon(self, button: QPushButton, fluent_icon) -> None:
        """Apply a FluentIcon to a rail button, tinted for the current theme."""
        button.setText("")
        button.setIcon(fluent_icon.icon(color=get_colors().text_secondary))
        button.setIconSize(QSize(18, 18))

    def _build_inspector_shell(self) -> QFrame:
        shell = QFrame()
        shell.setMinimumWidth(self._INSPECTOR_RAIL_WIDTH)
        self._inspector_shell = shell

        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self._inspector_content = QFrame()
        self._inspector_content.setMinimumWidth(self._INSPECTOR_OPEN_MIN - self._INSPECTOR_RAIL_WIDTH)
        content_layout = QVBoxLayout(self._inspector_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(34)
        self._inspector_header = header
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 6, 4)
        header_layout.setSpacing(6)
        self._inspector_title_lbl = QLabel(t("meta_edit_tags_group"))
        self._inspector_title_lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
        header_layout.addWidget(self._inspector_title_lbl)
        header_layout.addStretch()
        content_layout.addWidget(header)

        self._inspector_pages = QStackedWidget()
        self._inspector = QStackedWidget()
        self._inspector.setStyleSheet(
            "QWidget { font-size: 12px; }"
            "QGroupBox { font-weight: bold; font-size: 12px; margin-top: 10px; padding: 10px 8px 8px 8px; }"
            "QLineEdit { font-size: 12px; min-height: 24px; padding: 3px 8px; border-radius: 0px; }"
            "QPushButton { font-size: 12px; padding: 6px 10px; border-radius: 8px; }"
            "QLabel { font-size: 12px; }"
        )
        self._inspector.addWidget(self._build_inspector_empty())   # 0
        self._inspector.addWidget(self._build_inspector_folder())  # 1
        self._inspector.addWidget(self._build_inspector_tracks())  # 2
        # Four purpose-built categories (manual edit / derive from filename /
        # cleanup & clear tags / rename the physical file) — each op appears
        # in exactly one place, so the rail maps 1:1 onto "what am I trying
        # to do" instead of leftover groupings from earlier iterations.
        inspector_tools = [
            ("details", t("meta_edit_tags_group"), FluentIcon.EDIT, self._inspector),
            (
                "actions",
                t("meta_action_engine_title"),
                FluentIcon.TAG,
                self._build_action_engine_page(),
            ),
            (
                "from_filename",
                t("meta_group_from_filename"),
                FluentIcon.PASTE,
                self._build_inspector_actions(
                    ("title_strip", "title_full", "track_num", "split_at"),
                ),
            ),
            (
                "cleanup",
                t("meta_group_cleanup"),
                FluentIcon.ERASE_TOOL,
                self._build_inspector_actions(
                    None,
                    sections=(
                        ("meta_section_text_cleanup", ("normalize_spaces", "strip_junk", "album_artist")),
                        ("meta_section_clear_fields", (
                            "clear_title", "clear_artist", "clear_album", "clear_album_artist",
                            "clear_track_num", "clear_year", "clear_genre", "clear_comments",
                        )),
                    ),
                ),
            ),
            (
                "files",
                t("meta_rename_group"),
                FluentIcon.DOCUMENT,
                self._build_inspector_actions(
                    ("clean_filename", "strip_filename_numbering"),
                ),
            ),
            (
                "duplicates",
                t("meta_duplicates_tools_title"),
                FluentIcon.FINGERPRINT,
                self._build_duplicate_tools_page(),
            ),
            (
                "online",
                t("meta_online_title"),
                FluentIcon.SEARCH,
                self._build_online_metadata_page(),
            ),
            (
                "problems",
                t("meta_problems_title"),
                FluentIcon.INFO,
                self._build_problems_page(),
            ),
        ]
        self._inspector_tool_titles: list[str] = []
        self._inspector_tool_buttons: list[QPushButton] = []
        self._inspector_tool_kinds: list = []
        for _tool_id, title, icon, page in inspector_tools:
            self._inspector_tool_titles.append(title)
            self._inspector_tool_kinds.append(icon)
            self._inspector_pages.addWidget(page)
        content_layout.addWidget(self._inspector_pages, stretch=1)

        self._inspector_rail = QFrame()
        self._inspector_rail.setFixedWidth(self._INSPECTOR_RAIL_WIDTH)
        rail_layout = QVBoxLayout(self._inspector_rail)
        rail_layout.setContentsMargins(5, 6, 5, 6)
        rail_layout.setSpacing(6)

        for index, title in enumerate(self._inspector_tool_titles):
            btn = QPushButton()
            btn.setFixedSize(30, 30)
            self._set_tool_button_icon(btn, self._inspector_tool_kinds[index])
            # Icon-only: without an explicit name a screen reader announces
            # nothing at all for the whole inspector rail.
            a11y.describe(btn, title, tooltip=title)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, i=index: self._toggle_inspector_tool(i))
            self._inspector_tool_buttons.append(btn)
            rail_layout.addWidget(btn)
        rail_layout.addStretch()

        shell_layout.addWidget(self._inspector_content, stretch=1)
        shell_layout.addWidget(self._inspector_rail)
        self._select_inspector_tool(0)
        return shell

    def _build_action_engine_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(10)
        title = QLabel(t("meta_action_engine_page_title"))
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)
        body = QLabel(t("meta_action_engine_page_body"))
        body.setWordWrap(True)
        layout.addWidget(body)
        self._action_engine_btn = QPushButton(t("meta_action_engine_open"))
        self._action_engine_btn.setIcon(FluentIcon.TAG.icon())
        self._action_engine_btn.setAccessibleName(t("meta_action_engine_open"))
        self._action_engine_btn.clicked.connect(self._on_action_engine)
        layout.addWidget(self._action_engine_btn)
        layout.addStretch()
        return page

    def _create_action_engine_dialog(self) -> TagActionDialog:
        return TagActionDialog(
            self._workspace,
            active_folder=self._navigation.current,
            parent=self,
            accept_preview=self._accept_tag_action_preview,
            open_preset_transfer=self._on_metadata_io,
        )

    def _on_action_engine(self) -> None:
        dialog = self._create_action_engine_dialog()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._model.refresh_all()
            self._update_summary()
            self._refresh_checked_scope_state()
            self._populate_track_inspector()

    def _ordered_io_item_ids(self) -> tuple[int, ...]:
        visible = list(self._proxy.visible_tracks())
        seen = {id(item) for item in visible}
        ordered = visible + [item for item in self._workspace.tracks if id(item) not in seen]
        return tuple(self._workspace.item_id(item) for item in ordered)

    def _filtered_problem_issue_ids(self) -> tuple[str, ...]:
        if not hasattr(self, "_problems_table"):
            return ()
        return tuple(self._problems_table.item(row, 0).data(Qt.UserRole)
                     for row in range(self._problems_table.rowCount())
                     if self._problems_table.item(row, 0) is not None)

    def _on_metadata_io(self) -> None:
        if not self._metadata_io_callbacks:
            return
        from ui.panels.metadata_editor.io_dialog import MetadataIODialog
        dialog = MetadataIODialog(
            self._workspace, callbacks=self._metadata_io_callbacks,
            root=self._root_folder, ordered_item_ids=self._ordered_io_item_ids(),
            problem_issue_ids=self._filtered_problem_issue_ids(), parent=self,
        )
        self._metadata_io_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._metadata_io_dialog = None

    def on_metadata_io_started(self, identity) -> None:
        if self._metadata_io_dialog is not None:
            self._metadata_io_dialog.on_io_started(identity)

    def on_metadata_io_finished(self, identity, result) -> None:
        if self._metadata_io_dialog is not None:
            self._metadata_io_dialog.on_io_finished(identity, result)

    def on_metadata_io_error(self, identity, error) -> None:
        if self._metadata_io_dialog is not None:
            self._metadata_io_dialog.on_io_error(identity, error)

    def _build_online_metadata_page(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 14); layout.setSpacing(10)
        title = QLabel(t("meta_online_title")); title.setStyleSheet("font-weight: bold; font-size: 13px;")
        body = QLabel(t("meta_online_explicit_search_hint")); body.setWordWrap(True)
        self._online_scope_label = QLabel(t("meta_online_scope", n=0)); self._online_scope_label.setWordWrap(True)
        self._online_open_button = QPushButton(t("meta_online_open")); self._online_open_button.setAccessibleName(t("meta_online_open"))
        self._online_open_button.clicked.connect(self._on_online_metadata)
        for widget in (title, body, self._online_scope_label, self._online_open_button): layout.addWidget(widget)
        layout.addStretch(); return page

    def _on_online_metadata(self) -> None:
        tracks = self._workspace.selected_tracks()
        if not tracks:
            show_warning(self, t("meta_online_title"), t("meta_online_select_files")); return
        ids = tuple(sorted(self._workspace.item_id(item) for item in tracks))
        dialog = OnlineMetadataDialog(
            self._workspace, ids, parent=self,
            search=self.online_search_requested.emit,
            cancel=self.online_cancel_requested.emit,
            preview=self.online_preview_requested.emit,
            artwork=self.online_artwork_requested.emit,
            accept=self.online_accept_requested.emit,
        )
        self._online_dialog = dialog
        dialog.finished.connect(lambda _=0, target=dialog: self._retire_online_dialog(target))
        dialog.open()

    def _retire_online_dialog(self, dialog) -> None:
        if getattr(self, "_online_dialog", None) is dialog:
            self._online_dialog = None

    def on_online_lookup_started(self, request) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None:
            dialog.state.setText(t("meta_online_searching"))

    def on_online_lookup_finished(self, result) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_lookup_result(result)

    def on_online_release_detail_finished(self, result) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_release_detail_result(result)

    def on_online_match_preview(self, preview) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_match_preview(preview)

    def on_online_artwork_ready(self, candidates, selected, entry) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_artwork_ready(candidates, selected, entry)

    def on_online_artwork_error(self, message_key: str) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_artwork_error(message_key)

    def on_online_acceptance_error(self, message_key: str) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_acceptance_error(message_key)

    def on_online_acceptance_complete(self, accepted: bool) -> None:
        dialog = getattr(self, "_online_dialog", None)
        if dialog is not None: dialog.on_acceptance_complete(accepted)
        if accepted:
            self._model.refresh_all(); self._update_summary(); self._refresh_checked_scope_state(); self._populate_track_inspector()

    def _build_inspector_actions(
        self,
        op_keys: Optional[tuple[str, ...]],
        *,
        sections: Optional[tuple[tuple[str, tuple[str, ...]], ...]] = None,
    ) -> QScrollArea:
        # No inline title here — the persistent inspector header
        # (self._inspector_title_lbl) already shows the active category name,
        # so repeating it inside the page just duplicated the same text twice.
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignTop)

        layout.addWidget(self._build_magic_ops_widget(op_keys, sections=sections))
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        return scroll

    def _build_duplicate_tools_page(self) -> QScrollArea:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignTop)

        self._dupes_btn = QPushButton(self._toolbar_text("meta_find_duplicates"))
        self._dupes_btn.setIcon(FluentIcon.SEARCH_MIRROR.icon(color=get_colors().text_primary))
        self._dupes_btn.setIconSize(QSize(16, 16))
        self._dupes_btn.setEnabled(False)
        a11y.describe(self._dupes_btn, t("meta_find_duplicates"),
                      description=t("meta_dupes_tooltip"),
                      tooltip=t("meta_dupes_tooltip"))
        self._dupes_btn.clicked.connect(self._on_find_duplicates)
        layout.addWidget(self._dupes_btn)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        return scroll

    def _build_problems_page(self) -> QScrollArea:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self._problems_summary = QLabel(t("meta_problems_empty"))
        self._problems_summary.setWordWrap(True)
        layout.addWidget(self._problems_summary)
        filters = QHBoxLayout()
        self._problems_severity = QComboBox()
        self._problems_severity.addItem(t("meta_problems_all"), "")
        for key in ("information", "warning", "error", "blocker"):
            self._problems_severity.addItem(t(f"meta_problems_severity_{key}"), key)
        self._problems_category = QComboBox()
        self._problems_category.addItem(t("meta_problems_all_categories"), "")
        for key in ("basic_metadata", "numbering", "format_capability", "pending_changes", "artwork", "filename_path"):
            self._problems_category.addItem(t(f"meta_problems_category_{key}"), key)
        self._problems_state = QComboBox()
        self._problems_state.addItem(t("meta_problems_all_states"), "")
        for key in ("present_on_disk", "resolved_by_pending", "introduced_by_pending", "pending_blocker", "changed_excluded"):
            self._problems_state.addItem(t(f"meta_problems_state_{key}"), key)
        self._problems_search = QLineEdit()
        self._problems_search.setPlaceholderText(t("meta_problems_search"))
        self._problems_search.setAccessibleName(t("meta_problems_search"))
        self._problems_severity.currentIndexChanged.connect(self._render_problems)
        self._problems_category.currentIndexChanged.connect(self._render_problems)
        self._problems_state.currentIndexChanged.connect(self._render_problems)
        self._problems_search.textChanged.connect(self._render_problems)
        filters.addWidget(self._problems_severity)
        filters.addWidget(self._problems_category)
        filters.addWidget(self._problems_state)
        filters.addWidget(self._problems_search, stretch=1)
        layout.addLayout(filters)
        self._problems_table = QTableWidget(0, 7)
        self._problems_table.setHorizontalHeaderLabels([t("meta_problems_severity"), t("meta_problems_problem"),
                                                         t("meta_problems_category"), t("meta_problems_state"),
                                                         t("meta_problems_file"), t("meta_problems_field"), t("meta_problems_fixable")])
        self._problems_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._problems_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._problems_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._problems_table.horizontalHeader().setStretchLastSection(True)
        self._problem_selected_ids: set[str] = set()
        self._problems_table.itemSelectionChanged.connect(self._remember_problem_selection)
        layout.addWidget(self._problems_table, stretch=1)
        controls = QHBoxLayout()
        refresh = QPushButton(t("meta_problems_revalidate"))
        refresh.setAccessibleName(t("meta_problems_revalidate"))
        refresh.clicked.connect(self._begin_revalidate_problems)
        fix = QPushButton(t("meta_problems_fix_selected"))
        fix.setAccessibleName(t("meta_problems_fix_selected"))
        fix.clicked.connect(self._on_fix_selected_problems)
        select_all = QPushButton(t("meta_problems_select_all")); select_all.clicked.connect(self._problems_table.selectAll)
        clear = QPushButton(t("meta_problems_clear_selection")); clear.clicked.connect(self._problems_table.clearSelection)
        controls.addWidget(refresh); controls.addWidget(select_all); controls.addWidget(clear); controls.addWidget(fix); controls.addStretch()
        layout.addLayout(controls)
        self._problems_snapshot = None
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return scroll

    def on_validation_updated(self, snapshot) -> None:
        self._problems_snapshot = snapshot
        self._render_problems()

    def _begin_revalidate_problems(self) -> None:
        self._problems_summary.setText(t("meta_problems_validating"))
        self.revalidate_problems_requested.emit()

    def _remember_problem_selection(self) -> None:
        """Keep selection by immutable issue ID, not a filtered table row."""
        selected = {self._problems_table.item(row.row(), 0).data(Qt.UserRole)
                    for row in self._problems_table.selectionModel().selectedRows()
                    if self._problems_table.item(row.row(), 0) is not None}
        visible = {self._problems_table.item(row, 0).data(Qt.UserRole)
                   for row in range(self._problems_table.rowCount())
                   if self._problems_table.item(row, 0) is not None}
        self._problem_selected_ids.difference_update(visible)
        self._problem_selected_ids.update(selected)

    def _render_problems(self, *_args) -> None:
        if not hasattr(self, "_problems_table"):
            return
        snapshot = self._problems_snapshot
        issues = [] if snapshot is None else list(snapshot.issues)
        severity = self._problems_severity.currentData() or ""
        category = self._problems_category.currentData() or ""
        state = self._problems_state.currentData() or ""
        query = self._problems_search.text().casefold().strip()
        if severity:
            issues = [issue for issue in issues if issue.severity.value == severity]
        if category:
            issues = [issue for issue in issues if issue.category.value == category]
        if state:
            issues = [issue for issue in issues if getattr(issue.state, "value", issue.state) == state]
        if query:
            issues = [issue for issue in issues if query in " ".join((issue.message_key, *issue.display_paths)).casefold()]
        self._problems_table.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            values = (t(f"meta_problems_severity_{issue.severity.value}"), t(issue.message_key),
                      t(f"meta_problems_category_{issue.category.value}"), t(f"meta_problems_state_{getattr(issue.state, 'value', issue.state)}"),
                      issue.display_paths[0] if issue.display_paths else "", ", ".join(issue.fields),
                      t("meta_problems_yes") if issue.fixable else t("meta_problems_no"))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.UserRole, issue.id)
                if column == 4:
                    cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self._problems_table.setItem(row, column, cell)
            if issue.id in self._problem_selected_ids:
                self._problems_table.selectRow(row)
        if snapshot is not None and snapshot.cancelled:
            self._problems_summary.setText(t("meta_problems_cancelled"))
        elif snapshot is not None and snapshot.error:
            self._problems_summary.setText(t("meta_problems_error"))
        elif snapshot is not None and not snapshot.current_for(self._workspace):
            self._problems_summary.setText(t("meta_problems_stale"))
        else:
            self._problems_summary.setText(t("meta_problems_count", n=len(issues)) if issues else t("meta_problems_empty"))

    def _on_fix_selected_problems(self) -> None:
        rows = self._problems_table.selectionModel().selectedRows()
        issue_ids = [self._problems_table.item(row.row(), 0).data(Qt.UserRole) for row in rows]
        snapshot = getattr(self, "_problems_snapshot", None)
        issues = [] if snapshot is None else [issue for issue in snapshot.issues if issue.id in issue_ids]
        self._request_problem_fix([issue.id for issue in issues])

    def _request_problem_fix(self, issue_ids, *, text: str = "") -> None:
        snapshot = getattr(self, "_problems_snapshot", None)
        issues = [] if snapshot is None else [issue for issue in snapshot.issues if issue.id in set(issue_ids)]
        if not issues or not all(issue.fixable for issue in issues):
            show_warning(self, t("meta_problems_title"), t("meta_problems_no_safe_fix"))
            return
        value, ok = get_text(self, t("meta_problems_fix_selected"), t("meta_problems_value"), text=text)
        if not ok or not value.strip():
            return
        self.problem_fix_preview_requested.emit(list(issue_ids), value.strip())

    def on_problem_fix_preview(self, preview) -> None:
        """Render the immutable Phase 9 action preview; never reconstruct it."""
        dialog = QDialog(self); dialog.setWindowTitle(t("meta_problems_preview_title")); dialog.resize(760, 430)
        layout = QVBoxLayout(dialog)
        action = getattr(preview, "action_preview", None)
        deltas = () if action is None else action.deltas
        changed = sum(delta.status is ActionResultStatus.CHANGED for delta in deltas)
        results = ", ".join(
            f"{t(f'meta_action_status_{status.value}')}: {sum(delta.status is status for delta in deltas)}"
            for status in ActionResultStatus if any(delta.status is status for delta in deltas)
        )
        layout.addWidget(QLabel(t("meta_problems_preview_summary", n=len(preview.item_ids), changed=changed,
                                 value=preview.value, results=results)))
        table = QTableWidget(0, 6); table.setHorizontalHeaderLabels([t("meta_problems_file"), t("meta_problems_field"), t("meta_problems_old_value"), t("meta_problems_new_value"), t("meta_problems_result"), t("meta_problems_details")])
        for row, delta in enumerate(deltas):
            item = self._workspace.track_for_id(delta.item_id)
            table.insertRow(row)
            old = "" if item is None else str(item.proposed.effective_tags(item.original).field_value(preview.field) or "")
            proposed = str(delta.fields.get(preview.field, "")) if delta.status is ActionResultStatus.CHANGED else ""
            values = ("" if item is None else item.path.name, preview.field, old, proposed,
                      t(f"meta_action_status_{delta.status.value}"),
                      format_action_diagnostic(delta.diagnostic, filename="" if item is None else item.path.name))
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value); table.setItem(row, col, cell)
        table.horizontalHeader().setStretchLastSection(True); layout.addWidget(table, 1)
        buttons = QHBoxLayout(); back = QPushButton(t("meta_problems_return_parameters")); cancel = QPushButton(t("cancel_btn")); accept = QPushButton(t("meta_problems_add_pending"))
        back_requested = [False]
        def return_to_parameters() -> None:
            back_requested[0] = True
            dialog.reject()
        back.clicked.connect(return_to_parameters); cancel.clicked.connect(dialog.reject); accept.clicked.connect(dialog.accept)
        buttons.addWidget(back); buttons.addStretch(); buttons.addWidget(cancel); buttons.addWidget(accept); layout.addLayout(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.problem_fix_accept_requested.emit(preview)
        elif back_requested[0]:
            self._request_problem_fix(preview.issue_ids, text=preview.value)

    def on_problem_fix_preview_failed(self, message: str) -> None:
        show_warning(self, t("meta_problems_title"), message)

    def _select_inspector_tool(self, index: int) -> None:
        self._active_inspector_tool = index
        if hasattr(self, "_inspector_pages"):
            self._inspector_pages.setCurrentIndex(index)
        if hasattr(self, "_inspector_title_lbl"):
            titles = getattr(self, "_inspector_tool_titles", [])
            self._inspector_title_lbl.setText(titles[index] if 0 <= index < len(titles) else "")
        if self._right_collapsed:
            self._set_inspector_collapsed(False)
        self._refresh_tool_button_states()

    def _toggle_inspector_tool(self, index: int) -> None:
        if self._active_inspector_tool == index and not self._right_collapsed:
            self._set_inspector_collapsed(True)
            return
        self._select_inspector_tool(index)

    def _refresh_tool_button_states(self) -> None:
        c = get_colors()
        button_qss = (
            "QPushButton { background: transparent; border: none; border-radius: 8px; padding: 3px; }"
            f"QPushButton:hover {{ background: {c.surface2}; }}"
        )
        active_qss = (
            f"QPushButton {{ background: {c.surface2}; border: 1px solid {c.border};"
            " border-radius: 8px; padding: 3px; }"
        )
        if hasattr(self, "_inspector_tool_buttons"):
            for idx, btn in enumerate(self._inspector_tool_buttons):
                active = idx == self._active_inspector_tool and not self._right_collapsed
                btn.setStyleSheet(active_qss if active else button_qss)
                # Which tool is open was previously shown by background colour
                # alone.  The checked state carries the same meaning to a screen
                # reader and in high contrast; the stylesheet above still owns
                # the appearance, so nothing changes visually.
                btn.setChecked(active)
                icon = self._inspector_tool_kinds[idx] if idx < len(self._inspector_tool_kinds) else FluentIcon.EDIT
                self._set_tool_button_icon(btn, icon)
        if hasattr(self, "_tree_toggle_btn"):
            self._tree_toggle_btn.setStyleSheet(button_qss)
            self._set_tool_button_icon(self._tree_toggle_btn, FluentIcon.FOLDER)

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


    def _build_inspector_empty(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignCenter)
        lbl = QLabel(t("meta_select_files_prompt"))
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color: {get_colors().text_tertiary}; font-size: 13px;")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        return w

    def _build_inspector_folder(self) -> QScrollArea:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignTop)

        self._insp_folder_title = QLabel(t("meta_inspector_no_selection_title"))
        self._insp_folder_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._insp_folder_title.setWordWrap(True)
        layout.addWidget(self._insp_folder_title)
        note = QLabel(t("meta_inspector_no_selection_body"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 12px;")
        layout.addWidget(note)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        return scroll

    def _build_inspector_tracks(self) -> QScrollArea:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)

        self._insp_tracks_title = QLabel(t("meta_tracks_selected_count", n=0))
        self._insp_tracks_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(self._insp_tracks_title)

        self._insp_capability = QLabel()
        self._insp_capability.setWordWrap(True)
        self._insp_capability.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(self._insp_capability)

        fields_grp = QGroupBox(t("meta_inspector_metadata_section"))
        fields_layout = QVBoxLayout(fields_grp)
        fields_layout.setSpacing(6)

        self._insp_fields: dict[str, QLineEdit] = {}
        self._insp_clear_buttons: dict[str, QToolButton] = {}

        def _field(field_name: str, label: str) -> QLineEdit:
            row = QHBoxLayout()
            row.setSpacing(5)
            lbl = QLabel(label)
            # A minimum, not a fixed width: a long Hebrew field label at 200%
            # would otherwise be cut off mid-word.
            lbl.setMinimumWidth(96)
            lbl.setStyleSheet("font-size: 12px;")
            edit = QLineEdit()
            edit.setPlaceholderText(t("meta_mixed_placeholder"))
            # The visible QLabel is a sibling, not a buddy, so the field itself
            # must carry the name a screen reader announces on focus.
            lbl.setBuddy(edit)
            a11y.describe(edit, label)
            edit.textEdited.connect(
                lambda text, name=field_name: self._mark_insp_field_dirty(name, text)
            )
            clear_btn = QToolButton()
            clear_btn.setText(t("meta_inspector_clear_short"))
            # Every field row has a button reading just "Clear"; only the
            # accessible name can say which field this one clears.
            a11y.describe(
                clear_btn,
                t("meta_a11y_clear_named_field", field=label),
                tooltip=t("meta_inspector_clear_field"))
            clear_btn.clicked.connect(
                lambda _checked=False, name=field_name, target=edit: self._clear_insp_field(name, target)
            )
            row.addWidget(lbl)
            row.addWidget(edit)
            row.addWidget(clear_btn)
            fields_layout.addLayout(row)
            self._insp_fields[field_name] = edit
            self._insp_clear_buttons[field_name] = clear_btn
            return edit

        field_specs = (
            ("title", "meta_field_title"), ("artist", "meta_field_artist"),
            ("album", "meta_field_album"), ("album_artist", "meta_field_album_artist"),
            ("track_num", "meta_field_track"), ("track_total", "meta_field_track_total"),
            ("disc_num", "meta_field_disc"), ("disc_total", "meta_field_disc_total"),
            ("year", "meta_field_date"), ("genre", "meta_field_genre"),
            ("comment", "meta_field_comment"), ("composer", "meta_field_composer"),
            ("publisher", "meta_field_publisher"), ("copyright", "meta_field_copyright"),
            ("bpm", "meta_field_bpm"), ("isrc", "meta_field_isrc"),
            ("grouping", "meta_field_grouping"), ("sort_title", "meta_field_sort_title"),
            ("sort_artist", "meta_field_sort_artist"), ("sort_album", "meta_field_sort_album"),
            ("sort_album_artist", "meta_field_sort_album_artist"),
        )
        for field_name, label_key in field_specs:
            _field(field_name, t(label_key))

        # Compatibility aliases used by existing panel tests and handlers.
        self._insp_title = self._insp_fields["title"]
        self._insp_artist = self._insp_fields["artist"]
        self._insp_album = self._insp_fields["album"]
        self._insp_album_artist = self._insp_fields["album_artist"]
        self._insp_track = self._insp_fields["track_num"]

        btn_apply_fields = QPushButton(t("meta_apply_to_selection"))
        btn_apply_fields.setIcon(FluentIcon.ACCEPT.icon(color="#000000"))
        btn_apply_fields.setIconSize(QSize(14, 14))
        btn_apply_fields.setProperty("accentRole", "primary")
        btn_apply_fields.setStyleSheet(primary_btn_style())
        btn_apply_fields.clicked.connect(self._on_insp_apply_fields)
        self._selection_scope_buttons.append(btn_apply_fields)
        fields_layout.addWidget(btn_apply_fields)
        layout.addWidget(fields_grp)

        lyrics_grp = QGroupBox(t("meta_inspector_lyrics_section"))
        lyrics_layout = QVBoxLayout(lyrics_grp)
        self._insp_lyrics_state = QLabel()
        self._insp_lyrics_state.setWordWrap(True)
        lyrics_layout.addWidget(self._insp_lyrics_state)
        self._insp_lyrics = QTextEdit()
        self._insp_lyrics.setAcceptRichText(False)
        self._insp_lyrics.setMinimumHeight(120)
        self._insp_lyrics.setLayoutDirection(Qt.LayoutDirectionAuto)
        self._insp_lyrics.textChanged.connect(self._on_lyrics_text_changed)
        lyrics_layout.addWidget(self._insp_lyrics)
        lyrics_meta = QHBoxLayout()
        self._insp_lyrics_language = QLineEdit()
        self._insp_lyrics_language.setPlaceholderText(t("meta_lyrics_language"))
        self._insp_lyrics_description = QLineEdit()
        self._insp_lyrics_description.setPlaceholderText(t("meta_lyrics_description"))
        lyrics_meta.addWidget(self._insp_lyrics_language)
        lyrics_meta.addWidget(self._insp_lyrics_description)
        lyrics_layout.addLayout(lyrics_meta)
        lyrics_buttons = QHBoxLayout()
        self._insp_lyrics_set_btn = QPushButton(t("meta_lyrics_propose_replace"))
        self._insp_lyrics_set_btn.clicked.connect(self._on_lyrics_propose_set)
        self._insp_lyrics_clear_btn = QPushButton(t("meta_lyrics_propose_clear"))
        self._insp_lyrics_clear_btn.clicked.connect(self._on_lyrics_propose_clear)
        self._insp_lyrics_revert_btn = QPushButton(t("meta_lyrics_revert_pending"))
        self._insp_lyrics_revert_btn.clicked.connect(self._on_lyrics_revert)
        for button in (self._insp_lyrics_set_btn, self._insp_lyrics_clear_btn, self._insp_lyrics_revert_btn):
            lyrics_buttons.addWidget(button)
            self._selection_scope_buttons.append(button)
        lyrics_layout.addLayout(lyrics_buttons)
        layout.addWidget(lyrics_grp)

        artwork_grp = QGroupBox(t("meta_inspector_artwork_section"))
        artwork_layout = QVBoxLayout(artwork_grp)
        self._insp_artwork_current_label = QLabel(t("meta_artwork_current"))
        artwork_layout.addWidget(self._insp_artwork_current_label)
        self._insp_artwork_preview = ArtworkDropPreview(self._on_artwork_drop)
        self._insp_artwork_preview.setFixedSize(150, 150)
        self._insp_artwork_preview.setAlignment(Qt.AlignCenter)
        self._insp_artwork_preview.setStyleSheet("border: 1px dashed #8a8a8a; border-radius: 4px;")
        artwork_layout.addWidget(self._insp_artwork_preview, alignment=Qt.AlignHCenter)
        self._insp_artwork_proposed_label = QLabel(t("meta_artwork_proposed"))
        self._insp_artwork_proposed_preview = QLabel()
        self._insp_artwork_proposed_preview.setFixedSize(150, 150)
        self._insp_artwork_proposed_preview.setAlignment(Qt.AlignCenter)
        self._insp_artwork_proposed_preview.setStyleSheet("border: 1px solid #8a8a8a; border-radius: 4px;")
        artwork_layout.addWidget(self._insp_artwork_proposed_label)
        artwork_layout.addWidget(self._insp_artwork_proposed_preview, alignment=Qt.AlignHCenter)
        self._insp_artwork_state = QLabel()
        self._insp_artwork_state.setWordWrap(True)
        artwork_layout.addWidget(self._insp_artwork_state)
        artwork_buttons = QHBoxLayout()
        self._insp_artwork_add_btn = QPushButton(t("meta_artwork_add"))
        self._insp_artwork_replace_btn = QPushButton(t("meta_artwork_replace"))
        self._insp_artwork_remove_btn = QPushButton(t("meta_artwork_remove"))
        self._insp_artwork_paste_btn = QPushButton(t("meta_artwork_paste"))
        self._insp_artwork_export_btn = QPushButton(t("meta_artwork_export"))
        self._insp_artwork_revert_btn = QPushButton(t("meta_artwork_revert"))
        self._insp_artwork_add_btn.clicked.connect(self._on_artwork_add_choose)
        self._insp_artwork_replace_btn.clicked.connect(self._on_artwork_replace_choose)
        self._insp_artwork_remove_btn.clicked.connect(self._on_artwork_remove)
        self._insp_artwork_paste_btn.clicked.connect(self._on_artwork_paste)
        self._insp_artwork_export_btn.clicked.connect(self._on_artwork_export)
        self._insp_artwork_revert_btn.clicked.connect(self._on_artwork_revert)
        for button in (self._insp_artwork_add_btn, self._insp_artwork_replace_btn,
                       self._insp_artwork_remove_btn, self._insp_artwork_paste_btn,
                       self._insp_artwork_export_btn, self._insp_artwork_revert_btn):
            artwork_buttons.addWidget(button)
            self._selection_scope_buttons.append(button)
        artwork_layout.addLayout(artwork_buttons)
        layout.addWidget(artwork_grp)

        replay_grp = QGroupBox(t("meta_inspector_replaygain_section"))
        replay_layout = QVBoxLayout(replay_grp)
        replay_note = QLabel(t("meta_replaygain_plain_explanation"))
        replay_note.setWordWrap(True)
        replay_note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        replay_layout.addWidget(replay_note)
        self._insp_replay_values: dict[str, QLabel] = {}
        for field_name, key in (
            (REPLAYGAIN_TRACK_GAIN, "meta_replaygain_track_gain"),
            (REPLAYGAIN_TRACK_PEAK, "meta_replaygain_track_peak"),
            (REPLAYGAIN_ALBUM_GAIN, "meta_replaygain_album_gain"),
            (REPLAYGAIN_ALBUM_PEAK, "meta_replaygain_album_peak"),
            (REPLAYGAIN_REFERENCE_LOUDNESS, "meta_replaygain_reference_loudness"),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(t(key)))
            value_label = QLabel(t("meta_inspector_empty_value"))
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(value_label, stretch=1)
            replay_layout.addLayout(row)
            self._insp_replay_values[field_name] = value_label
        replay_actions = QHBoxLayout()
        self._insp_rg_track_btn = QPushButton(t("meta_replaygain_analyze_track"))
        self._insp_rg_album_btn = QPushButton(t("meta_replaygain_analyze_album"))
        self._insp_rg_cancel_btn = QPushButton(t("meta_replaygain_cancel"))
        self._insp_rg_cancel_btn.setVisible(False)
        self._insp_rg_track_btn.clicked.connect(self._on_replaygain_track)
        self._insp_rg_album_btn.clicked.connect(self._on_replaygain_album)
        self._insp_rg_cancel_btn.clicked.connect(self.replaygain_cancel_requested)
        for button in (self._insp_rg_track_btn, self._insp_rg_album_btn):
            replay_actions.addWidget(button)
            self._selection_scope_buttons.append(button)
        replay_actions.addWidget(self._insp_rg_cancel_btn)
        replay_layout.addLayout(replay_actions)
        replay_clear = QHBoxLayout()
        self._insp_rg_clear_track_btn = QPushButton(t("meta_replaygain_clear_track"))
        self._insp_rg_clear_album_btn = QPushButton(t("meta_replaygain_clear_album"))
        self._insp_rg_revert_btn = QPushButton(t("meta_replaygain_revert"))
        self._insp_rg_clear_track_btn.clicked.connect(self._on_replaygain_clear_track)
        self._insp_rg_clear_album_btn.clicked.connect(self._on_replaygain_clear_album)
        self._insp_rg_revert_btn.clicked.connect(self._on_replaygain_revert)
        for button in (self._insp_rg_clear_track_btn, self._insp_rg_clear_album_btn, self._insp_rg_revert_btn):
            replay_clear.addWidget(button)
            self._selection_scope_buttons.append(button)
        replay_layout.addLayout(replay_clear)
        self._insp_rg_progress = QProgressBar()
        self._insp_rg_progress.setVisible(False)
        replay_layout.addWidget(self._insp_rg_progress)
        layout.addWidget(replay_grp)

        props_grp = QGroupBox(t("meta_inspector_file_properties_section"))
        props_layout = QVBoxLayout(props_grp)
        self._insp_properties = QLabel()
        self._insp_properties.setWordWrap(True)
        self._insp_properties.setTextInteractionFlags(Qt.TextSelectableByMouse)
        props_layout.addWidget(self._insp_properties)
        self._insp_external_status = QLabel()
        self._insp_external_status.setWordWrap(True)
        props_layout.addWidget(self._insp_external_status)
        self._insp_external_review_btn = QPushButton(t("meta_external_review_action"))
        a11y.describe(self._insp_external_review_btn, t("meta_external_review_action"),
                      description=t("meta_external_review_title"))
        self._insp_external_review_btn.clicked.connect(
            self._review_selected_external_conflict)
        self._insp_external_review_btn.setVisible(False)
        props_layout.addWidget(self._insp_external_review_btn)
        layout.addWidget(props_grp)

        rename_grp = QGroupBox(t("meta_rename_group"))
        rename_layout = QVBoxLayout(rename_grp)
        rename_layout.setSpacing(6)
        rename_note = QLabel(t("meta_rename_note"))
        rename_note.setWordWrap(True)
        rename_note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        rename_layout.addWidget(rename_note)
        btn_rename = QPushButton(t("meta_rename_btn"))
        btn_rename.setIcon(FluentIcon.SAVE_AS.icon(color="#000000"))
        btn_rename.setIconSize(QSize(14, 14))
        btn_rename.setStyleSheet(btn_style())
        btn_rename.clicked.connect(self._on_insp_rename_from_title)
        self._selection_scope_buttons.append(btn_rename)
        rename_layout.addWidget(btn_rename)
        layout.addWidget(rename_grp)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        return scroll

    def _make_op_side_button(self, glyph: str, tooltip: str, accent: bool = False) -> QPushButton:
        """Small icon button shown on the trailing edge of an op row.

        ``accent=True`` gives the button a tinted, always-visible look so
        configurable actions (the settings gear) are easy to spot next to
        the subtler info icon.
        """
        c = get_colors()
        btn = QPushButton(glyph)
        btn.setFixedSize(28, 28)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # The glyph is decorative, so the tooltip is the only stated meaning;
        # it must also be the accessible name or the button is announced as an
        # unlabelled symbol.
        a11y.describe(btn, tooltip, tooltip=tooltip)
        if accent:
            btn.setStyleSheet(
                f"QPushButton {{ border: 1px solid transparent; font-size: 14px;"
                f"  background: transparent; color: {c.text_secondary}; border-radius: 8px; }}"
                f"QPushButton:hover {{ background: {c.surface2}; color: {c.accent}; border-color: {c.border}; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ border: none; background: transparent; font-size: 13px;"
                f"  color: {c.text_secondary}; border-radius: 8px; }}"
                f"QPushButton:hover {{ background: {c.surface2}; color: {c.accent}; }}"
            )
        return btn

    def _refresh_op_rows_style(self) -> None:
        """Re-apply theme-aware styling to all inspector op rows."""
        if not hasattr(self, "_op_rows"):
            return
        qss = op_row_qss()
        for row in self._op_rows:
            row.setStyleSheet(qss)

    def _build_magic_ops_widget(
        self,
        op_keys: Optional[tuple[str, ...]] = None,
        *,
        sections: Optional[tuple[tuple[str, tuple[str, ...]], ...]] = None,
    ) -> QWidget:
        """Build the magic operations list — modern card rows, all act on checked tracks.

        Either pass `op_keys` (a flat, already-ordered list of op ids) or
        `sections` — a list of (subheader_i18n_key, op_ids) groups — to add
        light dividers inside a single category (e.g. "Text Cleanup" vs
        "Clear Fields" inside the broader cleanup category).
        """
        container = QWidget()
        grp_layout = QVBoxLayout(container)
        grp_layout.setContentsMargins(0, 0, 0, 0)
        grp_layout.setSpacing(8)

        op_handlers: dict[str, object] = {
            "title_strip":      lambda tracks: self.title_from_filename.emit(tracks, True),
            "title_full":       lambda tracks: self.title_from_filename.emit(tracks, False),
            "normalize_spaces": lambda tracks: self.normalize_title_spaces.emit(tracks),
            "track_num":        lambda tracks: self.track_from_filename.emit(tracks),
            "split_at":         lambda tracks: self.split_artist_title.emit(tracks),
            "album_artist":     lambda tracks: self.album_artist_from_artist.emit(tracks),
            "strip_junk":       lambda tracks: self.strip_web_junk.emit(tracks),
            "clear_comments":   lambda tracks: self.clear_comments.emit(tracks),
            "clear_track_num":  lambda tracks: self.clear_track_num.emit(tracks),
            "clear_year":       lambda tracks: self.clear_year.emit(tracks),
            "clear_genre":      lambda tracks: self.clear_genre.emit(tracks),
            "clear_title":      lambda tracks: self.clear_title.emit(tracks),
            "clear_artist":     lambda tracks: self.clear_artist.emit(tracks),
            "clear_album":      lambda tracks: self.clear_album.emit(tracks),
            "clear_album_artist": lambda tracks: self.clear_album_artist.emit(tracks),
            "clean_filename":   lambda tracks: self.clean_filename.emit(tracks),
            "strip_filename_numbering": lambda tracks: self.strip_filename_numbering.emit(tracks),
        }
        op_defs_by_key = {key: (label_key, desc_key) for key, label_key, desc_key in MAGIC_OP_DEFS}

        def run_for_checked(handler) -> None:
            tracks = self._workspace.edit_scope()
            if not tracks:
                self._refresh_checked_scope_state()
                return
            handler(tracks)

        def add_row(key: str) -> None:
            label_key, desc_key = op_defs_by_key[key]
            label = t(label_key)
            desc  = t(desc_key)
            row = OpRow(label)
            if key in op_handlers:
                row.clicked.connect(lambda handler=op_handlers[key]: run_for_checked(handler))

            if key in ("strip_junk", "clean_filename"):
                cfg_btn = self._make_op_side_button("", t("meta_clean_cfg_tooltip"), accent=True)
                cfg_btn.setIcon(FluentIcon.SETTING.icon())
                cfg_btn.setIconSize(QSize(14, 14))
                a11y.describe(cfg_btn, t("meta_a11y_configure_action", action=label),
                              description=t("meta_clean_cfg_tooltip"))
                cfg_btn.clicked.connect(self._on_clean_settings)
                row.add_side_button(cfg_btn)

            info_btn = self._make_op_side_button("", desc)
            info_btn.setIcon(FluentIcon.INFO.icon())
            info_btn.setIconSize(QSize(14, 14))
            # A short name to announce, the full explanation as the description:
            # the raw tooltip is a paragraph and makes a poor accessible name.
            a11y.describe(info_btn, t("meta_a11y_about_action", action=label),
                          description=desc)
            info_btn.clicked.connect(lambda _, l=label, d=desc: self._show_info(l, d))
            row.add_side_button(info_btn)

            grp_layout.addWidget(row)
            self._op_rows.append(row)

        if sections is not None:
            for i, (subheader_key, keys) in enumerate(sections):
                header = QLabel(t(subheader_key))
                header.setStyleSheet(
                    f"color: {get_colors().text_tertiary}; font-size: 11px; font-weight: bold;"
                    f"{'margin-top: 4px;' if i else ''}"
                )
                grp_layout.addWidget(header)
                for key in keys:
                    add_row(key)
        else:
            allowed = set(op_keys) if op_keys is not None else None
            for key, _, _ in MAGIC_OP_DEFS:
                if allowed is not None and key not in allowed:
                    continue
                add_row(key)

        self._refresh_op_rows_style()
        self._refresh_checked_scope_state()
        return container

    def _on_clean_settings(self) -> None:
        dlg = CleanSettingsDialog(self._cfg, self)
        dlg.exec()

    def _show_info(self, title: str, desc: str) -> None:
        show_info(self, title, desc)

    def _request_delete_files(self, paths: list[Path]) -> None:
        """Single-confirm Recycle Bin send for selected table rows.

        Called from `ExplorerFileListView.keyPressEvent` on Delete. Emits
        `delete_files_requested` only after the user confirms — the actual
        send2trash + rescan is owned by `MetadataController.delete_files`.
        """
        if not paths:
            return
        if confirm(
            self.window(),
            t("meta_delete_to_trash_title"),
            t("meta_delete_to_trash_body", n=len(paths)),
            accept_text=t("meta_delete_to_trash_confirm"),
            cancel_text=t("cancel_btn"),
            danger=True,
        ):
            result = self._run_file_operation("recycle_paths", list(paths))
            if result is None:
                return
            deleted = [outcome.source for outcome in result.succeeded]
            if deleted:
                self._model.remove_paths(deleted)
                self._rebuild_tree_from_loaded_tracks()
                self._update_summary()
                self._refresh_checked_scope_state()

    def _on_table_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if index.isValid() and not self._table.selectionModel().isSelected(index):
            self._table.selectRow(index.row())
            self._table.setCurrentIndex(index)
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        open_action = menu.addAction(t("meta_open_file"))
        reveal_action = menu.addAction(t("meta_reveal_in_explorer"))
        copy_action = menu.addAction(t("meta_copy_path"))
        menu.addSeparator()
        rename_action = menu.addAction(t("meta_rename_menu")) if len(tracks) == 1 else None
        move_action = menu.addAction(t("meta_move_menu"))
        properties_action = menu.addAction(t("meta_properties"))
        menu.addSeparator()
        delete_action = menu.addAction(t("meta_delete_menu"))
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == open_action:
            self._open_tracks(tracks)
        elif action == reveal_action:
            self._reveal_tracks(tracks)
        elif action == copy_action:
            self._copy_paths(tracks)
        elif rename_action is not None and action == rename_action:
            self._rename_tracks(tracks)
        elif action == move_action:
            self._move_tracks(tracks)
        elif action == properties_action:
            self._show_properties(tracks)
        elif action == delete_action:
            self._request_delete_files([track.path for track in tracks])

    def _open_tracks(self, tracks: list[AudioTrackItem]) -> None:
        self._perform_track_operation(tracks, self._file_operations.open_file)

    def _reveal_tracks(self, tracks: list[AudioTrackItem]) -> None:
        self._perform_track_operation(tracks, self._file_operations.reveal_in_explorer)

    def _copy_paths(self, tracks: list[AudioTrackItem]) -> None:
        try:
            paths = [self._file_operations.copy_path(track.path) for track in tracks]
        except FileOperationError as exc:
            show_warning(self, t("meta_error_title"), str(exc))
            return
        QApplication.clipboard().setText("\n".join(paths))

    def _rename_tracks(self, tracks: list[AudioTrackItem]) -> None:
        if len(tracks) != 1:
            return
        track = tracks[0]
        new_name, ok = get_text(self, t("meta_rename_dialog_title"), t("meta_rename_prompt"), text=track.path.name)
        if not ok or not new_name.strip():
            return
        result = self._run_file_operation("rename_path", track.path, new_name)
        if result is None or not result.succeeded:
            return
        destination = result.succeeded[0].destination
        if track.proposed_filename == destination.name:
            track.proposed_filename = None
        self._model.update_file_path(track, destination)
        self._rebuild_tree_from_loaded_tracks()
        self._refresh_checked_scope_state()

    def _move_tracks(self, tracks: list[AudioTrackItem]) -> None:
        if not self._root_folder:
            return
        folder = QFileDialog.getExistingDirectory(self, t("meta_move_choose_folder"), str(self._root_folder))
        if not folder:
            return
        by_path = {track.path: track for track in tracks}
        result = self._run_file_operation(
            "move_paths", list(by_path), Path(folder))
        if result is None:
            return
        for outcome in result.succeeded:
            track = by_path.get(outcome.source)
            if track is not None:
                self._model.update_file_path(track, outcome.destination)
        if result.succeeded:
            self._rebuild_tree_from_loaded_tracks()
            self._refresh_checked_scope_state()

    def _show_properties(self, tracks: list[AudioTrackItem]) -> None:
        lines: list[str] = []
        for track in tracks:
            try:
                props = self._file_operations.properties(track.path)
            except FileOperationError as exc:
                lines.append(str(exc))
                continue
            lines.append(t("meta_properties_item", name=props.path.name, path=str(props.path), size=isolate_number(f"{props.size_bytes:,}"), modified=isolate_number(display_timestamp(props.modified_at))))
        if lines:
            show_info(self, t("meta_properties"), "\n\n".join(lines))

    def _perform_track_operation(self, tracks: list[AudioTrackItem], operation) -> None:
        errors: list[str] = []
        for track in tracks:
            try:
                operation(track.path)
            except FileOperationError as exc:
                errors.append(str(exc))
        if errors:
            show_warning(self, t("meta_error_title"), "\n".join(errors))

    # ── Toolbar handlers ──────────────────────────────────────────────────────

    def _on_browse(self) -> None:
        saved_folder = getattr(self._cfg, "tag_editor_last_folder", "") if self._cfg else ""
        if self._root_folder and self._root_folder.is_dir():
            target = self._root_folder
        elif saved_folder and Path(saved_folder).is_dir():
            target = Path(saved_folder)
        else:
            target = None

        if target:
            parent = target.parent
            start_folder = parent if parent and parent.is_dir() and parent != target else target
        else:
            start_folder = Path.home()

        path = QFileDialog.getExistingDirectory(
            self, t("meta_choose_music_folder"), str(start_folder)
        )
        if path:
            self.scan_requested.emit(Path(path), True)

    def _on_scan(self) -> None:
        if not self._root_folder:
            return
        self.scan_requested.emit(self._root_folder, True)

    def on_workspace_replacement_started(self, folder: Path) -> None:
        """Update the visible root only after the controller accepts replacement."""
        self._root_folder = Path(folder)
        if self._navigation.root != self._root_folder:
            self._navigation.set_root(self._root_folder)
        self._file_operations.set_root(self._root_folder)
        if self._cfg:
            self._cfg.tag_editor_last_folder = str(self._root_folder)
            self._cfg.save()
        self._model.load_tracks([])
        self._tree.clear()
        self._folder_items.clear()
        self._file_items.clear()
        self._track_icon_cache.clear()
        self._inspector.setCurrentIndex(PAGE_EMPTY)
        self._apply_btn.setEnabled(False)
        self._revert_btn.setEnabled(False)
        self._dupes_btn.setEnabled(False)
        self._set_scan_loading(True)
        self._summary_lbl.setText(t("meta_scanning"))

    def _on_auto_arrange(self) -> None:
        tracks = self._workspace.edit_scope()
        if not tracks:
            self._refresh_checked_scope_state()
            return
        self.auto_sequence_requested.emit(tracks, list(self._auto_ops))

    def _on_apply(self) -> None:
        from core.change_sets import ApplyReviewPolicy
        backup_dir = get_tag_backup_dir()
        candidates = self._workspace.apply_candidates()
        if not candidates:
            blocked = self._workspace.apply_blockers()
            if blocked:
                show_warning(
                    self, t("meta_apply_blocked_title"),
                    t("meta_external_apply_blocked", n=len(blocked)))
                self._workspace.set_selected_items([blocked[0]])
                self._populate_track_inspector([blocked[0]])
            self._refresh_checked_scope_state()
            return
        if ApplyReviewPolicy.requires_full_review(self._workspace.change_set):
            self._on_review_changes()
        # The always-visible Apply-scope label already shows this exact count
        # before the action is invoked. Keep Apply a single toolbar action;
        # confirmation dialogs here hide that scope and break keyboard flow.
        self._apply_refresh_counter = 0
        self.apply_requested.emit(backup_dir, candidates)

    def _on_revert(self) -> None:
        if self._model.get_changed_count() <= 0:
            self._refresh_checked_scope_state()
            return
        self.revert_requested.emit(self._model.get_all_tracks())

    def _on_review_changes(self) -> None:
        """Show the authoritative stable-ID review surface for proposals.

        This dialog intentionally talks only in workspace IDs.  Qt rows are a
        view detail and can move as filters change; every command below is
        emitted as an ID/field tuple and is therefore safe to undo/redo.
        """
        self.review_opened.emit()
        records = self._workspace.change_set.records()
        if not records:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(t("meta_pending_changes"))
        dialog.setMinimumSize(860, 420)
        layout = QVBoxLayout(dialog)
        summary_label = QLabel(dialog)
        layout.addWidget(summary_label)
        filters = QHBoxLayout()
        file_filter = QComboBox(dialog); file_filter.addItem(t("meta_review_all_files"), "")
        type_filter = QComboBox(dialog); type_filter.addItem(t("meta_review_all_types"), "")
        category_filter = QComboBox(dialog); category_filter.addItem(t("meta_review_all_categories"), "")
        for category in ("metadata", "filename", "artwork", "lyrics", "replaygain"):
            category_filter.addItem(t(f"meta_review_category_{category}"), category)
        origin_filter = QComboBox(dialog); origin_filter.addItem(t("meta_review_all_origins"), "")
        inclusion_filter = QComboBox(dialog); inclusion_filter.addItem(t("meta_review_all_states"), "all")
        inclusion_filter.addItem(t("meta_change_included"), "included")
        inclusion_filter.addItem(t("meta_change_excluded"), "excluded")
        inclusion_filter.addItem(t("meta_review_warnings"), "warnings")
        inclusion_filter.addItem(t("meta_review_blocked"), "blocked")
        for record in records:
            item = self._workspace.track_for_id(record.item_id)
            filename = item.path.name if item else str(record.item_id)
            if file_filter.findData(record.item_id) < 0:
                file_filter.addItem(filename, record.item_id)
            if type_filter.findData(record.field) < 0:
                type_filter.addItem(record.field, record.field)
            if origin_filter.findData(record.origin.value) < 0:
                origin_filter.addItem(t(f"meta_change_origin_{record.origin.value}"), record.origin.value)
        for widget in (file_filter, type_filter, category_filter, origin_filter, inclusion_filter):
            filters.addWidget(widget)
        layout.addLayout(filters)
        table = QTableWidget(len(records), 8, dialog)
        table.setHorizontalHeaderLabels([
            t("meta_change_file"), t("meta_change_field"), t("meta_stored_value"),
            t("meta_proposed_value"), t("meta_change_source"),
            t("meta_change_included"), t("meta_review_warning"), t("meta_review_blocked"),
        ])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        blocked = self._review_blocked_records(records)
        for row, record in enumerate(records):
            item = self._workspace.track_for_id(record.item_id)
            filename = item.path.name if item else str(record.item_id)
            source_key = f"meta_change_origin_{record.origin.value}"
            source_text = record.source_attribution or t(source_key)
            if record.source_url:
                source_text = f"{source_text} — {record.source_url}"
            values = [filename, record.field, self._review_value(record.original_value),
                      self._review_value(record.proposed_value), source_text,
                      t("meta_change_excluded") if record.excluded_from_apply else t("meta_change_included"),
                      record.diagnostic or record.capability,
                      blocked.get((record.item_id, record.field), "")]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col in {0, 2, 3}:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, col, cell)
            table.setVerticalHeaderItem(row, QTableWidgetItem(f"{record.item_id}:{record.field}"))
        def apply_filters(*_):
            for row, record in enumerate(records):
                warning = bool(record.diagnostic or record.capability)
                is_blocked = (record.item_id, record.field) in blocked
                hidden = ((file_filter.currentData() and record.item_id != file_filter.currentData())
                          or (type_filter.currentData() and record.field != type_filter.currentData())
                          or (category_filter.currentData()
                              and self._review_category(record.field) != category_filter.currentData())
                          or (origin_filter.currentData() and record.origin.value != origin_filter.currentData())
                          or (inclusion_filter.currentData() == "included" and record.excluded_from_apply)
                          or (inclusion_filter.currentData() == "excluded" and not record.excluded_from_apply)
                          or (inclusion_filter.currentData() == "warnings" and not warning)
                          or (inclusion_filter.currentData() == "blocked" and not is_blocked))
                table.setRowHidden(row, bool(hidden))
            changed_files = {record.item_id for record in records}
            included_files = {record.item_id for record in records if not record.excluded_from_apply}
            excluded_files = changed_files - included_files
            pending_after_apply = len(excluded_files)
            summary_label.setText(t(
                "meta_review_counts", total=len(records), included=len(included_files),
                excluded=len(excluded_files), blocked=len(blocked), pending=pending_after_apply,
            ))
        apply_filters()
        file_filter.currentIndexChanged.connect(apply_filters)
        type_filter.currentIndexChanged.connect(apply_filters)
        category_filter.currentIndexChanged.connect(apply_filters)
        origin_filter.currentIndexChanged.connect(apply_filters)
        inclusion_filter.currentIndexChanged.connect(apply_filters)
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        actions = QHBoxLayout()
        include = QPushButton(t("meta_change_included"), dialog)
        exclude = QPushButton(t("meta_change_excluded"), dialog)
        revert_field = QPushButton(t("meta_review_revert_entries"), dialog)
        revert_file = QPushButton(t("meta_review_revert_files"), dialog)
        revert_filename = QPushButton(t("meta_review_revert_filename"), dialog)
        revert_artwork = QPushButton(t("meta_review_revert_artwork"), dialog)
        revert_lyrics = QPushButton(t("meta_review_revert_lyrics"), dialog)
        revert_replaygain = QPushButton(t("meta_review_revert_replaygain"), dialog)
        revert_all = QPushButton(t("meta_review_revert_all"), dialog)
        details = QPushButton(t("meta_review_blocker_details"), dialog)
        def selected_records():
            rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
            return [records[row] for row in rows if not table.isRowHidden(row)]
        def emit_include(excluded: bool):
            selected = selected_records()
            self.review_include_requested.emit(sorted({record.item_id for record in selected}), excluded)
            dialog.accept()
        include.clicked.connect(lambda: emit_include(False))
        exclude.clicked.connect(lambda: emit_include(True))
        revert_field.clicked.connect(lambda: (self.review_revert_records_requested.emit(
            [(record.item_id, record.field) for record in selected_records()]), dialog.accept()))
        revert_file.clicked.connect(lambda: (self.review_revert_files_requested.emit(
            sorted({record.item_id for record in selected_records()})), dialog.accept()))
        def revert_category(category: str):
            selected = [record for record in selected_records()
                        if self._review_category(record.field) == category]
            if selected:
                self.review_revert_records_requested.emit(
                    [(record.item_id, record.field) for record in selected])
                dialog.accept()
        revert_filename.clicked.connect(lambda: revert_category("filename"))
        revert_artwork.clicked.connect(lambda: revert_category("artwork"))
        revert_lyrics.clicked.connect(lambda: revert_category("lyrics"))
        revert_replaygain.clicked.connect(lambda: revert_category("replaygain"))
        revert_all.clicked.connect(lambda: (self.review_revert_files_requested.emit(
            sorted({record.item_id for record in records})), dialog.accept()))
        def show_blocker_details():
            selected = selected_records()
            messages = [blocked.get((record.item_id, record.field))
                        for record in selected if blocked.get((record.item_id, record.field))]
            if messages:
                show_warning(dialog, t("meta_review_blocker_details"), "\n".join(sorted(set(messages))))
        details.clicked.connect(show_blocker_details)
        for button in (include, exclude, revert_field, revert_file, revert_filename,
                       revert_artwork, revert_lyrics, revert_replaygain, revert_all, details):
            actions.addWidget(button)
        layout.addLayout(actions)
        table.itemDoubleClicked.connect(
            lambda item: self._navigate_review_record(records[item.row()]) if item.row() >= 0 else None)
        close = QPushButton(t("meta_ok"), dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    @staticmethod
    def _review_category(field: str) -> str:
        if field == "filename":
            return "filename"
        if field == ARTWORK_FIELD:
            return "artwork"
        if field == LYRICS_FIELD:
            return "lyrics"
        if field in REPLAYGAIN_FIELDS:
            return "replaygain"
        return "metadata"

    def _review_blocked_records(self, records) -> dict[tuple[int, str], str]:
        """Return only included records that cannot safely enter Apply."""
        from core.change_sets import file_identity_status
        blocked: dict[tuple[int, str], str] = {}
        for record in records:
            if record.excluded_from_apply:
                continue
            item = self._workspace.track_for_id(record.item_id)
            if item is None:
                blocked[(record.item_id, record.field)] = t("meta_review_missing_target")
                continue
            identity = file_identity_status(item.baseline_identity, item.path)
            external = getattr(item, "external_state", "current")
            if external_state_blocks_apply(external):
                blocked[(record.item_id, record.field)] = t(
                    "meta_external_review_blocker",
                    state=t(f"meta_external_state_{external}"))
            elif identity not in {"current", "unavailable"}:
                blocked[(record.item_id, record.field)] = t("meta_review_stale_target")
            elif record.capability:
                blocked[(record.item_id, record.field)] = record.capability
            elif record.diagnostic:
                blocked[(record.item_id, record.field)] = record.diagnostic
        return blocked

    def _navigate_review_record(self, record) -> None:
        """Navigate by stable ID; never reconstruct a target from a table row."""
        track = self._workspace.track_for_id(record.item_id)
        if track is None:
            return
        self._workspace.set_selected_items([track])
        for row in range(self._proxy.rowCount()):
            if self._proxy.track_at_row(row) is track:
                index = self._proxy.index(row, 0)
                self._table.scrollTo(index)
                self._table.selectRow(row)
                break

    @staticmethod
    def _review_value(value: object) -> str:
        text = str(value)
        return text if len(text) <= 180 else text[:177] + "..."

    def _on_restore_from_backup(self) -> None:
        """Pick a JSON tag backup, show exactly what it would touch, confirm, emit."""
        from core.metadata_processor import load_tag_backup

        backup_dir = get_tag_backup_dir()
        start_dir = str(backup_dir) if backup_dir.exists() else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, t("md_restore_pick_title"), start_dir,
            "BananaFlow tag backup (*.json);;JSON (*.json)",
        )
        if not path:
            return

        try:
            records = load_tag_backup(Path(path))
        except Exception as exc:
            show_warning(
                self, t("md_restore_invalid_title"), t("md_restore_invalid_msg"),
                details=str(exc),
            )
            return
        if not records:
            show_warning(self, t("md_restore_invalid_title"), t("md_restore_empty_msg"))
            return

        existing = [p for p, _tags in records if p.exists()]
        missing_n = len(records) - len(existing)
        if not existing:
            show_warning(self, t("md_restore_invalid_title"),
                         t("md_restore_all_missing_msg"))
            return

        msg = t("md_restore_confirm_msg", n=len(existing), backup=Path(path).name)
        if missing_n:
            msg += "\n" + t("md_restore_missing_note", n=missing_n)
        names = "\n".join(f"•  {p.name}" for p in existing[:10])
        if len(existing) > 10:
            names += "\n" + t("md_restore_more_files", n=len(existing) - 10)
        msg += "\n\n" + names

        if confirm(
            self,
            t("md_restore_confirm_title"),
            msg,
            accept_text=t("md_restore_confirm_btn"),
            danger=True,
        ):
            self.restore_requested.emit({"records": records, "backup_path": Path(path)})

    def _on_backup_manager(self) -> None:
        """Open the guarded backup workflow; disk actions remain signal-routed."""
        from ui.dialogs.backup_manager_dialog import BackupManagerDialog
        dialog = BackupManagerDialog(
            get_tag_backup_dir(),
            restore_callback=self.restore_requested.emit,
            undo_callback=self.undo_applied_requested.emit,
            parent=self,
        )
        dialog.exec()

    def on_restore_complete(self, outcomes: list) -> None:
        """Report per-file restore results; rescan so the table shows the restored tags."""
        from core.metadata_models import RestoreStatus
        self._set_restore_loading(False)
        self._artwork_thumbnail_cache.clear()
        self._cancel_artwork_thumbnails()

        restored  = sum(1 for o in outcomes if o.status == RestoreStatus.RESTORED)
        unchanged = sum(1 for o in outcomes if o.status == RestoreStatus.UNCHANGED)
        missing   = sum(1 for o in outcomes if o.status == RestoreStatus.MISSING)
        failed    = sum(1 for o in outcomes if o.status == RestoreStatus.FAILED)

        summary = t("md_restore_done", restored=restored, unchanged=unchanged,
                    missing=missing, fail=failed)
        problem_lines = "\n".join(
            f"{o.status}: {o.path}" + (f" ({o.error})" if o.error else "")
            for o in outcomes if o.status in (RestoreStatus.FAILED, RestoreStatus.MISSING)
        )
        if failed:
            show_warning(self, t("md_restore_summary_title"), summary,
                         details=problem_lines)
        else:
            show_info(self, t("md_restore_summary_title"), summary,
                      details=problem_lines)

        if self._root_folder:
            self._on_scan()

    def on_restore_started(self) -> None:
        self._set_restore_loading(True)

    def on_restore_progress(self, done: int, total: int) -> None:
        if not self._is_restoring:
            self.on_restore_started()
        if hasattr(self, "_center_progress"):
            if total > 0:
                self._center_progress.setRange(0, total)
                self._center_progress.setValue(min(done, total))
            else:
                self._center_progress.setRange(0, 0)

    def _on_find_duplicates(self) -> None:
        if not self._root_folder:
            return
        self._dupes_btn.setEnabled(False)
        self._summary_lbl.setText(t("meta_searching_duplicates"))
        self.find_duplicates_requested.emit(self._root_folder, True)  # always recursive

    # ── Tree handlers ─────────────────────────────────────────────────────────

    _ROLE_IS_FILE = Qt.UserRole + 1

    def _on_tree_item_changed(self, item: QTreeWidgetItem, col: int) -> None:
        """Propagate checkbox state to descendants and sync with the table model."""
        if col != 0 or self._ignore_tree_changes:
            return

        is_file = item.data(0, self._ROLE_IS_FILE)
        state   = item.checkState(0)

        if is_file:
            path = item.data(0, Qt.UserRole)
            if path:
                self._proxy.set_path_visible(path, state == Qt.Checked)
        else:
            if state == Qt.PartiallyChecked:
                return  # Qt.ItemIsAutoTristate manages this internally
            self._ignore_tree_changes = True
            self._propagate_check_state(item, state)
            self._ignore_tree_changes = False

    def _on_tree_navigation_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """Use a folder click for navigation; checkboxes remain visibility-only."""
        if item.data(0, self._ROLE_IS_FILE):
            return
        folder = item.data(0, Qt.UserRole)
        if folder and Path(folder).is_dir() and self._navigation.navigate(Path(folder)):
            self._apply_navigation_filter()

    def _on_navigate_back(self) -> None:
        if self._navigation.back():
            self._apply_navigation_filter()

    def _on_navigate_forward(self) -> None:
        if self._navigation.forward():
            self._apply_navigation_filter()

    def _on_navigate_up(self) -> None:
        if self._navigation.up():
            self._apply_navigation_filter()

    def _on_search_text_changed(self, text: str) -> None:
        self._apply_display_filter(lambda: self._proxy.set_search_text(text))
        self._refresh_checked_scope_state()

    def _apply_display_filter(self, update) -> None:
        """Keep workspace selection stable while Qt remaps filtered table rows."""
        selected = self._workspace.selected_tracks()
        self._is_filtering_view = True
        try:
            update()
        finally:
            self._is_filtering_view = False
        self._workspace.set_selected_items(selected)

    def _apply_navigation_filter(self) -> None:
        """Update proxy-only navigation state; never rescans or mutates Apply scope."""
        current = self._navigation.current
        self._apply_display_filter(
            lambda: self._proxy.set_folder(None if current == self._navigation.root else current)
        )
        self._refresh_checked_scope_state()
        self._refresh_navigation_controls()
        current = self._navigation.current
        if current is not None and current in self._folder_items:
            self._tree.setCurrentItem(self._folder_items[current])

    def _refresh_navigation_controls(self) -> None:
        if not hasattr(self, "_nav_back_btn"):
            return
        self._refresh_navigation_arrow_direction()
        self._nav_back_btn.setEnabled(self._navigation.can_go_back)
        self._nav_forward_btn.setEnabled(self._navigation.can_go_forward)
        self._nav_up_btn.setEnabled(self._navigation.can_go_up)
        root, current = self._navigation.root, self._navigation.current
        while self._breadcrumbs_layout.count():
            item = self._breadcrumbs_layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        if root is None or current is None:
            return
        folders = [root]
        if current != root:
            cursor = root
            for part in current.relative_to(root).parts:
                cursor /= part
                folders.append(cursor)
        for index, folder in enumerate(folders):
            if index:
                self._breadcrumbs_layout.addWidget(QLabel("›"))
            button = QToolButton()
            button.setText(folder.name or str(folder))
            button.setToolTip(str(folder))
            button.setMaximumWidth(160)
            button.setEnabled(folder != current)
            button.clicked.connect(lambda _=False, target=folder: self._navigate_breadcrumb(target))
            self._breadcrumbs_layout.addWidget(button)
        self._breadcrumbs_layout.addStretch()

    def _refresh_navigation_arrow_direction(self) -> None:
        """Back/Forward arrows are logical, not hard-coded physical directions."""
        rtl = QApplication.layoutDirection() == Qt.RightToLeft
        self._nav_back_btn.setText("→" if rtl else "←")
        self._nav_forward_btn.setText("←" if rtl else "→")

    def _navigate_breadcrumb(self, folder: Path) -> None:
        if folder.is_dir() and self._navigation.navigate(folder):
            self._apply_navigation_filter()

    # ── Table handlers ────────────────────────────────────────────────────────

    def _on_table_selection_changed(self, selected: QItemSelection, _desel) -> None:
        if self._is_filtering_view:
            return
        # A draft belongs to the immutable workspace IDs that were selected
        # when the user edited it. Commit that valid draft before changing the
        # canonical selection so it can never be redirected to the new rows.
        self._commit_inspector_draft()
        self._cancel_artwork_thumbnails()
        rows = self._table.selectionModel().selectedRows()
        self._workspace.set_selected_paths({
            track.path for idx in rows
            if (track := self._proxy.track_at_row(idx.row())) is not None
        })
        self._refresh_selection_scope_state()
        
        # Update the header 'Select All' checkbox
        hdr = self._table.horizontalHeader()
        if hasattr(hdr, 'setChecked'):
            total = self._proxy.rowCount()
            hdr.setChecked(len(rows) == total and total > 0)

        if rows:
            tracks = []
            for idx in rows:
                track = self._proxy.track_at_row(idx.row())
                if track is not None:
                    tracks.append(track)
            self._populate_track_inspector(tracks)
            self._inspector.setCurrentIndex(PAGE_TRACKS)
        elif self._model.get_all_tracks():
            # Default back to "apply to all checked" panel
            visible = self._proxy.rowCount()
            self._insp_folder_title.setText(t("meta_n_files_checked", n=visible))
            self._inspector.setCurrentIndex(PAGE_FOLDER)
        else:
            self._inspector.setCurrentIndex(PAGE_EMPTY)


    def _on_select_all_toggled(self, checked: bool) -> None:
        if checked:
            self._table.selectAll()
        else:
            self._table.clearSelection()
        self._refresh_selection_scope_state()

    def _on_model_data_changed(self, *_) -> None:
        self._update_summary()
        self._refresh_checked_scope_state()

    def _on_workspace_state_changed(self) -> None:
        if hasattr(self, "_apply_scope_lbl"):
            self._refresh_checked_scope_state()

    # ── Inspector handlers ────────────────────────────────────────────────────

    def _on_insp_folder_artist(self) -> None:
        artist = self._insp_folder_artist.text().strip()
        if not artist:
            return
        tracks = self._workspace.edit_scope()
        if not tracks:
            self._refresh_checked_scope_state()
            return
        self.artist_to_scope.emit(artist, tracks)

    def _on_insp_folder_album(self) -> None:
        album = self._insp_folder_album.text().strip()
        if not album:
            return
        tracks = self._workspace.edit_scope()
        if not tracks:
            self._refresh_checked_scope_state()
            return
        self.album_to_scope.emit(album, tracks)

    def _on_insp_apply_fields(self) -> None:
        self._commit_inspector_draft(show_invalid=True)
        tracks = self._get_selected_tracks()
        if not tracks:
            self._refresh_selection_scope_state()
            return
        self._model.refresh_all()
        self._update_summary()
        self._populate_track_inspector(tracks)

    def _commit_inspector_draft(self, *, show_invalid: bool = False) -> bool:
        """Commit the exact ID-bound Inspector draft into proposals.

        Returns False when a value is invalid. Invalid drafts remain bound to
        their original item IDs and are never applied to a later selection.
        """
        if not self._insp_field_dirty or self._insp_draft_item_ids is None:
            return True
        tracks = [
            track for identity in self._insp_draft_item_ids
            if (track := self._workspace.track_for_id(identity)) is not None
        ]
        if not tracks:
            self._discard_inspector_draft()
            return True
        affected = unsupported = 0
        invalid: list[str] = []
        completed: set[str] = set()
        for field_name in tuple(self._insp_field_dirty):
            value = self._insp_draft_values[field_name]
            try:
                result = (
                    self._inspector_state.propose_clear(tracks, field_name)
                    if value == ""
                    else self._inspector_state.propose_set(tracks, field_name, value)
                )
            except (TypeError, ValueError):
                invalid.append(field_name)
                continue
            affected += result.affected_count
            unsupported += result.unsupported_count
            completed.add(field_name)
        self._insp_field_dirty.difference_update(completed)
        for field_name in completed:
            self._insp_draft_values.pop(field_name, None)
        if invalid and show_invalid:
            show_warning(
                self.window(),
                t("meta_inspector_invalid_value_title"),
                t("meta_inspector_invalid_value_body", fields=", ".join(invalid)),
            )
        elif unsupported:
            self._insp_capability.setText(
                t("meta_inspector_partial_scope", affected=affected, unsupported=unsupported)
            )
        if not self._insp_field_dirty:
            self._insp_draft_item_ids = None
            self._insp_draft_values.clear()
        if completed:
            self._workspace.capture_proposals(tracks, ChangeOrigin.MANUAL, label="inspector edit")
            self._model.refresh_all()
            self._update_summary()
        return not invalid

    def _mark_insp_field_dirty(self, field_name: str, value: str) -> None:
        if self._insp_populating:
            return
        current_ids = tuple(sorted(
            self._workspace.item_id(track)
            for track in self._workspace.selected_tracks()
        ))
        if not current_ids:
            return
        if self._insp_draft_item_ids not in (None, current_ids):
            # A late signal from controls belonging to an older selection must
            # not create a draft for the current one.
            return
        self._insp_draft_item_ids = current_ids
        self._insp_field_dirty.add(field_name)
        self._insp_draft_values[field_name] = value

    def _clear_insp_field(self, field_name: str, edit: QLineEdit) -> None:
        self._mark_insp_field_dirty(field_name, "")
        edit.clear()

    def _discard_inspector_draft(self) -> None:
        self._insp_field_dirty.clear()
        self._insp_draft_values.clear()
        self._insp_draft_item_ids = None

    def _on_lyrics_text_changed(self) -> None:
        if not getattr(self, "_lyrics_refreshing", False):
            self._lyrics_dirty = True

    def _on_lyrics_propose_set(self) -> None:
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        text = self._insp_lyrics.toPlainText()
        # Only a single ID3-backed selection exposes language/descriptor
        # controls. Batch replacement changes the text while preserving each
        # file's own primary language, descriptor, and container-local source.
        value = (
            LyricsEntry(
                text=text,
                language=self._insp_lyrics_language.text().strip() or "und",
                description=self._insp_lyrics_description.text(),
            )
            if len(tracks) == 1 and tracks[0].format_id in {"mp3", "wav"}
            else text
        )
        result = self._inspector_state.propose_set(tracks, LYRICS_FIELD, value)
        self._lyrics_dirty = False
        self._after_inspector_proposal(tracks, result, ChangeOrigin.LYRICS)

    def _on_lyrics_propose_clear(self) -> None:
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        result = self._inspector_state.propose_clear(tracks, LYRICS_FIELD)
        self._lyrics_dirty = False
        self._after_inspector_proposal(tracks, result, ChangeOrigin.LYRICS)

    def _on_lyrics_revert(self) -> None:
        tracks = self._get_selected_tracks()
        self._inspector_state.revert(tracks, {LYRICS_FIELD})
        self._after_inspector_proposal(tracks, origin=ChangeOrigin.LYRICS)

    def _on_artwork_add_choose(self) -> None:
        self._on_artwork_choose(add=True)

    def _on_artwork_replace_choose(self) -> None:
        self._on_artwork_choose(add=False)

    def _on_artwork_choose(self, *, add: bool) -> None:
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        name, _ = QFileDialog.getOpenFileName(self, t("meta_artwork_choose_title"), "", "Images (*.jpg *.jpeg *.png)")
        if not name:
            return
        self._propose_artwork_file(Path(name), tracks, add=add)

    def _propose_artwork_file(self, path: Path, tracks, *, add: bool = False) -> None:
        from core.artwork import ArtworkValidationError, load_artwork_file
        try:
            entry = load_artwork_file(path)
        except ArtworkValidationError as exc:
            show_warning(self.window(), t("meta_inspector_artwork_section"), t(exc.key))
            return
        result = (self._inspector_state.propose_add_artwork(tracks, entry)
                  if add else self._inspector_state.propose_set(tracks, ARTWORK_FIELD, entry))
        self._after_inspector_proposal(
            tracks, result, ChangeOrigin.ARTWORK_ADD if add else ChangeOrigin.ARTWORK_REPLACE,
        )

    def _on_artwork_drop(self, path: Path) -> None:
        tracks = self._get_selected_tracks()
        if tracks:
            self._propose_artwork_file(path, tracks, add=False)

    def _on_artwork_remove(self) -> None:
        tracks = self._get_selected_tracks()
        if tracks:
            self._after_inspector_proposal(tracks, self._inspector_state.propose_clear(tracks, ARTWORK_FIELD), ChangeOrigin.ARTWORK_REMOVE)

    def _on_artwork_revert(self) -> None:
        tracks = self._get_selected_tracks()
        self._inspector_state.revert(tracks, {ARTWORK_FIELD})
        self._after_inspector_proposal(tracks, origin=ChangeOrigin.ARTWORK_REMOVE)

    def _on_artwork_paste(self) -> None:
        tracks = self._get_selected_tracks()
        image = QApplication.clipboard().image() if tracks else QPixmap()
        if image.isNull():
            show_warning(self.window(), t("meta_inspector_artwork_section"), t("meta_artwork_invalid_image"))
            return
        payload = QByteArray(); buffer = QBuffer(payload); buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "PNG"); buffer.close()
        from core.artwork import ArtworkValidationError, validate_artwork_bytes
        try:
            entry = validate_artwork_bytes(bytes(payload))
        except ArtworkValidationError as exc:
            show_warning(self.window(), t("meta_inspector_artwork_section"), t(exc.key)); return
        self._after_inspector_proposal(tracks, self._inspector_state.propose_set(tracks, ARTWORK_FIELD, entry), ChangeOrigin.ARTWORK_REPLACE)

    def _on_artwork_export(self) -> None:
        tracks = self._get_selected_tracks()
        if len(tracks) != 1 or not tracks[0].original.artwork.primary:
            return
        destination = QFileDialog.getExistingDirectory(self, t("meta_artwork_export_title"))
        if not destination:
            return
        from core.artwork import ArtworkValidationError, export_artwork_entries
        try:
            export_artwork_entries(Path(destination), tracks[0].path.stem, tracks[0].original.artwork.entries)
        except ArtworkValidationError as exc:
            show_warning(self.window(), t("meta_artwork_export_title"), t(exc.key))

    def _on_replaygain_clear_track(self) -> None:
        self._propose_replaygain_clear({REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK})

    def _on_replaygain_clear_album(self) -> None:
        self._propose_replaygain_clear({REPLAYGAIN_ALBUM_GAIN, REPLAYGAIN_ALBUM_PEAK})

    def _propose_replaygain_clear(self, fields: set[str]) -> None:
        tracks = self._get_selected_tracks()
        affected = unsupported = 0
        for field_name in fields:
            result = self._inspector_state.propose_clear(tracks, field_name)
            affected += result.affected_count
            unsupported += result.unsupported_count
        self._after_inspector_proposal(tracks, origin=ChangeOrigin.REPLAYGAIN)
        if unsupported:
            self._insp_capability.setText(
                t("meta_inspector_partial_scope", affected=affected, unsupported=unsupported)
            )

    def _on_replaygain_revert(self) -> None:
        tracks = self._get_selected_tracks()
        self._inspector_state.revert(tracks, REPLAYGAIN_FIELDS)
        self._after_inspector_proposal(tracks, origin=ChangeOrigin.REPLAYGAIN)

    def _on_replaygain_track(self) -> None:
        tracks = self._get_selected_tracks()
        if tracks:
            self.replaygain_track_requested.emit(tracks)

    def _on_replaygain_album(self) -> None:
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        from core.replay_gain import group_album_scope
        groups = group_album_scope(tracks, item_id=self._workspace.item_id)
        ambiguous = sum(group.ambiguous for group in groups)
        included = [track for group in groups for track in group.tracks]
        details = "\n".join(
            t(
                "meta_replaygain_album_group_ambiguous"
                if group.ambiguous else "meta_replaygain_album_group_safe"
            )
            + " "
            + "\n".join(f"  \u2068{track.path.name}\u2069" for track in group.tracks)
            for group in groups
        )
        if confirm(
            self.window(),
            t("meta_replaygain_album_confirm_title"),
            t(
                "meta_replaygain_album_confirm_body",
                files=len(included), groups=len(groups), ambiguous=ambiguous,
            ),
            accept_text=t("meta_replaygain_analyze_album"),
            cancel_text=t("cancel_btn"),
            details=details,
        ):
            self.replaygain_album_requested.emit(included)

    def _after_inspector_proposal(self, tracks, result=None, origin=ChangeOrigin.MANUAL) -> None:
        self._workspace.capture_proposals(list(tracks), origin)
        self._model.refresh_all()
        self._update_summary()
        self._refresh_checked_scope_state()
        if tracks:
            self._populate_track_inspector(tracks)
        if result is not None and result.unsupported_count:
            self._insp_capability.setText(t(
                "meta_inspector_partial_scope",
                affected=result.affected_count,
                unsupported=result.unsupported_count,
            ))

    def _on_insp_rename_from_title(self) -> None:
        tracks = self._get_selected_tracks()
        if not tracks:
            self._refresh_selection_scope_state()
            return
        self.rename_from_title.emit(tracks)

    def _on_auto_arrange_settings(self) -> None:
        dlg = AutoArrangeSettingsDialog(self._auto_ops, self)
        if dlg.exec():
            self._auto_ops = dlg.result_ops
            if self._cfg:
                self._cfg.magic_auto_ops = list(self._auto_ops)
                self._cfg.save()

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

    # ── Public slots (wired by AppWindow) ─────────────────────────────────────

    def on_track_discovered(self, item: AudioTrackItem) -> None:
        self.on_tracks_discovered([item])

    def on_tracks_discovered(self, items: list[AudioTrackItem]) -> None:
        if self._is_scanning:
            return
        self._model.add_tracks(items)
        self._add_many_to_tree(items)
        self._update_summary()

    def on_scan_progress(self, done: int, total: int) -> None:
        if not self._is_scanning:
            self._set_scan_loading(True)

        if total <= 0:
            self._scan_progress.setRange(0, 0)
            self._scan_progress.setAccessibleDescription(t("meta_scanning"))
            if hasattr(self, "_center_progress"):
                self._center_progress.setRange(0, 0)
            self._summary_lbl.setText(t("meta_scanning"))
            return

        self._scan_progress.setRange(0, total)
        self._scan_progress.setValue(min(done, total))
        progress_text = t("meta_scanning_progress", done=done, total=total)
        self._scan_progress.setAccessibleDescription(progress_text)
        if hasattr(self, "_center_progress"):
            self._center_progress.setRange(0, total)
            self._center_progress.setValue(min(done, total))
        self._summary_lbl.setText(progress_text)

    def on_scan_error(self, msg: str) -> None:
        self._set_scan_loading(False)
        self._summary_lbl.setText(t("md_scan_error", msg=msg))

    def on_scan_complete(self, result: ScanResult) -> None:
        n = result.files_count
        if self._navigation.root != result.root:
            self._root_folder = result.root
            self._navigation.set_root(result.root)
        self._file_operations.set_root(result.root)
        if self._model.total_count() != n:
            self._model.load_tracks(result.tracks)
            self._tree.clear()
            self._folder_items.clear()
            self._file_items.clear()
            self._add_many_to_tree(result.tracks)

        was_blocked = self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        self._ignore_tree_changes = True
        try:
            for folder in sorted(result.folder_set, key=lambda p: (len(p.parts), str(p).lower())):
                self._get_or_create_folder_item(folder)
        finally:
            self._ignore_tree_changes = False
            self._tree.blockSignals(was_blocked)
            self._tree.setUpdatesEnabled(True)

        self._dupes_btn.setEnabled(True)
        self._apply_navigation_filter()
        self._update_summary()
        self._refresh_checked_scope_state()
        self._set_scan_loading(False)

        if self._tree.topLevelItemCount() > 0:
            self._tree.topLevelItem(0).setExpanded(True)

        if n > 0:
            self._insp_folder_title.setText(t("meta_inspector_no_selection_title"))
            self._inspector.setCurrentIndex(PAGE_FOLDER)

    def on_auto_rules_applied(self) -> None:
        self._model.refresh_all()
        self._table.viewport().update()
        self._update_summary()
        self._refresh_checked_scope_state()
        tracks = self._get_selected_tracks()
        if tracks:
            self._populate_track_inspector(tracks)

    def on_monitoring_state_changed(self, state, diagnostic: str = "") -> None:
        value = getattr(state, "value", str(state))
        self._monitoring_status.setText(t(f"meta_monitoring_{value}"))
        self._monitoring_status.setToolTip(
            t("meta_monitoring_diagnostic", detail=diagnostic)
            if diagnostic else t("meta_monitoring_status_tooltip"))
        self._manual_refresh_btn.setEnabled(
            self._root_folder is not None and value != "disabled")

    def on_external_changes_updated(self, count: int) -> None:
        self._set_stale_chip_count(int(count))
        self._refresh_checked_scope_state()

    def _set_stale_chip_count(self, count: int) -> None:
        """Keep the chip's label, accessible name and visibility on one count."""
        label = t("meta_external_filter", n=int(count))
        self._stale_chip.setText(label)
        self._stale_chip.setAccessibleName(label)
        self._stale_chip.setVisible(bool(count) or self._stale_chip.isChecked())

    def on_workspace_refresh_applied(self, payload) -> None:
        folders = set(payload.get("folders", ())) if isinstance(payload, dict) else set()
        loaded = {item.path: item for item in self._workspace.tracks}
        was_blocked = self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        try:
            for path in list(self._file_items):
                if path in loaded:
                    continue
                tree_item = self._file_items.pop(path)
                parent = tree_item.parent()
                if parent is not None:
                    parent.removeChild(tree_item)
            self._add_many_to_tree([
                item for path, item in loaded.items() if path not in self._file_items])
            for folder in sorted(
                    folders, key=lambda path: (len(path.parts), str(path).casefold())):
                self._get_or_create_folder_item(folder)
            for folder in sorted(list(self._folder_items),
                                 key=lambda path: len(path.parts), reverse=True):
                if folder == self._root_folder or folder in folders or folder.is_dir():
                    continue
                tree_item = self._folder_items.pop(folder)
                parent = tree_item.parent()
                if parent is not None:
                    parent.removeChild(tree_item)
        finally:
            self._tree.blockSignals(was_blocked)
            self._tree.setUpdatesEnabled(True)
        self._navigation.reconcile_filesystem()
        self._apply_navigation_filter()
        self._update_summary()
        selected = self._workspace.selected_tracks()
        if selected:
            identity = self._workspace.item_id(selected[0])
            record_like = type("RefreshSelection", (), {"item_id": identity})()
            self._navigate_review_record(record_like)

    def on_conflict_resolution_finished(self, summary) -> None:
        if summary is not None and not summary.accepted:
            show_warning(
                self, t("meta_external_resolution_failed_title"),
                t("meta_external_resolution_failed", detail=summary.diagnostic))
        self._update_summary()

    def on_apply_started(self) -> None:
        self._set_apply_loading(True)

    def on_apply_progress(self, done: int, total: int) -> None:
        if not self._is_applying:
            self.on_apply_started()
        if total > 0:
            self._scan_progress.setRange(0, total)
            self._scan_progress.setValue(min(done, total))
            if hasattr(self, "_center_progress"):
                self._center_progress.setRange(0, total)
                self._center_progress.setValue(min(done, total))
        else:
            self._scan_progress.setRange(0, 0)
            if hasattr(self, "_center_progress"):
                self._center_progress.setRange(0, 0)
        self._summary_lbl.setText(t("meta_writing_tags_progress", done=done, total=total))

    def on_apply_file_outcome(self, outcome) -> None:
        """Incrementally refresh one row from a structured ApplyOutcome."""
        from core.metadata_models import ApplyStatus

        path = outcome.original_path
        item = self._model.track_for_path(path)
        if item is None:
            return

        self._apply_refresh_counter += 1
        renamed = outcome.final_path != outcome.original_path
        success = outcome.status in (ApplyStatus.SUCCESS, ApplyStatus.PARTIAL)
        should_refresh = renamed or not success or self._apply_refresh_counter % 50 == 0
        if not should_refresh:
            return

        if self._model.refresh_path(path) and renamed:
            tree_item = self._file_items.pop(path, None)
            if tree_item is not None:
                tree_item.setText(0, item.display_name)
                tree_item.setData(0, Qt.UserRole, item.path)
                tree_item.setToolTip(0, str(item.path))
                tree_item.setIcon(0, self._track_icon(item))
                self._file_items[item.path] = tree_item

    def on_apply_error(self, message: str) -> None:
        """A batch-level abort (backup/preflight failure) — nothing was written."""
        self._set_apply_loading(False)
        self._update_summary()
        self._summary_lbl.setText(message)
        self._refresh_checked_scope_state()
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(
                title=t("meta_apply_blocked_title"), content=message, parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT, duration=8000,
            )
        except Exception:
            logger.debug("InfoBar notification failed", exc_info=True)

    def on_apply_batch_complete(self, result) -> None:
        """Render the structured batch result (backup-blocked / partial / done)."""
        # A batch-level abort (or a recovery-required stop) is surfaced through
        # on_apply_error; here just clear the loading state and refresh the model
        # so any files already written on disk show their new values.
        if result.aborted:
            self._set_apply_loading(False)
            self._refresh_checked_scope_state()
            return
        if getattr(result, "recovery_required", False):
            self._set_apply_loading(False)
            self._model.refresh_all()
            self._table.viewport().update()
            self._refresh_checked_scope_state()
            return

        self._set_apply_loading(False)
        self._artwork_thumbnail_cache.clear()
        self._cancel_artwork_thumbnails()
        self._update_summary()
        self._model.refresh_all()
        self._table.viewport().update()

        # Update right-side inspector to reflect the new values (proposed cleared)
        rows = self._table.selectionModel().selectedRows()
        if rows:
            tracks = []
            for idx in rows:
                track = self._proxy.track_at_row(idx.row())
                if track is not None:
                    tracks.append(track)
            self._populate_track_inspector(tracks)

        success = result.success_count
        partial = result.partial_count
        fail    = result.failed_count
        skip    = result.skipped_count + result.cancelled_count

        msg = t("meta_done_success_base", success=success)
        if partial:
            msg += t("meta_done_partial_suffix", partial=partial)
        if fail:
            msg += t("meta_done_failed_suffix", fail=fail)
        if skip:
            msg += t("meta_done_skipped_suffix", skip=skip)
        self._summary_lbl.setText(msg)
        self._refresh_checked_scope_state()

        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            if fail == 0 and partial == 0:
                InfoBar.success(
                    title=t("meta_done_summary_title"), content=msg, parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT, duration=4000,
                )
            else:
                InfoBar.warning(
                    title=t("meta_done_with_errors_title"), content=msg, parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT, duration=6000,
                )
        except Exception:
            # Toast is a nice-to-have; the summary label above already shows
            # the result, so a themed-InfoBar failure must not crash apply.
            logger.debug("InfoBar notification failed", exc_info=True)

    def on_recovery_available(self, summary: dict) -> None:
        """Offer review-first recovery of a crashed Apply operation (TE-SAFE-11).

        Three clear choices with honest semantics (defect 5):
          • Restore from backup — rename files back + restore original tags.
          • Not now — keep the journal so it is offered again next launch.
          • Forget — permanently delete the journal (destructive, reconfirmed).
        """
        from PySide6.QtWidgets import QMessageBox

        incomplete = summary.get("incomplete", 0)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("md_recovery_prompt_title"))
        box.setText(t("md_recovery_prompt_msg", n=incomplete))
        details = [
            f"{t('meta_backup_operation')}: {summary.get('operation_type', 'unknown')}",
            f"{t('meta_backup_created')}: {summary.get('created', '')}",
            f"{t('meta_backup_files')}: {summary.get('affected_files', summary.get('total', 0))}",
            f"{t('meta_backup_validity')}: {summary.get('backup_status', 'unknown')} / {summary.get('manifest_status', 'unknown')}",
            f"Disk: {summary.get('current_disk_state', 'unknown')}",
            f"Completed: {', '.join(summary.get('completed_stages', []))}",
            f"Pending: {', '.join(summary.get('pending_stages', []))}",
            f"Recommended: {summary.get('recommended_action', 'inspect')}",
        ]
        details.extend(
            f"{item.get('path')}: {item.get('journal_state')} / {item.get('disk_state')} / {item.get('uncertainty')}"
            for item in summary.get("files", [])
        )
        box.setDetailedText("\n".join(details))
        action_key = ("md_recovery_reconcile_btn"
                      if summary.get("recommended_action") == "reconcile"
                      else "md_recovery_restore_btn")
        restore_btn = box.addButton(t(action_key),
                                    QMessageBox.ButtonRole.AcceptRole)
        keep_btn = box.addButton(t("md_recovery_notnow_btn"),
                                 QMessageBox.ButtonRole.RejectRole)
        forget_btn = box.addButton(t("md_recovery_forget_btn"),
                                   QMessageBox.ButtonRole.DestructiveRole)
        restore_btn.setEnabled(not summary.get("malformed", False)
                               and summary.get("backup_status") == "verified")
        forget_btn.setEnabled(bool(summary.get("discard_allowed", False)))
        box.setDefaultButton(keep_btn)
        box.exec()
        clicked = box.clickedButton()

        if clicked is restore_btn:
            self.recover_requested.emit(summary)
        elif clicked is forget_btn:
            # Destructive: require an explicit second confirmation.
            if confirm(
                self, t("md_recovery_forget_title"), t("md_recovery_forget_msg"),
                accept_text=t("md_recovery_forget_btn"), danger=True,
            ):
                self.forget_recovery_requested.emit(summary)
            else:
                self.keep_recovery_requested.emit(summary)
        else:
            # 'Not now' or dialog closed → keep the journal for later.
            self.keep_recovery_requested.emit(summary)

    def on_status_update(self, msg: str) -> None:
        self._summary_lbl.setText(msg)

    def on_draft_available(self, info: dict) -> None:
        """Offer proposal-only recovery; leaving the dialog keeps the draft."""
        from PySide6.QtWidgets import QMessageBox
        root = Path(str(info.get("root") or ""))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("meta_draft_available_title"))
        box.setText(t("meta_draft_available_message", n=info.get("affected_files", 0),
                      root=str(root), age=info.get("created", "")))
        # A legacy draft we refused to merge or discard is still the user's work:
        # tell them it exists and where, in the one dialog they are already
        # answering about drafts (F-13).
        notice = info.get("migration_notice")
        if notice == "conflict_preserved":
            box.setInformativeText(t("meta_draft_legacy_conflict",
                                     path=str(info.get("migration_preserved_copy", ""))))
        elif notice == "failed":
            box.setInformativeText(t("meta_draft_migration_failed"))
        restore = box.addButton(t("meta_draft_restore"), QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton(t("meta_draft_discard"), QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton(t("meta_draft_keep"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is restore:
            self.draft_restore_requested.emit(info)
        elif box.clickedButton() is discard:
            self.draft_discard_requested.emit()

    def on_unsaved_changes_action_required(self, request) -> None:
        """Offer the complete proposal lifecycle choice before continuing."""
        from PySide6.QtWidgets import QMessageBox
        operation = request.get("operation", "operation") if isinstance(request, dict) else str(request)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("meta_draft_unsaved_title"))
        box.setText(t("meta_draft_unsaved_message", operation=operation))
        apply_btn = box.addButton(t("meta_draft_apply"), QMessageBox.ButtonRole.AcceptRole)
        keep_btn = box.addButton(t("meta_draft_keep_action"), QMessageBox.ButtonRole.ActionRole)
        discard_btn = box.addButton(t("meta_draft_discard"), QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(t("meta_cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is apply_btn:
            # Lifecycle Apply uses the same visible Review Changes surface and
            # then the normal immutable Apply controller path.
            self._on_review_changes()
            self.unsaved_choice_requested.emit("apply")
        elif clicked is keep_btn:
            self.unsaved_choice_requested.emit("keep_draft")
        elif clicked is discard_btn:
            self.unsaved_choice_requested.emit("discard")
        else:
            self.unsaved_choice_requested.emit("cancel")

    def on_duplicate_scan_progress(self, done: int, total: int, eta: str) -> None:
        self._summary_lbl.setText(t("meta_searching_duplicates_progress", done=done, total=total, eta=eta))

    def on_duplicate_scan_complete(self, groups, elapsed: float, strategy: str) -> None:
        self._dupes_btn.setEnabled(True)
        if getattr(groups, "cancelled", False):
            self.on_status_update(t("meta_problems_cancelled"))
            self._update_summary()
            return
        if hasattr(groups, "groups"):
            if not groups.groups:
                self.on_status_update(t("meta_no_duplicates_found", elapsed=elapsed))
                self._update_summary()
                return
        elif not groups:
            self.on_status_update(t("meta_no_duplicates_found", elapsed=elapsed))
            self._update_summary()
            return

        from ui.dialogs.duplicate_files_dialog import DuplicateFilesDialog
        dlg = DuplicateFilesDialog(groups, elapsed, strategy, self._root_folder, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.files_to_delete:
            self.delete_duplicates_requested.emit(dlg.files_to_delete)

    def on_duplicate_scan_error(self, msg: str) -> None:
        self._dupes_btn.setEnabled(True)
        self.on_status_update(t("meta_duplicate_search_error", msg=msg))
        self._update_summary()

    def on_duplicate_delete_complete(self, success: int, fail: int) -> None:
        note = t("meta_files_deleted_errors_suffix", fail=fail) if fail else ""
        self.on_status_update(t("meta_files_deleted", success=success, note=note))
        self._on_scan()   # trigger full folder rescan → refreshes tree and table

    # ── Tree construction helpers ─────────────────────────────────────────────

    def _make_audio_icon(self) -> QIcon:
        pix = QPixmap(18, 18)
        pix.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#27d3c4"))
        painter.drawRect(2, 2, 14, 14)

        font = QFont()
        font.setBold(True)
        font.setPointSize(12)
        painter.setFont(font)
        painter.setPen(QColor("#061a1c"))
        painter.drawText(pix.rect().adjusted(0, -1, 0, 0), Qt.AlignmentFlag.AlignCenter, "♪")
        painter.end()

        return QIcon(pix)

    def _folder_icon(self, folder: Path) -> QIcon:
        icon = self._icon_provider.icon(QFileInfo(str(folder)))
        if not icon.isNull():
            return icon
        return self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)

    def _track_icon(self, item: AudioTrackItem) -> QIcon:
        if item.status == TrackStatus.UNSUPPORTED:
            return self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        if item.status == TrackStatus.ERROR:
            return self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical)

        suffix = (item.path.suffix or item.ext).lower()
        cache_key = f"ext:{suffix}" if suffix else f"path:{item.path.name.casefold()}"
        cached = self._track_icon_cache.get(cache_key)
        if cached is not None:
            return cached

        icon = self._icon_provider.icon(QFileInfo(str(item.path)))
        if not icon.isNull():
            self._track_icon_cache[cache_key] = icon
            return icon
        self._track_icon_cache[cache_key] = self._audio_icon
        return self._audio_icon

    def _ensure_root_item(self) -> QTreeWidgetItem:
        if not self._root_folder:
            raise RuntimeError("Cannot build tree before root folder is selected")

        if self._tree.topLevelItemCount() > 0:
            root_item = self._tree.topLevelItem(0)
            self._folder_items.setdefault(self._root_folder, root_item)
            return root_item

        root_item = QTreeWidgetItem([self._root_folder.name])
        root_item.setIcon(0, self._folder_icon(self._root_folder))
        root_item.setData(0, Qt.UserRole, self._root_folder)
        root_item.setData(0, self._ROLE_IS_FILE, False)
        root_item.setFont(0, bold_font())
        root_item.setFlags(
            Qt.ItemIsEnabled | Qt.ItemIsSelectable |
            Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate |
            Qt.ItemIsDropEnabled
        )
        root_item.setCheckState(0, Qt.Checked)
        self._tree.addTopLevelItem(root_item)
        self._folder_items[self._root_folder] = root_item
        return root_item

    def _get_or_create_folder_item(self, folder: Path) -> QTreeWidgetItem:
        """Recursively ensure a tree item exists for folder and all its ancestors."""
        if folder in self._folder_items:
            return self._folder_items[folder]

        if self._root_folder and folder == self._root_folder:
            return self._ensure_root_item()

        parent = folder.parent
        if parent == folder:
            # Reached filesystem root — safety guard, return top-level item
            return self._ensure_root_item()

        parent_item = self._get_or_create_folder_item(parent)

        folder_item = QTreeWidgetItem([folder.name])
        folder_item.setIcon(0, self._folder_icon(folder))
        folder_item.setData(0, Qt.UserRole, folder)
        folder_item.setData(0, self._ROLE_IS_FILE, False)
        folder_item.setFlags(
            Qt.ItemIsEnabled | Qt.ItemIsSelectable |
            Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate |
            Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
        )
        folder_item.setCheckState(0, Qt.Checked)
        parent_item.addChild(folder_item)
        self._folder_items[folder] = folder_item
        return folder_item

    def _propagate_check_state(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        """Recursively apply check state to all descendant items and sync model."""
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            if child.data(0, self._ROLE_IS_FILE):
                path = child.data(0, Qt.UserRole)
                if path:
                    self._proxy.set_path_visible(path, state == Qt.Checked)
            else:
                self._propagate_check_state(child, state)

    def _add_to_tree(self, item: AudioTrackItem) -> None:
        """Add a discovered audio file to the tree, creating all ancestor folders."""
        self._add_many_to_tree([item])

    def _add_many_to_tree(self, items: list[AudioTrackItem]) -> None:
        """Add discovered audio files to the tree with painting/signals batched."""
        if not self._root_folder or not items:
            return

        was_blocked = self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        self._ignore_tree_changes = True
        try:
            self._ensure_root_item()
            for item in items:
                folder_item = self._get_or_create_folder_item(item.folder)
                if item.path in self._file_items:
                    continue

                file_item = QTreeWidgetItem([item.display_name])
                file_item.setIcon(0, self._track_icon(item))
                file_item.setData(0, Qt.UserRole, item.path)
                file_item.setData(0, self._ROLE_IS_FILE, True)
                file_item.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsSelectable |
                    Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled |
                    Qt.ItemIsDropEnabled
                )
                file_item.setCheckState(0, Qt.Checked)
                file_item.setToolTip(0, str(item.path))
                folder_item.addChild(file_item)
                self._file_items[item.path] = file_item
        finally:
            self._ignore_tree_changes = False
            self._tree.blockSignals(was_blocked)
            self._tree.setUpdatesEnabled(True)

    # ── Summary / info helpers ────────────────────────────────────────────────

    def _set_scan_loading(self, loading: bool) -> None:
        self._is_scanning = loading
        self._scan_progress.setVisible(False)
        if loading:
            self._scan_progress.setRange(0, 0)
            self._scan_progress.setValue(0)
            self._show_table_loading(t("meta_loading_scanning_title"), t("meta_loading_scanning_body"))
        else:
            self._scan_progress.setRange(0, 1)
            self._scan_progress.setValue(0)
            if self._model.get_all_tracks():
                self._show_table_content()
            else:
                self._show_table_empty()
        if hasattr(self, "_auto_btn"):
            self._refresh_checked_scope_state()

    def _set_apply_loading(self, loading: bool) -> None:
        self._is_applying = loading
        self._scan_progress.setVisible(False)
        if loading:
            self._scan_progress.setRange(0, 0)
            self._scan_progress.setValue(0)
            self._show_table_loading(t("meta_loading_apply_title"), t("meta_loading_apply_body"))
        elif not self._is_scanning:
            self._scan_progress.setRange(0, 1)
            self._scan_progress.setValue(0)
            if self._model.get_all_tracks():
                self._show_table_content()
            else:
                self._show_table_empty()
        if hasattr(self, "_apply_btn"):
            self._refresh_checked_scope_state()

    def _set_restore_loading(self, loading: bool) -> None:
        self._is_restoring = loading
        if loading:
            self._show_table_loading(t("meta_loading_restore_title"), t("meta_loading_restore_body"))
        elif not self._is_scanning and not self._is_applying:
            if self._model.get_all_tracks():
                self._show_table_content()
            else:
                self._show_table_empty()

    def _update_summary(self) -> None:
        tracks = self._model.get_all_tracks()
        folders = len(self._folder_items) if self._folder_items else len({tr.folder for tr in tracks})
        changed = self._model.get_changed_count()
        warnings = self._model.get_warning_count()
        parts = [
            t("meta_files_count", n=len(tracks)),
            t("meta_folders_count", n=folders),
        ]
        if changed:
            parts.append(t("meta_changes_proposed", n=changed))
        if warnings:
            parts.append(t("meta_warnings_count", n=warnings))
        self._summary_lbl.setText(" | ".join(parts) if parts else "")

    def _refresh_checked_scope_state(self) -> None:
        edit_tracks = self._workspace.edit_scope()
        edit_count = len(edit_tracks)
        has_edit_scope = edit_count > 0
        has_tracks = bool(self._model.get_all_tracks())
        has_changes = self._model.get_changed_count() > 0
        apply_candidates = self._workspace.apply_candidates()

        self._update_table_info()
        self._refresh_navigation_controls()

        for row in getattr(self, "_op_rows", []):
            row.setActionEnabled(has_edit_scope)
        for btn in getattr(self, "_checked_scope_buttons", []):
            btn.setEnabled(has_edit_scope)

        auto_enabled = (
            self._root_folder is not None
            and has_tracks
            and not self._is_scanning
            and not self._is_applying
            and not self._is_restoring
        )
        if hasattr(self, "_auto_container"):
            self._auto_container.setEnabled(auto_enabled)
        if hasattr(self, "_auto_btn"):
            self._auto_btn.setEnabled(auto_enabled)
        if hasattr(self, "_auto_cfg_btn"):
            self._auto_cfg_btn.setEnabled(auto_enabled)
        if hasattr(self, "_apply_btn"):
            blocked = self._review_blocked_records(self._workspace.change_set.records())
            self._apply_btn.setEnabled(
                bool(apply_candidates)
                and not blocked
                and not self._is_scanning
                and not self._is_applying
                and not self._is_restoring
            )
        if hasattr(self, "_revert_btn"):
            self._revert_btn.setEnabled(
                has_changes
                and not self._is_scanning
                and not self._is_applying
                and not self._is_restoring
            )
        if hasattr(self, "_undo_btn"):
            self._undo_btn.setEnabled(self._workspace.can_undo_proposals() and not self._is_applying)
        if hasattr(self, "_redo_btn"):
            self._redo_btn.setEnabled(self._workspace.can_redo_proposals() and not self._is_applying)
        if hasattr(self, "_review_btn"):
            self._review_btn.setEnabled(has_changes and not self._is_applying and not self._is_restoring)
        if hasattr(self, "_dupes_btn"):
            self._dupes_btn.setEnabled(self._root_folder is not None and not self._is_scanning)
        if hasattr(self, "_action_engine_btn"):
            self._action_engine_btn.setEnabled(has_tracks and not self._is_scanning and not self._is_applying)
        if hasattr(self, "_backup_manager_btn"):
            self._backup_manager_btn.setEnabled(not self._is_applying and not self._is_restoring)
        if hasattr(self, "_insp_folder_title"):
            self._insp_folder_title.setText(t("meta_inspector_no_selection_title"))
        if hasattr(self, "_browse_btn"):
            has_tracks = bool(self._model.get_all_tracks())
            self._browse_btn.setText(
                self._toolbar_text("meta_change_folder" if (self._root_folder and has_tracks) else "meta_browse_folder")
            )
        self._refresh_toolbar_action_styles()

    def _refresh_selection_scope_state(self) -> None:
        selected_count = len(self._get_selected_tracks()) if hasattr(self, "_table") else 0
        has_selection = bool(selected_count)
        if hasattr(self, "_online_scope_label"):
            self._online_scope_label.setText(t("meta_online_scope", n=selected_count))
        if hasattr(self, "_online_open_button"):
            self._online_open_button.setEnabled(has_selection)
        for btn in getattr(self, "_selection_scope_buttons", []):
            btn.setEnabled(has_selection)
        if getattr(self, "_replaygain_analysis_running", False):
            self._insp_rg_track_btn.setEnabled(False)
            self._insp_rg_album_btn.setEnabled(False)

    def _update_table_info(self) -> None:
        visible = self._proxy.rowCount()
        total   = len(self._model.get_all_tracks())
        if visible == total:
            self._table_info_lbl.setText(t("meta_total_files", total=total))
        else:
            self._table_info_lbl.setText(t("meta_showing_visible", visible=visible, total=total))
        candidates = self._workspace.apply_candidates()
        excluded = self._workspace.excluded_tracks()
        self._apply_scope_lbl.setText(t("meta_apply_scope_label", n=len(candidates)))
        excluded_label = t("meta_excluded_filter_chip", n=len(excluded))
        self._excluded_chip.setText(excluded_label)
        self._excluded_chip.setAccessibleName(excluded_label)
        self._excluded_chip.setVisible(bool(excluded) or self._excluded_chip.isChecked())
        stale = [item for item in self._workspace.tracks
                 if is_external_change(getattr(item, "external_state", "current"))]
        self._set_stale_chip_count(len(stale))
        selected_changed = [item for item in self._workspace.selected_tracks() if item.has_changes]
        all_excluded = bool(selected_changed) and all(item.excluded_from_apply for item in selected_changed)
        # This button flips between two meanings, so its accessible name has to
        # follow its label rather than be set once at construction.
        exclude_label = t("meta_include_in_apply" if all_excluded else "meta_exclude_from_apply")
        self._exclude_apply_btn.setText(exclude_label)
        self._exclude_apply_btn.setAccessibleName(exclude_label)
        self._exclude_apply_btn.setEnabled(bool(selected_changed))

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _get_selected_tracks(self) -> list[AudioTrackItem]:
        return self._workspace.selected_tracks()

    def _request_artwork_thumbnail(self, slot: str, entry, track_ids: tuple[int, ...]) -> None:
        """Request a bounded thumbnail; late results must match selection+hash."""
        self._artwork_thumbnail_generation += 1
        token = (self._workspace.generation, track_ids, entry.content_hash,
                 self._artwork_thumbnail_generation, slot)
        self._artwork_thumbnail_tokens[slot] = token
        target = self._insp_artwork_preview if slot == "current" else self._insp_artwork_proposed_preview
        cached = self._artwork_thumbnail_cache.get((entry.content_hash, 146))
        if cached is not None:
            pix = QPixmap(); pix.loadFromData(cached); target.setPixmap(pix); return
        target.setText(t("meta_artwork_loading"))
        worker = ArtworkThumbnailWorker(token, entry.data, 146, self)
        self._artwork_thumbnail_workers.add(worker)
        worker.ready.connect(self._on_artwork_thumbnail_ready)
        worker.failed.connect(self._on_artwork_thumbnail_failed)
        worker.finished.connect(lambda: self._artwork_thumbnail_workers.discard(worker))
        worker.start()

    def _on_artwork_thumbnail_ready(self, token, data: bytes) -> None:
        workspace_generation, track_ids, content_hash, _request, slot = token
        if self._artwork_thumbnail_tokens.get(slot) != token or workspace_generation != self._workspace.generation:
            return
        selected_ids = tuple(sorted(self._workspace.item_id(track) for track in self._get_selected_tracks()))
        if selected_ids != track_ids:
            return
        self._artwork_thumbnail_cache.put((content_hash, 146), data)
        pix = QPixmap(); pix.loadFromData(data)
        (self._insp_artwork_preview if slot == "current" else self._insp_artwork_proposed_preview).setPixmap(pix)

    def _on_artwork_thumbnail_failed(self, token, message_key: str) -> None:
        if self._artwork_thumbnail_tokens.get(token[-1]) == token:
            target = self._insp_artwork_preview if token[-1] == "current" else self._insp_artwork_proposed_preview
            target.setText(t(message_key))

    def _cancel_artwork_thumbnails(self) -> None:
        for worker in tuple(self._artwork_thumbnail_workers): worker.cancel()

    def shutdown_artwork_workers(self, timeout_ms: int = 5000) -> bool:
        """Cancel and join decoders; never drop the last live QThread reference."""
        from time import monotonic
        self._cancel_artwork_thumbnails()
        deadline = monotonic() + max(0, timeout_ms) / 1000
        for worker in tuple(self._artwork_thumbnail_workers):
            remaining = max(0, int((deadline - monotonic()) * 1000))
            if worker.isRunning() and not worker.wait(remaining):
                return False
        self._artwork_thumbnail_workers = {
            worker for worker in self._artwork_thumbnail_workers if worker.isRunning()
        }
        return not self._artwork_thumbnail_workers

    def _get_folder_tracks(self) -> list[AudioTrackItem]:
        return self._proxy.visible_tracks()

    def _toggle_selected_apply_exclusion(self) -> None:
        selected = [item for item in self._workspace.selected_tracks() if item.has_changes]
        if not selected:
            return
        exclude = not all(item.excluded_from_apply for item in selected)
        self._workspace.set_apply_excluded([item.path for item in selected], exclude)
        self._model.refresh_all()
        self._refresh_checked_scope_state()

    def _on_excluded_chip_toggled(self, enabled: bool) -> None:
        self._apply_display_filter(lambda: self._proxy.set_show_excluded_only(enabled))
        self._refresh_checked_scope_state()

    def _on_stale_chip_toggled(self, enabled: bool) -> None:
        self._apply_display_filter(lambda: self._proxy.set_show_stale_only(enabled))
        self._refresh_checked_scope_state()

    def _populate_track_inspector(self, tracks: list[AudioTrackItem]) -> None:
        track_ids = tuple(sorted(self._workspace.item_id(track) for track in tracks))
        preserve_draft = self._insp_draft_item_ids == track_ids
        self._insp_populating = True
        plural = "" if len(tracks) == 1 else "s"
        self._insp_tracks_title.setText(
            t("meta_tracks_selected_summary", n=len(tracks), plural=plural)
        )
        field_names = tuple(self._insp_fields) + (LYRICS_FIELD, ARTWORK_FIELD) + tuple(REPLAYGAIN_FIELDS)
        snapshot = self._inspector_state.snapshot(tracks, field_names)
        pending_total = sum(track.has_changes for track in tracks)
        if snapshot.editable_count == len(tracks):
            capability_text = t("meta_inspector_capability_all", n=len(tracks))
        elif snapshot.editable_count:
            capability_text = t(
                "meta_inspector_capability_some",
                supported=snapshot.editable_count,
                total=len(tracks),
            )
        else:
            capability_text = t("meta_inspector_capability_none")
        if pending_total:
            capability_text += " " + t("meta_inspector_pending_files", n=pending_total)
        self._insp_capability.setText(capability_text)

        for field_name, edit in self._insp_fields.items():
            state = snapshot.fields[field_name]
            if not preserve_draft or field_name not in self._insp_field_dirty:
                edit.blockSignals(True)
                try:
                    if state.value_state is ValueState.VALUE:
                        value = state.value
                        edit.setText("; ".join(value) if isinstance(value, tuple) else str(value))
                    else:
                        edit.clear()
                    edit.setPlaceholderText(
                        t("meta_mixed_placeholder")
                        if state.value_state is ValueState.MIXED
                        else t("meta_inspector_empty_value")
                    )
                finally:
                    edit.blockSignals(False)
            edit.setEnabled(state.supported_count > 0)
            self._insp_clear_buttons[field_name].setEnabled(state.supported_count > 0)
            edit.setStyleSheet(
                f"QLineEdit {{ border-bottom: 2px solid {get_colors().accent}; }}"
                if state.pending else ""
            )
            edit.setAccessibleDescription(
                t("meta_inspector_field_pending_tooltip") if state.pending else ""
            )
            if state.capability is CapabilityCoverage.SOME:
                edit.setToolTip(t(
                    "meta_inspector_field_partial_tooltip",
                    supported=state.supported_count,
                    total=state.total_count,
                ))
            elif state.capability is CapabilityCoverage.NONE:
                edit.setToolTip(t("meta_inspector_field_unsupported_tooltip"))
            else:
                edit.setToolTip(t("meta_inspector_field_pending_tooltip") if state.pending else "")

        self._insp_populating = False

        lyrics_state = snapshot.fields[LYRICS_FIELD]
        self._lyrics_refreshing = True
        try:
            if lyrics_state.value_state is ValueState.MIXED:
                lyrics_value = None
                self._insp_lyrics.clear()
                lyrics_status = t("meta_lyrics_mixed")
            elif lyrics_state.value_state is ValueState.EMPTY:
                lyrics_value = None
                self._insp_lyrics.clear()
                lyrics_status = t("meta_lyrics_none")
            else:
                lyrics_value = lyrics_state.value
                primary = lyrics_value.primary
                self._insp_lyrics.setPlainText(primary.text if primary else "")
                self._insp_lyrics_language.setText(primary.language if primary else "")
                self._insp_lyrics_description.setText(primary.description if primary else "")
                lyrics_status = t("meta_lyrics_present")
                if lyrics_value.secondary_count:
                    lyrics_status += " " + t("meta_lyrics_secondary_preserved", n=lyrics_value.secondary_count)
                if lyrics_value.has_synchronized:
                    lyrics_status += " " + t("meta_lyrics_synchronized_read_only")
                if any(track.format_id not in {"mp3", "wav"} for track in tracks):
                    lyrics_status += " " + t("meta_lyrics_language_not_supported")
            if lyrics_value is None:
                self._insp_lyrics_language.clear()
                self._insp_lyrics_description.clear()
            if lyrics_state.pending:
                lyrics_status += " " + t("meta_lyrics_pending")
            self._insp_lyrics_state.setText(lyrics_status)
            lyrics_enabled = lyrics_state.supported_count > 0
            self._insp_lyrics.setEnabled(lyrics_enabled)
            self._insp_lyrics_set_btn.setEnabled(lyrics_enabled)
            self._insp_lyrics_clear_btn.setEnabled(lyrics_enabled)
            self._insp_lyrics_revert_btn.setEnabled(lyrics_state.pending)
            details_enabled = (
                lyrics_enabled and len(tracks) == 1
                and tracks[0].format_id in {"mp3", "wav"}
            )
            self._insp_lyrics_language.setEnabled(details_enabled)
            self._insp_lyrics_description.setEnabled(details_enabled)
        finally:
            self._lyrics_refreshing = False

        artwork_state = snapshot.fields[ARTWORK_FIELD]
        stored_values = [track.original.artwork for track in tracks]
        common_stored = stored_values[0] if stored_values and all(
            stored_values[0].semantically_equal(value) for value in stored_values[1:]
        ) else None
        effective = artwork_state.value if artwork_state.value_state is ValueState.VALUE else None
        stored_primary = common_stored.primary if common_stored else None
        proposed_primary = effective.primary if effective else None
        if artwork_state.value_state is ValueState.MIXED:
            artwork_text = t("meta_artwork_mixed")
            self._insp_artwork_preview.setPixmap(QPixmap())
        elif stored_primary is None:
            artwork_text = t("meta_artwork_none")
            self._insp_artwork_preview.setPixmap(QPixmap())
        else:
            self._request_artwork_thumbnail("current", stored_primary, track_ids)
            artwork_text = t("meta_artwork_present", n=len(common_stored.entries))
            artwork_text += f" \u2066{stored_primary.mime_type} · {stored_primary.width}×{stored_primary.height}\u2069"
        if artwork_state.pending:
            artwork_text += " " + t("meta_artwork_pending")
            if proposed_primary:
                self._request_artwork_thumbnail("proposed", proposed_primary, track_ids)
            else:
                self._insp_artwork_proposed_preview.setPixmap(QPixmap())
                self._insp_artwork_proposed_preview.setText(t("meta_artwork_pending_removal"))
        else:
            self._insp_artwork_proposed_preview.clear()
        self._insp_artwork_proposed_label.setVisible(artwork_state.pending)
        self._insp_artwork_proposed_preview.setVisible(artwork_state.pending)
        if artwork_state.supported_count == 0:
            artwork_text += " " + t("meta_artwork_read_only")
        if common_stored and common_stored.diagnostics:
            artwork_text += " " + t("meta_artwork_invalid_image")
        self._insp_artwork_state.setText(artwork_text)
        editable_artwork = artwork_state.supported_count > 0
        can_add = editable_artwork and all(track.format_id != "m4a" for track in tracks)
        self._insp_artwork_add_btn.setEnabled(can_add)
        self._insp_artwork_replace_btn.setEnabled(editable_artwork)
        removable = any(track.original.artwork.entries or track.original.artwork.diagnostics for track in tracks)
        self._insp_artwork_remove_btn.setEnabled(editable_artwork and removable)
        self._insp_artwork_remove_btn.setText(t("meta_artwork_remove_all") if len(tracks) > 1 else t("meta_artwork_remove"))
        self._insp_artwork_paste_btn.setEnabled(editable_artwork)
        self._insp_artwork_export_btn.setEnabled(len(tracks) == 1 and stored_primary is not None)
        self._insp_artwork_revert_btn.setEnabled(artwork_state.pending)

        for field_name, label in self._insp_replay_values.items():
            state = snapshot.fields[field_name]
            if state.value_state is ValueState.MIXED:
                text = t("meta_inspector_mixed_value")
            elif state.value_state is ValueState.EMPTY:
                text = t("meta_inspector_empty_value")
            elif field_name in {REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_ALBUM_GAIN}:
                text = f"\u2066{float(state.value):+.2f} dB\u2069"
            elif field_name == REPLAYGAIN_REFERENCE_LOUDNESS:
                text = f"\u2066{float(state.value):.1f} dB\u2069"
            else:
                text = f"\u2066{float(state.value):.8f}\u2069"
            if state.pending:
                text += " " + t("meta_inspector_pending_marker")
            label.setText(text)

        replay_supported = snapshot.fields[REPLAYGAIN_TRACK_GAIN].supported_count > 0
        if not self._replaygain_analysis_running:
            self._insp_rg_track_btn.setEnabled(replay_supported)
            self._insp_rg_album_btn.setEnabled(replay_supported)
        self._insp_rg_clear_track_btn.setEnabled(replay_supported)
        self._insp_rg_clear_album_btn.setEnabled(replay_supported)
        self._insp_rg_revert_btn.setEnabled(
            any(snapshot.fields[field_name].pending for field_name in REPLAYGAIN_FIELDS)
        )

        if len(tracks) == 1:
            props = tracks[0].original.file_properties
            prop_lines = []
            for key, label_key in (
                ("filename", "meta_property_filename"),
                ("path", "meta_property_path"),
                ("format_id", "meta_property_format"),
                ("duration_seconds", "meta_property_duration"),
                ("bitrate", "meta_property_bitrate"),
                ("sample_rate", "meta_property_sample_rate"),
                ("channels", "meta_property_channels"),
                ("size_bytes", "meta_property_size"),
                ("modified_time", "meta_property_modified"),
            ):
                if key in props:
                    prop_lines.append(t(label_key) + ": " + self._format_property_value(key, props[key]))
            self._insp_properties.setText("\n".join(prop_lines) or t("meta_property_unavailable"))
            state = getattr(tracks[0], "external_state", "current")
            self._insp_external_status.setText(
                t("meta_external_inspector_status",
                  state=t(f"meta_external_state_{state}")))
            self._insp_external_review_btn.setVisible(
                getattr(tracks[0], "external_conflict", None) is not None)
        else:
            self._insp_properties.setText(t("meta_property_single_selection_only"))
            states = {getattr(track, "external_state", "current") for track in tracks}
            self._insp_external_status.setText(
                t("meta_external_multiple_states", n=len(states)))
            self._insp_external_review_btn.setVisible(False)

    def _review_selected_external_conflict(self) -> None:
        tracks = self._get_selected_tracks()
        if len(tracks) != 1 or tracks[0].external_conflict is None:
            return
        from core.file_refresh_service import ConflictResolutionAction
        from ui.dialogs.external_change_dialog import ExternalChangeReviewDialog
        conflict = tracks[0].external_conflict
        dialog = ExternalChangeReviewDialog(conflict, self)
        with a11y.focus_restored_after(self._insp_external_review_btn):
            accepted = dialog.exec() == QDialog.Accepted
        if not accepted:
            return
        target = None
        if dialog.selected_action is ConflictResolutionAction.LOCATE_MOVED:
            value, _selected = QFileDialog.getOpenFileName(
                self, t("meta_external_locate_moved"),
                str(self._root_folder or Path.home()), t("meta_audio_files"))
            if not value:
                return
            target = Path(value)
        self.conflict_resolution_requested.emit(
            conflict, dialog.selected_action.value, target)

    @staticmethod
    def _format_property_value(key: str, value: object) -> str:
        if key == "duration_seconds":
            seconds = max(0, int(float(value)))
            value = f"{seconds // 60}:{seconds % 60:02d}"
        elif key == "bitrate":
            value = f"{int(float(value) / 1000)} kbps"
        elif key == "sample_rate":
            value = f"{int(value)} Hz"
        elif key == "size_bytes":
            value = f"{float(value) / (1024 * 1024):.2f} MB"
        elif key == "modified_time":
            value = display_timestamp(float(value))
        # Every branch above produces a left-to-right numeric string, so
        # this isolates unconditionally - see ui.direction.isolate_number
        # for why that matters inside Hebrew text (issue #43).
        return isolate_number(value)

    def on_replaygain_analysis_started(self, _mode: str, total: int) -> None:
        self._replaygain_analysis_running = True
        self._insp_rg_progress.setRange(0, total)
        self._insp_rg_progress.setValue(0)
        self._insp_rg_progress.setVisible(True)
        self._insp_rg_cancel_btn.setVisible(True)
        self._insp_rg_track_btn.setEnabled(False)
        self._insp_rg_album_btn.setEnabled(False)

    def on_replaygain_analysis_progress(self, done: int, total: int) -> None:
        self._insp_rg_progress.setRange(0, max(total, 1))
        self._insp_rg_progress.setValue(done)

    def on_replaygain_analysis_complete(self, _summary: dict) -> None:
        self._replaygain_analysis_running = False
        self._insp_rg_progress.setVisible(False)
        self._insp_rg_cancel_btn.setVisible(False)
        self._refresh_selection_scope_state()
        tracks = self._get_selected_tracks()
        if tracks:
            self._populate_track_inspector(tracks)

    def _on_zoom_minus(self) -> None:
        self._change_zoom(-10)

    def _on_zoom_plus(self) -> None:
        self._change_zoom(10)

    def _on_zoom_custom(self) -> None:
        text = self._zoom_val_lbl.text().replace("%", "").strip()
        try:
            val = int(text)
            val = max(50, min(200, val))
            self._set_zoom(val)
        except ValueError:
            self._zoom_val_lbl.setText(f"{self._zoom_level}%")

    def _change_zoom(self, delta: int) -> None:
        new_val = max(50, min(200, self._zoom_level + delta))
        self._set_zoom(new_val)

    def _set_zoom(self, pct: int) -> None:
        self._zoom_level = pct
        self._zoom_val_lbl.setText(f"{pct}%")
        
        if self._cfg:
            self._cfg.tag_editor_zoom = pct
            self._cfg.save()
            
        font_size = max(6, int(10 * (pct / 100.0)))
        factor = pct / 100.0
        
        table_colors = get_colors()
        # Win11 Details View: flat header (no per-section vertical borders,
        # no bold, muted color, single underline). Capsule paint handles
        # selection — keep selection-background-color transparent so Qt
        # doesn't overdraw it with a flat rectangle.
        self._table.setStyleSheet(
            f"QTableView {{ background: {table_colors.bg}; color: {table_colors.text_primary};"
            f"  border: none; border-radius: 0;"
            f"  selection-background-color: transparent; selection-color: {table_colors.text_primary};"
            f"  font-size: {font_size}pt; }}"
            "QTableView::item { background: transparent; border: none; }"
            f"QHeaderView::section {{ background: {table_colors.bg};"
            f"  color: {table_colors.text_secondary};"
            f"  border: none;"
            f"  padding: 0 12px; height: 32px;"
            f"  font-size: {font_size}pt; font-weight: normal; }}"
            f"QHeaderView::section:hover {{ color: {table_colors.text_primary}; }}"
            f"QTableCornerButton::section {{ background: {table_colors.bg};"
            f"  border: none; }}"
        )

        font = self._table.font()
        font.setPointSize(font_size)
        self._table.setFont(font)
        
        hdr = self._table.horizontalHeader()
        hdr_font = hdr.font()
        hdr_font.setPointSize(font_size)
        hdr.setFont(hdr_font)
        
        # Load saved widths
        saved_widths = {}
        if self._cfg:
            try:
                saved_widths = {int(k): v for k, v in self._cfg.tag_editor_column_widths.items()}
            except Exception:
                pass

        self._ignore_header_resize = True
        try:
            for col in range(COLUMN_COUNT):
                base_w = saved_widths.get(col, DEFAULT_COL_WIDTHS.get(col, 100))
                if col == COL_CHECK:
                    self._table.setColumnWidth(col, ExplorerDetailsView._SIDE_EMPTY_GUTTER)
                else:
                    self._table.setColumnWidth(col, max(10, int(base_w * factor)))
        finally:
            self._ignore_header_resize = False

        # Restoring saved widths above can reopen a gap at the trailing edge
        # (or overshoot the viewport) — re-settle the filler column last.
        self._fill_leftover_space()

    def _on_tree_item_moved(self, src: Path, dest: Path) -> None:
        """Physically moves a file or folder on the disk, and updates UI.

        The controller owns the lifecycle guard, the evidence and the
        reconciliation; the panel only rebases what it displays afterwards.
        """
        result = self._run_file_operation("move_paths", [src], Path(dest).parent)
        if result is not None and result.succeeded:
            self._rebase_loaded_paths(src, result.succeeded[0].destination)

    def _on_tree_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        if not item:
            return

        path_str = item.data(0, Qt.UserRole)
        if not path_str:
            return
        path = Path(path_str)
        is_file = item.data(0, self._ROLE_IS_FILE)
        add_folder_action = None

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        open_action = menu.addAction(t("meta_open_file")) if is_file else None
        reveal_action = menu.addAction(t("meta_reveal_in_explorer"))
        copy_action = menu.addAction(t("meta_copy_path"))
        properties_action = menu.addAction(t("meta_properties"))
        menu.addSeparator()
        if not is_file:
            add_folder_action = menu.addAction(FluentIcon.FOLDER_ADD.icon(), t("meta_add_folder"))
            menu.addSeparator()

        rename_action = menu.addAction(FluentIcon.EDIT.icon(), t("meta_rename_menu"))
        move_action = menu.addAction(t("meta_move_menu"))
        delete_action = menu.addAction(FluentIcon.DELETE.icon(), t("meta_delete_menu"))

        action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if open_action is not None and action == open_action:
            self._perform_path_operation([path], self._file_operations.open_file)
        elif action == reveal_action:
            self._perform_path_operation([path], self._file_operations.reveal_in_explorer)
        elif action == copy_action:
            self._copy_tree_path(path)
        elif action == properties_action:
            self._show_path_properties(path)
        elif add_folder_action is not None and action == add_folder_action:
            self._on_tree_add_folder(path)
        elif action == rename_action:
            self._on_tree_rename(path, is_file)
        elif action == move_action:
            self._move_tree_path(path)
        elif action == delete_action:
            self._on_tree_delete(path, is_file)

    def _on_tree_add_folder(self, parent_path: Path) -> None:
        new_name, ok = get_text(
            self,
            t("meta_new_folder_dialog_title"),
            t("meta_new_folder_prompt"),
            text=t("meta_new_folder_default"),
        )
        if not ok or not new_name.strip():
            return

        result = self._run_file_operation("create_folder", parent_path, new_name)
        if result is not None and result.succeeded:
            self._rebuild_tree_from_loaded_tracks()
            self._get_or_create_folder_item(result.succeeded[0].destination)

    def _on_tree_rename(self, path: Path, is_file: bool) -> None:
        new_name, ok = get_text(
            self,
            t("meta_rename_dialog_title"),
            t("meta_rename_prompt"),
            text=path.name,
        )
        if not ok or not new_name.strip():
            return

        result = self._run_file_operation("rename_path", path, new_name)
        if result is not None and result.succeeded:
            self._rebase_loaded_paths(path, result.succeeded[0].destination)

    def _on_tree_delete(self, path: Path, is_file: bool) -> None:
        title = t("meta_delete_file_title") if is_file else t("meta_delete_folder_title")
        text = t("meta_delete_confirm", name=path.name)
        if not is_file:
            text += t("meta_delete_recursive_note")

        if not confirm(self, title, text, accept_text=t("meta_delete_menu"),
                       cancel_text=t("cancel_btn"), danger=True):
            return
        result = self._run_file_operation("recycle_paths", [path])
        if result is None or not result.succeeded:
            return
        removed = [track.path for track in self._model.get_all_tracks()
                   if track.path == path or self._is_path_within(track.path, path)]
        self._model.remove_paths(removed)
        self._navigation.reconcile_after_delete(path)
        self._rebuild_tree_from_loaded_tracks()
        self._apply_navigation_filter()
        self._update_summary()
        self._refresh_checked_scope_state()

    def _move_tree_path(self, path: Path) -> None:
        if not self._root_folder:
            return
        folder = QFileDialog.getExistingDirectory(self, t("meta_move_choose_folder"), str(self._root_folder))
        if not folder:
            return
        destination = Path(folder) / path.name
        self._on_tree_item_moved(path, destination)

    def _copy_tree_path(self, path: Path) -> None:
        try:
            QApplication.clipboard().setText(self._file_operations.copy_path(path))
        except FileOperationError as exc:
            show_warning(self, t("meta_error_title"), str(exc))

    def _show_path_properties(self, path: Path) -> None:
        try:
            props = self._file_operations.properties(path)
        except FileOperationError as exc:
            show_warning(self, t("meta_error_title"), str(exc))
            return
        show_info(self, t("meta_properties"), t("meta_properties_item", name=props.path.name, path=str(props.path), size=isolate_number(f"{props.size_bytes:,}"), modified=isolate_number(display_timestamp(props.modified_at))))

    def _perform_path_operation(self, paths: list[Path], operation) -> None:
        errors: list[str] = []
        for path in paths:
            try:
                operation(path)
            except FileOperationError as exc:
                errors.append(str(exc))
        if errors:
            show_warning(self, t("meta_error_title"), "\n".join(errors))

    @staticmethod
    def _is_path_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _rebase_loaded_paths(self, source: Path, destination: Path) -> None:
        self._navigation.remap_folder(source, destination)
        for track in self._model.get_all_tracks():
            try:
                relative = track.path.relative_to(source)
            except ValueError:
                continue
            new_path = destination / relative
            if track.proposed_filename == new_path.name:
                track.proposed_filename = None
            self._model.update_file_path(track, new_path)
        self._rebuild_tree_from_loaded_tracks()
        self._apply_navigation_filter()

    def _rebuild_tree_from_loaded_tracks(self) -> None:
        if not self._root_folder:
            return
        was_blocked = self._tree.blockSignals(True)
        self._tree.setUpdatesEnabled(False)
        self._ignore_tree_changes = True
        try:
            self._tree.clear()
            self._folder_items.clear()
            self._file_items.clear()
            self._ensure_root_item()
            self._add_many_to_tree(self._model.get_all_tracks())
            current = self._navigation.current
            if current is not None and current.is_dir():
                self._get_or_create_folder_item(current)
        finally:
            self._ignore_tree_changes = False
            self._tree.blockSignals(was_blocked)
            self._tree.setUpdatesEnabled(True)

    def closeEvent(self, event) -> None:
        if not self.shutdown_artwork_workers():
            event.ignore()
            return
        super().closeEvent(event)
