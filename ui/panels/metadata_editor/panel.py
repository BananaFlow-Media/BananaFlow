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

from . import prompts

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
    QLayout,
    QLineEdit,
    QMenu,
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
    QWidgetAction,
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
from ui.dialogs.styled_dialog import add_header
from ui.workers.artwork_thumbnail_worker import ArtworkThumbnailCache, ArtworkThumbnailWorker

from .dialogs import (
    ApplyConfirmationDialog,
    ApplyResultDialog,
    AutoArrangeSettingsDialog,
    CleanSettingsDialog,
    MoreColumnsDialog,
)
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
    mark_tag_editor_dialog,
    op_row_qss,
    primary_btn_style,
    tag_editor_colors,
)
from .tree import ExplorerTreeWidget
# ArtworkDropPreview is re-exported: it used to be defined here, and external
# importers (including tests) still reach for it on this module.
from .widgets import ArtworkDropPreview, OpRow, VerticalLabel

# Panel behaviour split across mixins purely to keep each file readable. They
# are not independent components: every method still runs on the panel and
# reaches the panel's attributes directly, exactly as before the split.
from .file_actions import FileActionsMixin
from .inspector_build import InspectorBuildMixin
from .pane_layout import PaneLayoutMixin
from .table_layout import TableLayoutMixin
from .tree_ops import TreeOpsMixin

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


class MetadataEditorPanel(
    PaneLayoutMixin,
    InspectorBuildMixin,
    FileActionsMixin,
    TableLayoutMixin,
    TreeOpsMixin,
    QWidget,
):
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
    scan_cancel_requested       = Signal()
    conflict_resolution_requested = Signal(object, str, object)

    _TREE_RAIL_WIDTH = 42
    _INSPECTOR_RAIL_WIDTH = 58
    _TREE_OPEN_MIN = 160
    _TABLE_OPEN_MIN = 340
    _INSPECTOR_OPEN_MIN = 270
    _COLLAPSE_DRAG_MARGIN = 46

    _DEFAULT_SPLITTER_SIZES = [220, 688, 370]

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
        self._responsive_mode = "wide"
        self._responsive_forced_tree_collapse = False
        self._active_inspector_tool = 0
        self._inspector_rail_buttons: dict[str, QPushButton] = {}
        self._inspector_mode_buttons: dict[str, QPushButton] = {}
        self._inspector_pane_modes: list[str] = []
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
            prompts.show_warning(self, t("meta_error_title"),
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
        root_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_toolbar())

        self._toolbar_sep = QFrame()
        self._toolbar_sep.setFrameShape(QFrame.Shape.HLine)
        self._toolbar_sep.setFixedHeight(1)
        self._toolbar_sep.setStyleSheet(f"background: {get_colors().border}; border: none;")
        root_layout.addWidget(self._toolbar_sep)

        root_layout.addWidget(self._build_body(), stretch=1)

        self._footer_sep = QFrame()
        self._footer_sep.setFrameShape(QFrame.Shape.HLine)
        self._footer_sep.setFixedHeight(1)
        self._footer_sep.setStyleSheet(f"background: {get_colors().border}; border: none;")
        root_layout.addWidget(self._footer_sep)
        root_layout.addWidget(self._build_footer())
        self._refresh_footer()

        from ui.theme_manager import ThemeManager as _TM
        _tm = _TM.instance()
        if _tm is not None:
            _tm.theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Re-apply theme-dependent styles for the toolbar, tree, table, buttons."""
        c = tag_editor_colors()
        accent = c.accent
        accent_dim = dim_hex(accent)
        self.setStyleSheet(
            f"QWidget#metadataEditorPage {{ background: {c.bg}; color: {c.text_primary};"
            " border: none; border-radius: 0px; font-family: 'Segoe UI'; font-size: 13px; }}"
            f"QWidget#metadataEditorPage QToolTip {{ background: {c.surface}; color: {c.text_primary};"
            f" border: 1px solid {c.border}; border-radius: 8px; padding: 5px 8px; }}"
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
                "QFrame#autoOrderSplitButton { background: transparent; border: none; }"
            )
        if hasattr(self, "_auto_separator"):
            self._auto_separator.setStyleSheet(f"background: {accent_border}; border: none;")
        if hasattr(self, "_auto_btn") and hasattr(self, "_auto_cfg_btn"):
            self._auto_btn.setIcon(self._make_magic_wand_icon(color=auto_text))
            self._auto_cfg_btn.setIcon(FluentIcon.SETTING.icon(color=auto_text))
            self._auto_btn.setStyleSheet(
                f"QToolButton {{ background: {c.accent}; color: #ffffff; border: 1px solid {c.accent};"
                " border-radius: 9px; padding: 0 10px; font-size: 11.5px; font-weight: 800; }"
                f"QToolButton:hover {{ background: {c.accent_dark}; border-color: {c.accent_dark}; }}"
                f"QToolButton:disabled {{ background: {c.surface3}; color: {c.text_tertiary}; border-color: {c.border}; }}"
            )
            self._auto_cfg_btn.setStyleSheet(
                f"QToolButton {{ background: {c.surface}; color: {c.text_primary}; border: 1px solid {c.border};"
                " border-radius: 9px; padding: 0 10px; font-size: 11px; font-weight: 700; }"
                f"QToolButton:hover {{ background: {c.surface3}; }}"
                f"QToolButton:disabled {{ color: {c.text_tertiary}; }}"
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
            rail_qss = f"QFrame {{ background: {c.surface2}; border-right: 1px solid {c.border}; border-radius: 0px; }}"
            self._tree_rail.setStyleSheet(rail_qss)
        if hasattr(self, "_tree_frame"):
            self._tree_frame.setStyleSheet(
                f"QFrame {{ background: {c.surface2}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_tree_body"):
            self._tree_body.setStyleSheet(
                f"QFrame {{ background: {c.surface2}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_table_frame"):
            self._table_frame.setStyleSheet(
                f"QFrame {{ background: {c.surface2}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_table_empty_page"):
            empty_qss = f"QWidget {{ background: {c.bg}; color: {c.text_primary}; }}"
            self._table_empty_page.setStyleSheet(empty_qss)
            self._table_loading_page.setStyleSheet(empty_qss)
            if hasattr(self, "_table_error_page"):
                self._table_error_page.setStyleSheet(empty_qss)
        if hasattr(self, "_empty_state_card"):
            card_qss = (
                f"QFrame#metadataStateCard {{ background: {c.surface}; border: 1px solid {c.border};"
                " border-radius: 13px; }}"
                "QLabel { background: transparent; border: none; }"
            )
            title_qss = f"color: {c.text_primary}; font-size: 20px; font-weight: 800;"
            body_qss = f"color: {c.text_secondary}; font-size: 13px;"
            self._empty_state_card.setStyleSheet(card_qss)
            self._loading_state_card.setStyleSheet(card_qss)
            self._empty_title_lbl.setStyleSheet(title_qss)
            self._loading_title_lbl.setStyleSheet(title_qss)
            self._empty_body_lbl.setStyleSheet(body_qss)
            self._loading_detail_lbl.setStyleSheet(body_qss)
            if hasattr(self, "_error_state_card"):
                self._error_state_card.setStyleSheet(card_qss)
                self._error_title_lbl.setStyleSheet(title_qss)
                self._error_body_lbl.setStyleSheet(body_qss)
        self._apply_shell_theme(c)
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
                f"QFrame {{ background: {c.surface}; border-left: 1px solid {c.border}; border-radius: 0px; }}"
            )
        if hasattr(self, "_inspector_content"):
            self._inspector_content.setStyleSheet(
                f"QFrame {{ background: {c.surface}; border: none; border-radius: 0px; }}"
            )
        if hasattr(self, "_inspector_header"):
            self._inspector_header.setStyleSheet(
                f"QFrame {{ background: {c.surface3}; border-bottom: 1px solid {c.border}; border-radius: 0px; }}"
            )
        self._refresh_tool_button_states()

        # Tree widget
        if hasattr(self, "_tree"):
            self._tree.setStyleSheet(
                f"QTreeWidget {{ border: none; border-radius: 0; background: {c.surface2}; color: {c.text_primary}; }}"
                f"QTreeWidget::viewport {{ background: {c.surface2}; }}"
                f"QTreeWidget::item {{ min-height: 28px; padding: 0 7px; border-radius: 7px; }}"
                f"QTreeWidget::item:selected {{ background: {accent}33; color: {c.text_primary}; }}"
                f"QTreeWidget::item:hover {{ background: {c.surface3}; }}"
            )
            tree_pal = self._tree.viewport().palette()
            tree_pal.setColor(QPalette.Base, QColor(c.surface2))
            tree_pal.setColor(QPalette.Window, QColor(c.surface2))
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
            tc = tag_editor_colors()
            self._table.viewport().setStyleSheet(f"background: {tc.surface};")
            pal = self._table.viewport().palette()
            pal.setColor(QPalette.Base, QColor(tc.surface))
            pal.setColor(QPalette.Window, QColor(tc.surface))
            self._table.viewport().setPalette(pal)
            self._table.viewport().setAutoFillBackground(True)
            self._table.viewport().update()

        # Inspector action rows
        self._refresh_op_rows_style()
        primary_qss = primary_btn_style()
        for btn in getattr(self, "_checked_scope_buttons", []):
            btn.setStyleSheet(primary_qss)
        for btn in getattr(self, "_selection_scope_buttons", []):
            if btn.property("accentRole") == "primary":
                style = primary_qss
            elif btn.property("dangerRole"):
                c = tag_editor_colors()
                style = (
                    btn_style()
                    + f"QPushButton {{ color: {c.danger}; border-color: {c.danger}; }}"
                    + f"QPushButton:hover {{ background: {c.danger_soft}; border-color: {c.danger}; }}"
                )
            else:
                style = btn_style()
            btn.setStyleSheet(style)
        if hasattr(self, "_table_info_lbl"):
            self._refresh_checked_scope_state()
            self._refresh_selection_scope_state()

    def _apply_shell_theme(self, c) -> None:
        """Apply the HTML prototype's component-level tokens."""
        if hasattr(self, "_path_chip"):
            self._path_chip.setStyleSheet(
                f"QLabel {{ background: {c.surface3}; color: {c.text_secondary};"
                " border: none; border-radius: 9px; padding: 0 10px; font-size: 12px; }}")
        if hasattr(self, "_search_edit"):
            self._search_edit.setStyleSheet(
                f"QLineEdit {{ background: {c.surface}; color: {c.text_primary};"
                f" border: 1px solid {c.border}; border-radius: 10px; padding: 0 9px; }}"
                f"QLineEdit:focus {{ border: 1px solid {c.accent}; }}")
        if hasattr(self, "_more_btn"):
            self._more_btn.setStyleSheet(self._toolbar_button_style("neutral"))
        if hasattr(self, "_tree_header"):
            self._tree_header.setStyleSheet(
                f"QFrame#tagEditorTreeHeader {{ background: {c.surface2};"
                f" border-bottom: 1px solid {c.border}; border-radius: 0; }}"
                f"QToolButton {{ background: transparent; color: {c.text_secondary}; border: none;"
                " border-radius: 8px; padding: 0; }}"
                f"QToolButton:hover {{ background: {c.surface3}; }}")
        if hasattr(self, "_tree_hint"):
            self._tree_hint.setStyleSheet(
                f"QLabel#tagEditorTreeHint {{ background: {c.surface3}; color: {c.text_secondary};"
                " border: none; border-radius: 9px; margin: 8px 5px; font-size: 10.5px; }}")

        if hasattr(self, "_footer_bar"):
            self._footer_bar.setStyleSheet(
                f"QFrame#tagEditorFooter {{ background: {c.surface}; border: none;"
                " border-radius: 0px; }}")
            self._footer_sep.setStyleSheet(f"background: {c.border}; border: none;")
            self._footer_count.setStyleSheet(
                f"QLabel {{ background: {c.accent_soft}; color: {c.accent}; border-radius: 9px;"
                " font-weight: 900; font-size: 13px; }}")
            self._footer_title.setStyleSheet(
                f"color: {c.text_primary}; font-size: 12px; font-weight: 800;")
            self._footer_desc.setStyleSheet(f"color: {c.text_secondary}; font-size: 10.5px;")

        if hasattr(self, "_table_card"):
            self._table_card.setStyleSheet(
                f"QFrame#tagEditorTableCard {{ background: {c.surface}; border: 1px solid {c.border};"
                " border-radius: 13px; }}")
        if hasattr(self, "_table_status_bar"):
            self._table_status_bar.setStyleSheet(
                f"QFrame#tagEditorTableStatus {{ background: {c.surface3};"
                f" border-top: 1px solid {c.border}; border-radius: 0px; }}"
                f"QLabel {{ color: {c.text_secondary}; font-size: 10.5px; font-weight: 600; }}")
        if hasattr(self, "_nav_host"):
            nav_qss = (
                f"QToolButton, QPushButton {{ background: {c.surface}; color: {c.text_secondary};"
                f" border: 1px solid {c.border}; border-radius: 8px; padding: 0; }}"
                f"QToolButton:hover, QPushButton:hover {{ background: {c.surface3}; }}"
                f"QToolButton#tagEditorExcludedChip {{ padding: 0 8px; border-radius: 7px;"
                " font-size: 10.5px; font-weight: 800; }}"
                f"QToolButton#tagEditorStaleChip {{ padding: 0 8px; border-radius: 7px;"
                f" background: {c.warn_soft}; color: {c.warn}; border-color: {c.warn};"
                " font-size: 10.5px; font-weight: 800; }}")
            self._nav_host.setStyleSheet(nav_qss)
        if hasattr(self, "_zoom_frame"):
            self._zoom_frame.setStyleSheet(
                f"QFrame#tagEditorZoom {{ background: {c.surface}; border: 1px solid {c.border};"
                " border-radius: 8px; }}"
                f"QPushButton {{ background: transparent; color: {c.text_secondary}; border: none;"
                " border-radius: 6px; padding: 0; }}"
                f"QPushButton:hover {{ background: {c.surface3}; }}"
                f"QLineEdit {{ background: transparent; color: {c.text_secondary}; border: none;"
                " padding: 0; font-size: 10.5px; font-weight: 800; }}")

        if hasattr(self, "_inspector_header"):
            self._inspector_header.setStyleSheet(
                f"QFrame#tagEditorInspectorHeader {{ background: {c.surface3};"
                f" border-bottom: 1px solid {c.border}; border-radius: 0; }}"
                f"QToolButton {{ background: transparent; color: {c.text_secondary}; border: none;"
                " border-radius: 8px; padding: 0; }}"
                f"QToolButton:hover {{ background: {c.surface}; }}")
        if getattr(self, "_inspector_mode_buttons", None):
            mode_qss = (
                f"QPushButton {{ background: transparent; color: {c.text_secondary}; border: none;"
                " border-radius: 7px; padding: 0 8px; font-weight: 800; font-size: 11.5px; }}"
                f"QPushButton:hover {{ background: {c.surface3}; }}"
                f"QPushButton:checked {{ background: {c.accent}; color: #ffffff; }}")
            for btn in self._inspector_mode_buttons.values():
                btn.setStyleSheet(mode_qss)
        if hasattr(self, "_inspector_pages"):
            self._inspector_pages.setStyleSheet(
                f"QStackedWidget, QScrollArea {{ background: {c.surface}; border: none; }}"
                f"QScrollArea > QWidget > QWidget {{ background: {c.surface}; }}"
                f"QGroupBox {{ background: transparent; color: {c.text_primary}; border: none;"
                " margin: 0; padding: 0; }}"
                f"QGroupBox::title {{ color: transparent; height: 0; }}"
                f"QLabel {{ background: transparent; color: {c.text_primary}; }}"
                f"QLabel#tagEditorInspectorNote {{ background: {c.surface3}; color: {c.text_secondary};"
                " border-radius: 9px; padding: 8px 10px; font-size: 10.5px; }}"
                f"QLabel#tagEditorDialogNote {{ background: {c.surface3}; color: {c.text_secondary};"
                f" border: 1px solid {c.border}; border-radius: 9px; padding: 7px 9px; font-size: 10.5px; }}"
                f"QLabel#tagEditorSectionTitle {{ color: {c.text_primary}; font-size: 11.5px; font-weight: 800; }}"
                f"QLabel#tagEditorCoverCurrent {{ background: {c.surface}; color: {c.text_secondary};"
                f" border: 1px solid {c.border}; border-radius: 10px; padding: 8px; font-size: 10.5px; }}"
                f"QLabel#tagEditorCoverProposed {{ background: {c.accent_soft}; color: {c.accent_dark};"
                f" border: 1px dashed {c.accent}; border-radius: 10px; padding: 8px; font-size: 10.5px; }}"
                f"QTextEdit#tagEditorLyrics {{ background: {c.surface}; color: {c.text_primary};"
                f" border: 1px solid {c.border}; border-radius: 10px; padding: 9px; font-size: 11px; }}"
                f"QWidget#tagEditorFieldRow QLineEdit {{ background: {c.surface}; color: {c.text_primary};"
                f" border: 1px solid {c.border}; border-radius: 8px; padding: 0 9px; }}"
                f"QWidget#tagEditorFieldRow QLabel#tagEditorReadOnlyValue {{ background: {c.surface};"
                f" color: {c.text_primary}; border: 1px solid {c.border}; border-radius: 8px; padding: 5px 9px; }}"
                f"QWidget#tagEditorFieldRow QLineEdit:focus {{ border-color: {c.accent}; }}"
                f"QWidget#tagEditorFieldRow QToolButton {{ background: {c.surface}; color: {c.text_tertiary};"
                f" border: 1px solid {c.border}; border-radius: 8px; padding: 0; font-size: 9.5px; }}"
                f"QToolButton#tagEditorAdvancedFields {{ background: {c.surface}; color: {c.text_secondary};"
                f" border: 1px dashed {c.border}; border-radius: 9px; padding: 0 9px;"
                " font-size: 10.5px; font-weight: 800; text-align: left; }}"
                f"QFrame#tagEditorPropertyTable {{ background: {c.surface}; border: 1px solid {c.border};"
                " border-radius: 10px; }}"
                f"QFrame#tagEditorPropertyRow {{ background: transparent; border-bottom: 1px solid {c.surface3}; }}"
                f"QLabel#tagEditorPropertyName {{ color: {c.text_secondary}; font-size: 10.5px; font-weight: 800; }}"
                f"QLabel#tagEditorPropertyValue {{ color: {c.text_primary}; font-size: 10.5px; }}"
                f"QFrame#tagEditorPendingItem, QFrame#tagEditorProblemItem {{ background: {c.surface};"
                f" border: 1px solid {c.border}; border-radius: 9px; }}"
                f"QLabel#tagEditorPendingFile {{ color: {c.text_tertiary}; font-size: 9.5px; }}"
                f"QLabel#tagEditorPendingChange, QLabel#tagEditorProblemCopy {{ color: {c.text_primary}; font-size: 10.5px; }}"
                f"QLabel#tagEditorSeverityError {{ background: {c.danger_soft}; color: {c.danger};"
                " border-radius: 6px; padding: 4px 6px; font-size: 9px; font-weight: 900; }}"
                f"QPushButton {{ background: {c.surface}; color: {c.text_primary}; border: 1px solid {c.border};"
                " border-radius: 9px; min-height: 30px; padding: 0 9px; font-size: 10.5px; font-weight: 700; }}"
                f"QPushButton:hover {{ background: {c.surface3}; }}"
                f"QPushButton[accentRole=\"primary\"] {{ background: {c.accent}; color: #ffffff; border-color: {c.accent}; }}"
                f"QPushButton[dangerRole=\"true\"] {{ color: {c.danger}; border-color: {c.danger}; }}"
                f"QToolButton {{ background: {c.surface}; color: {c.text_secondary}; border: 1px solid {c.border};"
                " border-radius: 8px; }}")
        if getattr(self, "_inspector_tool_buttons", None):
            chip_qss = (
                f"QPushButton {{ background: {c.surface}; color: {c.text_secondary};"
                f" border: 1px solid {c.border}; border-radius: 7px; padding: 0 9px;"
                " font-size: 10.5px; font-weight: 800; }}"
                f"QPushButton:hover {{ border-color: {c.accent}; }}"
                f"QPushButton:checked {{ background: {c.accent_soft}; border-color: {c.accent};"
                f" color: {c.accent_dark}; }}")
            for btn in self._inspector_tool_buttons:
                btn.setStyleSheet(chip_qss)

    def _toolbar_button_style(self, role: str) -> str:
        c = tag_editor_colors()
        accent_color = QColor(c.accent)
        primary_text = "#ffffff" if not accent_color.isValid() or accent_color.lightness() < 170 else "#111827"

        if role == "primary":
            primary_bg = (
                f"rgba({accent_color.red()}, {accent_color.green()}, {accent_color.blue()}, 1.0)"
                if accent_color.isValid() else c.accent
            )
            return (
                f"QToolButton {{ background: {primary_bg}; color: {primary_text};"
                f"  border: 1px solid {c.accent}; border-radius: 9px;"
                f"  padding: 0 12px; font-weight: 800; }}"
                f"QToolButton:hover {{ background: {c.accent_dark}; border-color: {c.accent_dark}; }}"
                f"QToolButton:pressed {{ background: {c.accent_dark}; border-color: {c.accent_dark}; }}"
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
                f"  border: 1px solid transparent; border-radius: 9px;"
                f"  padding: 0 12px; font-weight: 700; }}"
                f"QToolButton:hover {{ background: {hover_bg}; border-color: {c.accent}; }}"
                f"QToolButton:pressed {{ background: {hover_bg}; }}"
                f"QToolButton:disabled {{ background: {c.bg}; color: {c.text_tertiary};"
                f"  border: 1px solid {c.border}; }}"
            )

        return (
            f"QToolButton {{ background: {c.surface}; color: {c.text_primary};"
            f"  border: 1px solid {c.border}; border-radius: 9px;"
            f"  padding: 0 12px; font-weight: 700; }}"
            f"QToolButton:hover {{ background: {c.surface3}; border-color: {c.border}; }}"
            f"QToolButton:disabled {{ background: {c.surface}; color: {c.text_tertiary};"
            f"  border-color: {c.border}; }}"
        )

    def _toolbar_icon_color(self, role: str, *, enabled: bool = True) -> str:
        c = tag_editor_colors()
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
            browse_role = "primary"
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
        btn.setIconSize(QSize(18, 18))
        btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        btn.setFixedHeight(32)
        btn.setMinimumWidth(32)
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
        container.setFixedHeight(74)
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._auto_container = container

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._auto_cfg_btn = QToolButton()
        self._auto_cfg_btn.setText(t("meta_auto_configure_actions"))
        self._auto_cfg_btn.setIcon(FluentIcon.SETTING.icon(color=get_colors().text_primary))
        self._auto_cfg_btn.setIconSize(QSize(16, 16))
        self._auto_cfg_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        a11y.describe(self._auto_cfg_btn, t("meta_auto_cfg_tooltip"),
                      tooltip=t("meta_auto_cfg_tooltip"))
        self._auto_cfg_btn.setFixedHeight(34)
        self._auto_cfg_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._auto_cfg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_cfg_btn.setEnabled(False)
        self._auto_cfg_btn.clicked.connect(self._on_auto_arrange_settings)

        self._auto_separator = QFrame()
        self._auto_separator.setVisible(False)

        self._auto_btn = QToolButton()
        self._auto_btn.setText(self._toolbar_text("meta_auto_btn"))
        self._auto_btn.setIcon(self._make_magic_wand_icon(color=get_colors().text_primary))
        self._auto_btn.setIconSize(QSize(17, 17))
        self._auto_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_btn.setEnabled(False)
        self._auto_btn.clicked.connect(self._on_auto_arrange)
        self._auto_btn.setFixedHeight(34)
        self._auto_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout.addWidget(self._auto_btn)
        layout.addWidget(self._auto_cfg_btn)

        return container

    def _build_toolbar(self) -> QFrame:
        """Slim toolbar: what you act on, not what you do to it.

        Everything that operates on *pending changes* (undo/redo, review,
        revert, apply) moved to the footer, next to the count it acts on.
        Everything that operates on *stored data* (import/export, backups,
        restore) moved behind "More". What is left is the folder itself.
        """
        bar = QFrame()
        bar.setObjectName("tagEditorToolbar")
        bar.setFixedHeight(54)
        self._toolbar_bar = bar

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(8)

        self._browse_btn = self._make_toolbar_action(
            "meta_browse_folder", FluentIcon.FOLDER, self._on_browse
        )
        self._browse_btn.setFixedHeight(36)
        layout.addWidget(self._browse_btn)

        # A path is not prose: it stays LTR and elides from the left so the
        # folder name — the part that identifies it — survives truncation.
        self._path_chip = QLabel(t("meta_shell_no_folder"))
        self._path_chip.setObjectName("tagEditorPathChip")
        self._path_chip.setLayoutDirection(Qt.LeftToRight)
        self._path_chip.setTextFormat(Qt.PlainText)
        self._path_chip.setFixedHeight(32)
        self._path_chip.setMaximumWidth(240)
        self._path_chip.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        a11y.describe(self._path_chip, t("meta_shell_active_folder"))
        layout.addWidget(self._path_chip)

        # Two different refreshes behind one control: the click is the cheap
        # incremental reconcile people press often; the full rescan re-reads
        # the folder and is deliberately one step further away.
        self._manual_refresh_btn = self._make_toolbar_action(
            "meta_manual_refresh", FluentIcon.SYNC,
            self.manual_refresh_requested.emit, enabled=False)
        self._manual_refresh_btn.setText("")
        self._manual_refresh_btn.setFixedSize(36, 32)
        self._manual_refresh_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._manual_refresh_btn.setAccessibleName(t("meta_manual_refresh"))
        self._manual_refresh_btn.setPopupMode(QToolButton.MenuButtonPopup)
        refresh_menu = QMenu(self._manual_refresh_btn)
        refresh_menu.setAccessibleName(t("meta_shell_refresh_menu"))
        self._rescan_action = refresh_menu.addAction(t("meta_shell_rescan"))
        self._rescan_action.setToolTip(t("meta_shell_rescan_tooltip"))
        self._rescan_action.triggered.connect(self._on_scan)
        self._refresh_menu = refresh_menu
        self._manual_refresh_btn.setMenu(refresh_menu)
        layout.addWidget(self._manual_refresh_btn)

        self._search_edit = QLineEdit()
        self._search_edit.addAction(FluentIcon.SEARCH.icon(), QLineEdit.LeadingPosition)
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setPlaceholderText(t("meta_search_tracks"))
        # Filtering changes what is listed, never what Apply writes, so the
        # description says so rather than leaving it to be inferred.
        a11y.describe(self._search_edit, t("meta_search_tracks"),
                      description=t("meta_a11y_search_scope_note"))
        self._search_edit.setFixedSize(230, 34)
        # Qt builds the inline clear button itself and leaves it unnamed, so a
        # screen reader reaches an anonymous button at the end of the field.
        for clear_button in self._search_edit.findChildren(QToolButton):
            a11y.describe(clear_button, t("meta_a11y_clear_search"),
                          tooltip=t("meta_a11y_clear_search"))
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self._search_edit)

        layout.addStretch()

        layout.addWidget(self._build_more_button())

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

    def _build_footer(self) -> QFrame:
        """Pending work and the actions that resolve it, in one place.

        Apply's scope is every included, unblocked pending change — never the
        selection and never what the filter happens to show. Putting the count
        and the button side by side is the point: the number next to Apply is
        the number Apply writes.
        """
        footer = QFrame()
        footer.setObjectName("tagEditorFooter")
        footer.setFixedHeight(56)
        self._footer_bar = footer
        a11y.describe(footer, t("meta_footer_a11y"))

        layout = QHBoxLayout(footer)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(9)

        self._footer_count = QLabel("0")
        self._footer_count.setObjectName("tagEditorFooterCount")
        self._footer_count.setAlignment(Qt.AlignCenter)
        self._footer_count.setFixedSize(30, 30)
        layout.addWidget(self._footer_count)

        self._footer_title = QLabel(t("meta_footer_ready"))
        self._footer_title.setObjectName("tagEditorFooterTitle")
        layout.addWidget(self._footer_title)

        self._footer_desc = QLabel("")
        self._footer_desc.setObjectName("tagEditorFooterDesc")
        layout.addWidget(self._footer_desc)

        layout.addStretch()

        self._undo_btn = self._make_toolbar_action(
            "meta_undo_changes", FluentIcon.RETURN, self.undo_requested.emit, enabled=False
        )
        self._undo_btn.setText("")
        self._undo_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._undo_btn.setFixedSize(32, 32)
        self._undo_btn.setShortcut("Ctrl+Z")
        layout.addWidget(self._undo_btn)

        self._redo_btn = self._make_toolbar_action(
            "meta_redo_changes", FluentIcon.SYNC, self.redo_requested.emit, enabled=False
        )
        self._redo_btn.setText("")
        self._redo_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._redo_btn.setFixedSize(32, 32)
        self._redo_btn.setShortcut("Ctrl+Y")
        layout.addWidget(self._redo_btn)

        self._review_btn = self._make_toolbar_action(
            "meta_review_changes", FluentIcon.VIEW, self._on_review_changes, enabled=False
        )
        self._review_btn.setShortcut("Ctrl+Shift+R")
        # The shortcut is not discoverable from an icon-and-label button.
        self._review_btn.setToolTip(
            f'{t("meta_review_changes").strip()} (Ctrl+Shift+R)')
        layout.addWidget(self._review_btn)

        self._revert_btn = self._make_toolbar_action(
            "meta_revert_changes", FluentIcon.LEFT_ARROW, self._on_revert, enabled=False
        )
        layout.addWidget(self._revert_btn)

        self._apply_btn = self._make_toolbar_action(
            "meta_apply_changes", FluentIcon.SAVE, self._on_apply, enabled=False
        )
        self._apply_btn.setMinimumWidth(92)
        self._apply_btn.setFixedHeight(36)
        layout.addWidget(self._apply_btn)

        return footer

    def _refresh_footer(self) -> None:
        """Mirror the apply scope into the footer's own summary."""
        if not hasattr(self, "_footer_title"):
            return
        candidates = self._workspace.apply_candidates()
        changed = self._workspace.changed_tracks()
        blocked = self._workspace.apply_blockers()
        excluded = self._workspace.excluded_tracks()
        change_count = sum(
            len(item.proposed.changed_fields(item.original)) + bool(item.proposed_filename)
            for item in changed
        )
        file_count = len(changed)

        has_work = bool(changed)
        for widget in (self._footer_count, self._undo_btn, self._redo_btn,
                       self._review_btn, self._revert_btn, self._apply_btn):
            widget.setVisible(has_work)

        if not has_work:
            self._footer_bar.setFixedHeight(30)
            total = len(self._model.get_all_tracks())
            self._footer_title.setText(t("meta_footer_ready"))
            self._footer_desc.setText(
                t("meta_footer_loaded", total=total) if total else "")
            return

        self._footer_bar.setFixedHeight(56)
        self._footer_count.setText(isolate_number(str(len(candidates))))
        self._footer_count.setAccessibleName(
            t("meta_footer_count_a11y", n=len(candidates)))
        self._footer_title.setText(
            t("meta_footer_pending_one_file", changes=change_count)
            if file_count == 1
            else t("meta_footer_pending", changes=change_count, files=file_count)
        )
        notes = [t("meta_footer_backup_note")]
        if blocked:
            notes.append(t("meta_footer_blocked_note", n=len(blocked)))
        if excluded:
            notes.append(t("meta_footer_excluded_note", n=len(excluded)))
        self._footer_desc.setText(" · ".join(notes))
        self._apply_btn.setText(t("meta_apply_count", n=change_count))

    def _columns_button(self) -> QToolButton:
        """Direct access to the column picker.

        It was previously only reachable by right-clicking the header, which
        is not a discoverable place to look for "show me a different field".
        The header menu stays exactly as it was — this is an additional door.
        """
        self._columns_btn = QToolButton()
        self._columns_btn.setIcon(FluentIcon.LAYOUT.icon())
        self._columns_btn.setIconSize(QSize(14, 14))
        self._columns_btn.setFixedSize(26, 26)
        a11y.describe(self._columns_btn, t("mt_more_columns"),
                      description=t("mt_search_columns"),
                      tooltip=t("mt_more_columns"))
        self._columns_btn.clicked.connect(self._on_more_columns)
        return self._columns_btn

    def _build_table_status_bar(self) -> QFrame:
        """Counts that describe the listing, pinned under the table."""
        bar = QFrame()
        bar.setObjectName("tagEditorTableStatus")
        bar.setFixedHeight(26)
        self._table_status_bar = bar
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 3, 12, 3)
        layout.setSpacing(10)

        self._table_info_lbl = QLabel("")
        layout.addWidget(self._table_info_lbl)

        self._apply_scope_lbl = QLabel("")
        self._apply_scope_lbl.setAccessibleName(t("meta_apply_scope_label"))
        layout.addWidget(self._apply_scope_lbl)

        layout.addStretch()

        self._exclude_apply_btn = QToolButton()
        self._exclude_apply_btn.clicked.connect(self._toggle_selected_apply_exclusion)
        self._exclude_apply_btn.setVisible(False)
        layout.addWidget(self._exclude_apply_btn)

        self._scope_hint_lbl = QLabel(t("meta_scope_hint"))
        self._scope_hint_lbl.setObjectName("tagEditorScopeHint")
        layout.addWidget(self._scope_hint_lbl)
        return bar

    @staticmethod
    def _migrate_saved_column_order(saved_order) -> list[int] | None:
        """Widen a column order saved before the status column existed.

        The restore path only accepts an order of exactly COLUMN_COUNT entries.
        Adding a column would therefore make every previously saved order fail
        that check and silently reset the user's arrangement on first launch of
        the new version. Appending the columns they have never seen keeps the
        order they chose and leaves the new one at the end.
        """
        if not saved_order:
            return None
        order = list(saved_order)
        if len(order) == COLUMN_COUNT:
            return order
        if len(order) > COLUMN_COUNT:
            return None
        missing = [col for col in range(COLUMN_COUNT) if col not in order]
        if len(order) + len(missing) != COLUMN_COUNT:
            # Duplicate or out-of-range entries: not a layout we can trust.
            return None
        return order + missing

    def _build_more_button(self) -> QToolButton:
        """The data-management actions, kept as real buttons inside a menu.

        They are hosted as QWidgetActions rather than replaced by plain
        QActions so they remain the same QToolButtons the theme pass styles
        and the enable/disable logic drives — moving them must not fork them
        into a second, subtly different set of controls.
        """
        self._more_btn = QToolButton()
        self._more_btn.setText(self._toolbar_text("meta_shell_more"))
        self._more_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._more_btn.setPopupMode(QToolButton.InstantPopup)
        self._more_btn.setFixedHeight(32)
        self._more_btn.setMinimumWidth(64)
        a11y.describe(self._more_btn, t("meta_shell_more"),
                      description=t("meta_shell_more_tooltip"),
                      tooltip=t("meta_shell_more_tooltip"))

        menu = QMenu(self._more_btn)
        self._more_menu = menu

        self._io_btn = self._make_toolbar_action(
            "meta_io_toolbar", FluentIcon.DOCUMENT, self._on_metadata_io
        )
        self._io_btn.setToolTip(t("meta_io_subtitle"))
        self._io_btn.setAccessibleName(t("meta_io_title"))

        self._backup_manager_btn = self._make_toolbar_action(
            "meta_backup_manager", FluentIcon.FOLDER, self._on_backup_manager
        )

        self._restore_btn = self._make_toolbar_action(
            "meta_restore_btn", FluentIcon.HISTORY, self._on_restore_from_backup
        )
        self._restore_btn.setToolTip(t("meta_restore_tooltip"))

        for button in (self._io_btn, self._backup_manager_btn, self._restore_btn):
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)
            # A button inside a menu does not dismiss it on its own.
            button.clicked.connect(menu.close)
            action = QWidgetAction(menu)
            action.setDefaultWidget(button)
            menu.addAction(action)

        self._more_btn.setMenu(menu)
        return self._more_btn

    def _build_state_card(self, page: QWidget) -> tuple[QFrame, QVBoxLayout]:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(11, 0, 11, 10)

        card = QFrame()
        card.setObjectName("metadataStateCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(12)
        outer.addWidget(card)
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

        self._empty_browse_btn = QPushButton(t("meta_browse_folder"))
        self._empty_browse_btn.setStyleSheet(primary_btn_style())
        self._empty_browse_btn.clicked.connect(self._on_browse)
        layout.addWidget(self._empty_browse_btn, alignment=Qt.AlignCenter)

        return page

    def _build_table_error_page(self) -> QWidget:
        """A scan failure has to be visible where the files would have been.

        It previously only reached a summary label that is never shown, so a
        folder that failed to scan looked identical to an empty one.
        """
        page = QWidget()
        card, layout = self._build_state_card(page)
        self._error_state_card = card

        layout.addWidget(EmptyStateIcon("warning", card), alignment=Qt.AlignCenter)

        self._error_title_lbl = QLabel(t("meta_scan_error_title"))
        self._error_title_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._error_title_lbl)

        self._error_body_lbl = QLabel("")
        self._error_body_lbl.setAlignment(Qt.AlignCenter)
        self._error_body_lbl.setWordWrap(True)
        self._error_body_lbl.setMaximumWidth(330)
        self._error_body_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._error_body_lbl)
        layout.addSpacing(8)

        self._error_retry_btn = QPushButton(t("meta_scan_error_retry"))
        self._error_retry_btn.setStyleSheet(btn_style())
        a11y.describe(self._error_retry_btn, t("meta_scan_error_retry"))
        self._error_retry_btn.clicked.connect(self._on_scan)
        layout.addWidget(self._error_retry_btn, alignment=Qt.AlignCenter)

        return page

    def _show_table_error(self, message: str) -> None:
        if hasattr(self, "_error_body_lbl"):
            self._error_body_lbl.setText(message)
        if hasattr(self, "_error_retry_btn"):
            self._error_retry_btn.setEnabled(self._root_folder is not None)
        if hasattr(self, "_table_stack"):
            self._table_stack.setCurrentWidget(self._table_error_page)

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

        self._loading_cancel_btn = QPushButton(t("cancel"))
        self._loading_cancel_btn.setStyleSheet(btn_style())
        self._loading_cancel_btn.clicked.connect(self.scan_cancel_requested)
        layout.addWidget(self._loading_cancel_btn, alignment=Qt.AlignCenter)

        return page

    def _show_table_content(self) -> None:
        if hasattr(self, "_table_stack"):
            self._table_stack.setCurrentWidget(self._table_content)

    def _show_table_empty(self) -> None:
        loaded_folder = self._root_folder is not None
        if hasattr(self, "_empty_title_lbl"):
            self._empty_title_lbl.setText(t(
                "meta_empty_folder_title" if loaded_folder else "meta_empty_title"))
            self._empty_body_lbl.setText(t(
                "meta_empty_folder_body" if loaded_folder else "meta_empty_body"))
            self._empty_browse_btn.setText(t(
                "meta_change_folder" if loaded_folder else "meta_browse_folder"))
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
        self._tree_rail_label = VerticalLabel(t("meta_files_folders_header"))
        self._tree_rail_label.setAlignment(Qt.AlignCenter)
        self._tree_rail_label.setStyleSheet(
            f"color: {tag_editor_colors().text_tertiary}; font-size: 10px; font-weight: 800;")
        tree_rail_layout.addWidget(self._tree_rail_label, stretch=1)
        tree_rail_layout.addStretch()
        tree_shell_layout.addWidget(self._tree_rail)

        self._tree_body = QFrame()
        self._tree_body.setMinimumWidth(self._TREE_OPEN_MIN)
        tree_layout = QVBoxLayout(self._tree_body)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)

        self._tree_header = QFrame()
        self._tree_header.setObjectName("tagEditorTreeHeader")
        self._tree_header.setFixedHeight(38)
        tree_header_row = QHBoxLayout(self._tree_header)
        tree_header_row.setContentsMargins(9, 0, 9, 0)
        tree_header_row.setSpacing(4)
        tree_header = QLabel(t("meta_files_folders_header"))
        tree_header.setStyleSheet("font-weight: 800; font-size: 12px;")
        tree_header_row.addWidget(tree_header)
        tree_header_row.addStretch()

        self._tree_add_folder_btn = QToolButton()
        self._tree_add_folder_btn.setIcon(FluentIcon.FOLDER_ADD.icon())
        self._tree_add_folder_btn.setIconSize(QSize(16, 16))
        self._tree_add_folder_btn.setFixedSize(28, 28)
        a11y.describe(self._tree_add_folder_btn, t("meta_add_folder"), tooltip=t("meta_add_folder"))
        self._tree_add_folder_btn.clicked.connect(self._on_tree_header_add_folder)
        tree_header_row.addWidget(self._tree_add_folder_btn)

        self._tree_header_collapse_btn = QToolButton()
        self._tree_header_collapse_btn.setText("×")
        self._tree_header_collapse_btn.setFixedSize(28, 28)
        a11y.describe(self._tree_header_collapse_btn, t("meta_shell_collapse_tree"),
                      tooltip=t("meta_shell_collapse_tree"))
        self._tree_header_collapse_btn.clicked.connect(self._toggle_tree_pane)
        tree_header_row.addWidget(self._tree_header_collapse_btn)
        tree_layout.addWidget(self._tree_header)

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
        tree_layout.addWidget(self._tree, stretch=1)

        self._tree_hint = QLabel(t("meta_shell_tree_hint"))
        self._tree_hint.setObjectName("tagEditorTreeHint")
        self._tree_hint.setWordWrap(True)
        self._tree_hint.setContentsMargins(8, 8, 8, 8)
        tree_layout.addWidget(self._tree_hint)
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
        table_content_layout.setContentsMargins(11, 0, 11, 10)
        table_content_layout.setSpacing(0)

        self._nav_host = QFrame()
        self._nav_host.setObjectName("tagEditorNavigation")
        self._nav_host.setFixedHeight(40)
        nav_bar = QHBoxLayout(self._nav_host)
        nav_bar.setContentsMargins(0, 6, 0, 6)
        nav_bar.setSpacing(6)
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
        # Search now lives in the toolbar, beside the folder it searches.
        self._refresh_navigation_arrow_direction()

        # Filters, zoom and column choice share the breadcrumb row because they
        # all answer "what am I looking at". What will be *written* is the
        # footer's job, and keeping the two rows apart keeps that distinction
        # visible rather than implied.
        self._excluded_chip = QToolButton()
        self._excluded_chip.setObjectName("tagEditorExcludedChip")
        self._excluded_chip.setFixedHeight(24)
        self._excluded_chip.setCheckable(True)
        a11y.describe_filter_toggle(
            self._excluded_chip, t("meta_excluded_filter_chip", n=0),
            t("meta_a11y_excluded_filter_desc"))
        self._excluded_chip.toggled.connect(self._on_excluded_chip_toggled)
        nav_bar.addWidget(self._excluded_chip)

        self._stale_chip = QToolButton()
        self._stale_chip.setObjectName("tagEditorStaleChip")
        self._stale_chip.setFixedHeight(24)
        self._stale_chip.setCheckable(True)
        self._stale_chip.setText(t("meta_external_filter", n=0))
        a11y.describe_filter_toggle(
            self._stale_chip, t("meta_external_filter", n=0),
            t("meta_a11y_external_filter_desc"))
        self._stale_chip.toggled.connect(self._on_stale_chip_toggled)
        nav_bar.addWidget(self._stale_chip)

        tbl_head = nav_bar

        # Zoom controls — the magnifying-glass +/- icons say "zoom" on their
        # own, so no separate leading icon is needed alongside them.
        self._zoom_frame = QFrame()
        self._zoom_frame.setObjectName("tagEditorZoom")
        self._zoom_frame.setFixedHeight(28)
        zoom_layout = QHBoxLayout(self._zoom_frame)
        zoom_layout.setContentsMargins(2, 2, 2, 2)
        zoom_layout.setSpacing(0)

        self._zoom_minus_btn = QPushButton()
        self._zoom_minus_btn.setIcon(FluentIcon.ZOOM_OUT.icon())
        self._zoom_minus_btn.setIconSize(QSize(12, 12))
        self._zoom_minus_btn.setFixedSize(24, 24)
        self._zoom_minus_btn.setAccessibleName(t("meta_a11y_zoom_out"))
        self._zoom_minus_btn.clicked.connect(self._on_zoom_minus)
        zoom_layout.addWidget(self._zoom_minus_btn)

        self._zoom_val_lbl = QLineEdit("100%")
        self._zoom_val_lbl.setFixedSize(48, 24)
        self._zoom_val_lbl.setAlignment(Qt.AlignCenter)
        self._zoom_val_lbl.setAccessibleName(t("meta_a11y_zoom_value"))
        self._zoom_val_lbl.editingFinished.connect(self._on_zoom_custom)
        zoom_layout.addWidget(self._zoom_val_lbl)

        self._zoom_plus_btn = QPushButton()
        self._zoom_plus_btn.setIcon(FluentIcon.ZOOM_IN.icon())
        self._zoom_plus_btn.setIconSize(QSize(12, 12))
        self._zoom_plus_btn.setFixedSize(24, 24)
        self._zoom_plus_btn.setAccessibleName(t("meta_a11y_zoom_in"))
        self._zoom_plus_btn.clicked.connect(self._on_zoom_plus)
        zoom_layout.addWidget(self._zoom_plus_btn)
        tbl_head.addWidget(self._zoom_frame)
        
        tbl_head.addSpacing(10)
        tbl_head.addWidget(self._columns_button())
        table_content_layout.addWidget(self._nav_host)

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
            FilenameDelegate(self._table, icon_provider=self._track_icon, show_checkbox=False),
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
            if col == COL_CHECK:
                self._table.setColumnHidden(col, True)
            elif col == COL_FILENAME:
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
            saved_order = self._migrate_saved_column_order(
                self._cfg.tag_editor_column_order)

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

        self._table_card = QFrame()
        self._table_card.setObjectName("tagEditorTableCard")
        table_card_layout = QVBoxLayout(self._table_card)
        table_card_layout.setContentsMargins(1, 1, 1, 1)
        table_card_layout.setSpacing(0)
        table_card_layout.addWidget(self._table, stretch=1)
        table_card_layout.addWidget(self._build_table_status_bar())
        table_content_layout.addWidget(self._table_card, stretch=1)
        self._table_stack.addWidget(self._table_content)
        self._table_empty_page = self._build_table_empty_page()
        self._table_loading_page = self._build_table_loading_page()
        self._table_error_page = self._build_table_error_page()
        self._table_stack.addWidget(self._table_empty_page)
        self._table_stack.addWidget(self._table_loading_page)
        self._table_stack.addWidget(self._table_error_page)
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

        self._initial_body_sizes = self._restore_body_sizes()
        self._apply_body_sizes(self._initial_body_sizes, save=False)
        # QSplitter is constructed before the panel receives its real window
        # width.  Qt otherwise compresses both side panes to their old minima
        # and keeps those accidental widths after the first resize.  Reapply
        # the reference allocation once actual geometry is available.
        QTimer.singleShot(0, self._restore_initial_body_layout)

        # Set initial table zoom level
        self._set_zoom(self._zoom_level)

        # Connect resize signal and disable the initial resize-ignoring flag
        hdr.sectionResized.connect(self._on_section_resized)
        self._ignore_header_resize = False

        return splitter

    def _restore_initial_body_layout(self) -> None:
        if not hasattr(self, "_body_splitter"):
            return
        self._apply_body_sizes(list(self._initial_body_sizes), save=False)
        self._apply_responsive_layout(self.width())

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
        header.setObjectName("tagEditorInspectorHeader")
        self._inspector_header = header
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(11, 9, 11, 0)
        header_layout.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(7)
        self._inspector_title_lbl = QLabel(t("meta_inspector_no_selection_header"))
        self._inspector_title_lbl.setStyleSheet("font-weight: 800; font-size: 12px;")
        title_row.addWidget(self._inspector_title_lbl)
        self._inspector_selection_lbl = QLabel("")
        self._inspector_selection_lbl.setStyleSheet(
            f"color: {tag_editor_colors().text_secondary}; font-size: 10.5px;")
        title_row.addWidget(self._inspector_selection_lbl)
        title_row.addStretch()
        self._inspector_collapse_btn = QToolButton()
        self._inspector_collapse_btn.setText("‹" if QApplication.layoutDirection() == Qt.RightToLeft else "›")
        self._inspector_collapse_btn.setFixedSize(28, 28)
        a11y.describe(self._inspector_collapse_btn, t("meta_shell_collapse_inspector"),
                      tooltip=t("meta_shell_collapse_inspector"))
        self._inspector_collapse_btn.clicked.connect(lambda: self._set_inspector_collapsed(True))
        title_row.addWidget(self._inspector_collapse_btn)
        header_layout.addLayout(title_row)
        header_layout.addSpacing(8)

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

        # Three modes answering three different questions — what are these
        # files, what do I want done to them, what is wrong with them — each
        # with its own sub-tabs. The flat page list below is deliberately
        # ordered so its first eight entries are the eight tools the old rail
        # exposed, in the same order: _select_inspector_tool() is part of the
        # panel's API and indices 0..7 have to keep meaning what they meant.
        inspector_panes = [
            # (mode, title, icon, page)
            ("edit",  t("meta_edit_tags_group"),        FluentIcon.EDIT,        self._inspector),
            ("tools", t("meta_action_engine_title"),    FluentIcon.TAG,         self._build_action_engine_page()),
            ("tools", t("meta_group_from_filename"),    FluentIcon.PASTE,
             self._build_inspector_actions(
                 ("title_strip", "title_full", "track_num", "split_at"),
                 extra=self._build_apply_value_group())),
            ("tools", t("meta_group_cleanup"),          FluentIcon.ERASE_TOOL,
             self._build_inspector_actions(
                 None,
                 sections=(
                     ("meta_section_text_cleanup", ("normalize_spaces", "strip_junk", "album_artist")),
                     ("meta_section_clear_fields", (
                         "clear_title", "clear_artist", "clear_album", "clear_album_artist",
                         "clear_track_num", "clear_year", "clear_genre", "clear_comments",
                     )),
                 ),
             )),
            ("tools", t("meta_rename_group"),           FluentIcon.DOCUMENT,
             self._build_inspector_actions(("clean_filename", "strip_filename_numbering"))),
            ("check", t("meta_duplicates_tools_title"), FluentIcon.FINGERPRINT, self._build_duplicate_tools_page()),
            ("tools", t("meta_online_title"),           FluentIcon.SEARCH,      self._build_online_metadata_page()),
            ("check", t("meta_problems_title"),         FluentIcon.INFO,        self._build_problems_page()),
            # Everything past here is new to the redesign.
            ("edit",  t("meta_inspector_artwork_section"),         FluentIcon.PHOTO,   self._build_edit_artwork_page()),
            ("edit",  t("meta_inspector_lyrics_section"),          FluentIcon.FONT,    self._build_edit_lyrics_page()),
            ("edit",  t("meta_inspector_replaygain_section"),      FluentIcon.VOLUME,  self._build_edit_gain_page()),
            ("edit",  t("meta_inspector_file_properties_section"), FluentIcon.INFO,    self._build_edit_properties_page()),
            ("tools", t("meta_auto_btn").strip(),                  FluentIcon.BRUSH,   self._build_tools_auto_page()),
            ("check", t("meta_pending_tab"),                       FluentIcon.VIEW,    self._build_check_pending_page()),
            ("check", t("meta_external_tab"),                      FluentIcon.SYNC,    self._build_check_external_page()),
        ]

        self._inspector_pane_modes: list[str] = []
        self._inspector_tool_titles: list[str] = []
        self._inspector_tool_buttons: list[QPushButton] = []
        self._inspector_tool_kinds: list = []
        for index, (mode, title, icon, page) in enumerate(inspector_panes):
            self._inspector_pane_modes.append(mode)
            self._inspector_tool_titles.append(title)
            self._inspector_tool_kinds.append(icon)
            self._inspector_pages.addWidget(page)

            chip = QPushButton(title)
            chip.setObjectName("tagEditorSubtab")
            chip.setCheckable(True)
            chip.setFixedHeight(25)
            a11y.describe(chip, title, tooltip=title)
            chip.clicked.connect(lambda _=False, i=index: self._toggle_inspector_tool(i))
            self._inspector_tool_buttons.append(chip)

        header_layout.addWidget(self._build_inspector_mode_tabs())
        header_layout.addWidget(self._build_inspector_subtabs())
        content_layout.addWidget(header)
        content_layout.addWidget(self._inspector_pages, stretch=1)

        self._inspector_rail = QFrame()
        self._inspector_rail.setFixedWidth(self._INSPECTOR_RAIL_WIDTH)
        rail_layout = QVBoxLayout(self._inspector_rail)
        rail_layout.setContentsMargins(5, 6, 5, 6)
        rail_layout.setSpacing(6)

        # Collapsed, the rail offers the three modes rather than every pane:
        # a 40px strip cannot carry twelve legible targets.
        for mode, icon in (("edit", FluentIcon.EDIT), ("tools", FluentIcon.DEVELOPER_TOOLS),
                           ("check", FluentIcon.CERTIFICATE)):
            btn = QPushButton()
            btn.setFixedSize(30, 30)
            self._set_tool_button_icon(btn, icon)
            label = self._inspector_mode_label(mode)
            a11y.describe(btn, label, tooltip=label)
            btn.clicked.connect(
                lambda _=False, m=mode: self._open_inspector_mode(m))
            self._inspector_rail_buttons[mode] = btn
            rail_layout.addWidget(btn)
            rail_label = QLabel(label)
            rail_label.setAlignment(Qt.AlignCenter)
            rail_label.setStyleSheet(
                f"color: {tag_editor_colors().text_secondary}; font-size: 9px; font-weight: 800;")
            rail_layout.addWidget(rail_label)
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
            tracks = self._get_selected_tracks()
            if tracks:
                self._populate_track_inspector(tracks)

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
            prompts.show_warning(self, t("meta_online_title"), t("meta_online_select_files")); return
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
        extra: Optional[QWidget] = None,
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
        if extra is not None:
            layout.addWidget(extra)
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
            prompts.show_warning(self, t("meta_problems_title"), t("meta_problems_no_safe_fix"))
            return
        value, ok = prompts.get_text(self, t("meta_problems_fix_selected"), t("meta_problems_value"), text=text)
        if not ok or not value.strip():
            return
        self.problem_fix_preview_requested.emit(list(issue_ids), value.strip())

    def on_problem_fix_preview(self, preview) -> None:
        """Render the immutable Phase 9 action preview; never reconstruct it."""
        dialog = QDialog(self); mark_tag_editor_dialog(dialog)
        dialog.setWindowTitle(t("meta_problems_preview_title")); dialog.resize(760, 430)
        layout = QVBoxLayout(dialog)
        add_header(layout, t("meta_problems_preview_title"),
                   t("meta_problems_preview_subtitle"), icon=FluentIcon.EDIT.icon())
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
        prompts.show_warning(self, t("meta_problems_title"), message)

    _INSPECTOR_MODE_KEYS = {
        "edit": "meta_inspector_mode_edit",
        "tools": "meta_inspector_mode_tools",
        "check": "meta_inspector_mode_check",
    }

    @classmethod
    def _inspector_mode_label(cls, mode: str) -> str:
        return t(cls._INSPECTOR_MODE_KEYS[mode])

    def _build_inspector_mode_tabs(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("tagEditorModeTabs")
        bar.setFixedHeight(35)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(3)
        self._inspector_mode_buttons: dict[str, QPushButton] = {}
        for mode in ("edit", "tools", "check"):
            label = self._inspector_mode_label(mode)
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setObjectName("tagEditorModeTab")
            btn.setFixedHeight(27)
            a11y.describe(btn, label, tooltip=label)
            btn.clicked.connect(lambda _=False, m=mode: self._open_inspector_mode(m))
            self._inspector_mode_buttons[mode] = btn
            layout.addWidget(btn, stretch=1)
        return bar

    def _build_inspector_subtabs(self) -> QScrollArea:
        """Chips for the panes of the active mode; rebuilt whenever it changes."""
        self._inspector_subtab_host = QWidget()
        self._inspector_subtab_layout = QHBoxLayout(self._inspector_subtab_host)
        self._inspector_subtab_layout.setContentsMargins(0, 8, 0, 9)
        self._inspector_subtab_layout.setSpacing(5)
        self._inspector_subtab_layout.addStretch()

        scroll = QScrollArea()
        scroll.setObjectName("tagEditorSubtabs")
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(42)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self._inspector_subtab_host)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._inspector_subtab_scroll = scroll
        return scroll

    def _open_inspector_mode(self, mode: str) -> None:
        """Switch modes, landing on that mode's first pane."""
        if self._right_collapsed:
            self._set_inspector_collapsed(False)
        if self._inspector_pane_modes[self._active_inspector_tool] == mode:
            return
        first_for_mode = {"edit": 0, "tools": 12, "check": 13}
        self._select_inspector_tool(first_for_mode[mode])

    def _rebuild_inspector_subtabs(self) -> None:
        active_mode = self._inspector_pane_modes[self._active_inspector_tool]
        while self._inspector_subtab_layout.count():
            item = self._inspector_subtab_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)
        visual_order = {
            "edit": (0, 8, 9, 10, 11),
            "tools": (12, 1, 2, 3, 4, 6),
            "check": (13, 7, 5, 14),
        }
        for index in visual_order[active_mode]:
            self._inspector_subtab_layout.addWidget(self._inspector_tool_buttons[index])
            self._inspector_tool_buttons[index].setVisible(True)
        self._inspector_subtab_layout.addStretch()

    def _select_inspector_tool(self, index: int) -> None:
        self._active_inspector_tool = index
        if hasattr(self, "_inspector_pages"):
            self._inspector_pages.setCurrentIndex(index)
        if self._right_collapsed:
            self._set_inspector_collapsed(False)
        if hasattr(self, "_inspector_subtab_layout"):
            self._rebuild_inspector_subtabs()
        self._refresh_tool_button_states()

    def _toggle_inspector_tool(self, index: int) -> None:
        if self._active_inspector_tool == index and not self._right_collapsed:
            self._set_inspector_collapsed(True)
            return
        self._select_inspector_tool(index)

    def _refresh_tool_button_states(self) -> None:
        """Track which pane is open.

        Appearance for the mode tabs and sub-tab chips comes from their
        stylesheets' :checked rule (see _apply_shell_theme), so this only sets
        state. Checked state is also what carries "this one is open" to a
        screen reader and in high contrast, where a background colour does not.
        """
        c = get_colors()
        button_qss = (
            "QPushButton { background: transparent; border: none; border-radius: 8px; padding: 3px; }"
            f"QPushButton:hover {{ background: {c.surface2}; }}"
        )
        active_qss = (
            f"QPushButton {{ background: {c.surface2}; border: 1px solid {c.border};"
            " border-radius: 8px; padding: 3px; }"
        )
        open_pane = self._active_inspector_tool
        expanded = not self._right_collapsed
        active_mode = (
            self._inspector_pane_modes[open_pane]
            if self._inspector_pane_modes else None
        )

        for idx, btn in enumerate(getattr(self, "_inspector_tool_buttons", [])):
            btn.setChecked(idx == open_pane and expanded)
        for mode, btn in getattr(self, "_inspector_mode_buttons", {}).items():
            btn.setChecked(mode == active_mode and expanded)

        # The collapsed rail is icon-only, so it keeps the icon-button styling.
        for mode, btn in getattr(self, "_inspector_rail_buttons", {}).items():
            btn.setStyleSheet(active_qss if mode == active_mode else button_qss)
        if hasattr(self, "_tree_toggle_btn"):
            self._tree_toggle_btn.setStyleSheet(button_qss)
            self._set_tool_button_icon(self._tree_toggle_btn, FluentIcon.FOLDER)


    # ── Toolbar handlers ──────────────────────────────────────────────────────

    def _on_tree_header_add_folder(self) -> None:
        parent = self._navigation.current or self._root_folder
        if parent is not None:
            self._on_tree_add_folder(Path(parent))

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
                prompts.show_warning(
                    self, t("meta_apply_blocked_title"),
                    t("meta_external_apply_blocked", n=len(blocked)))
                self._workspace.set_selected_items([blocked[0]])
                self._populate_track_inspector([blocked[0]])
            self._refresh_checked_scope_state()
            return
        if ApplyReviewPolicy.requires_full_review(self._workspace.change_set):
            self._on_review_changes()
            # Review may exclude files or revert proposals.  Never apply the
            # stale pre-review list merely because it was safe a moment ago.
            candidates = self._workspace.apply_candidates()
            if not candidates:
                self._refresh_checked_scope_state()
                return
        if self.isVisible():
            confirmation = ApplyConfirmationDialog(
                self._workspace.change_set.summary(),
                candidate_count=len(candidates),
                blocker_count=len(self._workspace.apply_blockers()),
                parent=self,
            )
            if confirmation.exec() != QDialog.DialogCode.Accepted:
                return
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
        mark_tag_editor_dialog(dialog)
        dialog.setWindowTitle(t("meta_pending_changes"))
        dialog.setMinimumSize(860, 420)
        layout = QVBoxLayout(dialog)
        add_header(layout, t("meta_pending_changes"), t("meta_review_subtitle"),
                   icon=FluentIcon.VIEW.icon())
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
                prompts.show_warning(dialog, t("meta_review_blocker_details"), "\n".join(sorted(set(messages))))
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
            prompts.show_warning(
                self, t("md_restore_invalid_title"), t("md_restore_invalid_msg"),
                details=str(exc),
            )
            return
        if not records:
            prompts.show_warning(self, t("md_restore_invalid_title"), t("md_restore_empty_msg"))
            return

        existing = [p for p, _tags in records if p.exists()]
        missing_n = len(records) - len(existing)
        if not existing:
            prompts.show_warning(self, t("md_restore_invalid_title"),
                         t("md_restore_all_missing_msg"))
            return

        msg = t("md_restore_confirm_msg", n=len(existing), backup=Path(path).name)
        if missing_n:
            msg += "\n" + t("md_restore_missing_note", n=missing_n)
        names = "\n".join(f"•  {p.name}" for p in existing[:10])
        if len(existing) > 10:
            names += "\n" + t("md_restore_more_files", n=len(existing) - 10)
        msg += "\n\n" + names

        if prompts.confirm(
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
            prompts.show_warning(self, t("md_restore_summary_title"), summary,
                         details=problem_lines)
        else:
            prompts.show_info(self, t("md_restore_summary_title"), summary,
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
            self._inspector_title_lbl.setText(t("meta_inspector_no_selection_header"))
            self._inspector_selection_lbl.clear()
            self._inspector.setCurrentIndex(PAGE_FOLDER)
        else:
            self._inspector_title_lbl.setText(t("meta_inspector_no_selection_header"))
            self._inspector_selection_lbl.clear()
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
            prompts.show_warning(
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

    def _on_artwork_choose_adaptive(self) -> None:
        """Prototype's single Choose button: append only when nothing exists."""
        tracks = self._get_selected_tracks()
        if not tracks:
            return
        has_artwork = any(track.original.artwork.entries for track in tracks)
        can_append = all(track.format_id != "m4a" for track in tracks)
        self._on_artwork_choose(add=not has_artwork and can_append)

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
            prompts.show_warning(self.window(), t("meta_inspector_artwork_section"), t(exc.key))
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
            prompts.show_warning(self.window(), t("meta_inspector_artwork_section"), t("meta_artwork_invalid_image"))
            return
        payload = QByteArray(); buffer = QBuffer(payload); buffer.open(QBuffer.WriteOnly)
        image.save(buffer, "PNG"); buffer.close()
        from core.artwork import ArtworkValidationError, validate_artwork_bytes
        try:
            entry = validate_artwork_bytes(bytes(payload))
        except ArtworkValidationError as exc:
            prompts.show_warning(self.window(), t("meta_inspector_artwork_section"), t(exc.key)); return
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
            prompts.show_warning(self.window(), t("meta_artwork_export_title"), t(exc.key))

    def _on_replaygain_clear_track(self) -> None:
        self._propose_replaygain_clear({REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK})

    def _on_replaygain_clear_album(self) -> None:
        self._propose_replaygain_clear({REPLAYGAIN_ALBUM_GAIN, REPLAYGAIN_ALBUM_PEAK})

    def _on_replaygain_clear_all(self) -> None:
        self._propose_replaygain_clear(set(REPLAYGAIN_FIELDS))

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
        if prompts.confirm(
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
            # The Tools > Auto arrange page lists what the button will run.
            self._refresh_auto_enabled_list()


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
        # Only show the failure card when there is nothing to fall back to;
        # a failed refresh of an already-loaded folder must not blank the table.
        if not self._model.get_all_tracks():
            self._show_table_error(msg)

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
        """Keep refresh availability in sync without exposing monitor internals."""
        value = getattr(state, "value", str(state))
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
            prompts.show_warning(
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
        if self.isVisible():
            ApplyResultDialog(error_message=message, parent=self).exec()

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
        if self.isVisible():
            ApplyResultDialog(result, parent=self).exec()

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
        mark_tag_editor_dialog(box)
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
            if prompts.confirm(
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
        mark_tag_editor_dialog(box)
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
        mark_tag_editor_dialog(box)
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
        if hasattr(self, "_rescan_action"):
            self._rescan_action.setEnabled(
                self._root_folder is not None
                and not self._is_scanning
                and not self._is_applying
                and not self._is_restoring
            )
        self._refresh_path_chip()
        self._refresh_footer()
        self._refresh_check_pages()
        self._refresh_toolbar_action_styles()

    def _refresh_path_chip(self) -> None:
        """Show the active folder, elided from the left so the leaf survives."""
        if not hasattr(self, "_path_chip"):
            return
        if self._root_folder is None:
            self._path_chip.setText(t("meta_shell_no_folder"))
            self._path_chip.setToolTip("")
            return
        full = str(self._root_folder)
        metrics = self._path_chip.fontMetrics()
        self._path_chip.setText(
            metrics.elidedText(full, Qt.ElideLeft, self._path_chip.maximumWidth() - 16)
        )
        self._path_chip.setToolTip(full)

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
        if hasattr(self, "_inspector_title_lbl"):
            self._inspector_title_lbl.setText(
                t("meta_tracks_selected_summary", n=len(tracks), plural=plural))
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
        if hasattr(self, "_inspector_selection_lbl"):
            self._inspector_selection_lbl.setText(
                t("mt_status_read_only") if snapshot.editable_count == 0 else "")
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
            self._insp_artwork_preview.setText(t("meta_artwork_mixed_short"))
        elif stored_primary is None:
            artwork_text = t("meta_artwork_none")
            self._insp_artwork_preview.setPixmap(QPixmap())
            self._insp_artwork_preview.setText(t("meta_artwork_none_short"))
        else:
            self._insp_artwork_preview.clear()
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
            self._insp_artwork_proposed_preview.setPixmap(QPixmap())
            self._insp_artwork_proposed_preview.setText(t("meta_artwork_drop_prompt"))
        # The reference always shows both cover slots.  Hiding the proposed
        # slot made the entire page jump sideways before/after an edit.
        self._insp_artwork_proposed_label.setVisible(True)
        self._insp_artwork_proposed_preview.setVisible(True)
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
        self._insp_rg_clear_all_btn.setEnabled(replay_supported)
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
            def display_property(key: str, value: object) -> str:
                if value is None or value == "":
                    return t("meta_inspector_empty_value")
                return self._format_property_value(key, value)

            property_values = {
                "path": str(tracks[0].path),
                "format_id": display_property(
                    "format_id", props.get("format_id", tracks[0].format_id)),
                "size_bytes": display_property("size_bytes", props.get("size_bytes")),
                "duration_seconds": display_property(
                    "duration_seconds", props.get("duration_seconds", 0)),
                "bitrate": display_property("bitrate", props.get("bitrate")),
                "sample_rate": display_property("sample_rate", props.get("sample_rate")),
                "channels": display_property("channels", props.get("channels")),
                "modified_time": display_property(
                    "modified_time", props.get("modified_time")),
                "capability": (
                    t("meta_property_capability_full")
                    if snapshot.editable_count else t("mt_status_read_only")
                ),
            }
            for key, label in self._insp_property_values.items():
                label.setText(str(property_values.get(key, "")))
            self._insp_property_table.setVisible(True)
            self._insp_properties.setVisible(False)
            for button in (self._insp_property_open_btn, self._insp_property_reveal_btn,
                           self._insp_property_copy_btn):
                button.setEnabled(True)
            state = getattr(tracks[0], "external_state", "current")
            self._insp_external_status.setText(
                t("meta_external_inspector_status",
                  state=t(f"meta_external_state_{state}")))
            self._insp_external_review_btn.setVisible(
                getattr(tracks[0], "external_conflict", None) is not None)
        else:
            self._insp_properties.setText(t("meta_property_single_selection_only"))
            self._insp_properties.setVisible(True)
            for label in self._insp_property_values.values():
                label.clear()
            self._insp_property_table.setVisible(False)
            for button in (self._insp_property_open_btn, self._insp_property_reveal_btn,
                           self._insp_property_copy_btn):
                button.setEnabled(False)
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
        
        table_colors = tag_editor_colors()
        # Win11 Details View: flat header (no per-section vertical borders,
        # no bold, muted color, single underline). Capsule paint handles
        # selection — keep selection-background-color transparent so Qt
        # doesn't overdraw it with a flat rectangle.
        self._table.setStyleSheet(
            f"QTableView {{ background: {table_colors.surface}; color: {table_colors.text_primary};"
            f"  border: none; border-radius: 0;"
            f"  selection-background-color: transparent; selection-color: {table_colors.text_primary};"
            f"  font-size: {font_size}pt; }}"
            "QTableView::item { background: transparent; border: none; }"
            f"QHeaderView::section {{ background: {table_colors.surface3};"
            f"  color: {table_colors.text_secondary};"
            f"  border: none;"
            f"  padding: 0 8px; height: 36px;"
            f"  font-size: {font_size}pt; font-weight: 800; }}"
            f"QHeaderView::section:hover {{ color: {table_colors.text_primary}; }}"
            f"QTableCornerButton::section {{ background: {table_colors.surface3};"
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


    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout(event.size().width())

    def _apply_responsive_layout(self, width: int) -> None:
        """Mirror the prototype's 1180/1040 responsive breakpoints."""
        if not hasattr(self, "_toolbar_bar"):
            return
        compact = width <= 1180
        narrow = width <= 1040
        mode = "narrow" if narrow else "compact" if compact else "wide"
        for widget in (self._path_chip,):
            widget.setVisible(not compact)
        self._search_edit.setFixedWidth(150 if narrow else 180 if compact else 230)
        if hasattr(self, "_footer_desc"):
            self._footer_desc.setVisible(not compact)

        if not hasattr(self, "_body_splitter"):
            return
        sizes = self._body_splitter.sizes()
        if len(sizes) != 3:
            return
        changed = False

        if narrow:
            if sizes[0] > self._TREE_RAIL_WIDTH + 4:
                self._last_tree_width = max(self._TREE_OPEN_MIN, sizes[0])
                sizes[1] += sizes[0] - self._TREE_RAIL_WIDTH
                sizes[0] = self._TREE_RAIL_WIDTH
                self._responsive_forced_tree_collapse = True
                changed = True
        else:
            tree_target = min(self._last_tree_width, 195) if compact else self._last_tree_width
            if self._responsive_forced_tree_collapse:
                available = max(0, sizes[1] - self._TABLE_OPEN_MIN)
                take = min(available, max(0, tree_target - sizes[0]))
                sizes[0] += take
                sizes[1] -= take
                if sizes[0] >= self._TREE_OPEN_MIN:
                    self._responsive_forced_tree_collapse = False
                changed = changed or take > 0
            elif sizes[0] > self._TREE_RAIL_WIDTH + 4:
                delta = tree_target - sizes[0]
                if delta > 0:
                    take = min(max(0, sizes[1] - self._TABLE_OPEN_MIN), delta)
                    sizes[0] += take
                    sizes[1] -= take
                    changed = changed or take > 0
                elif delta < 0:
                    sizes[0] += delta
                    sizes[1] -= delta
                    changed = True

        inspector_target = 300 if narrow else 330 if compact else self._last_inspector_width
        if sizes[2] > inspector_target:
            sizes[1] += sizes[2] - inspector_target
            sizes[2] = inspector_target
            changed = True
        elif sizes[2] > self._INSPECTOR_RAIL_WIDTH + 4 and sizes[2] < inspector_target:
            take = min(max(0, sizes[1] - self._TABLE_OPEN_MIN), inspector_target - sizes[2])
            sizes[2] += take
            sizes[1] -= take
            changed = changed or take > 0
        if changed:
            self._apply_body_sizes(sizes, save=False)
        self._responsive_mode = mode


    def closeEvent(self, event) -> None:
        if not self.shutdown_artwork_workers():
            event.ignore()
            return
        super().closeEvent(event)
