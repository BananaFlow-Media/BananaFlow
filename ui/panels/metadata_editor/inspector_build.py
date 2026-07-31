"""
ui/panels/metadata_editor/inspector_build.py  –  Tag Editor
==============================================================================
Inspector page construction: the empty/folder/tracks pages, the
magic-operation rows, and their shared side buttons.

Extracted from panel.py unchanged; MetadataEditorPanel mixes this in,
so every attribute reference resolves exactly as before.
"""

from __future__ import annotations

from . import prompts

from .widgets import ArtworkDropPreview

from typing import TYPE_CHECKING, Optional
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QFileInfo,
    QItemSelection,
    QPoint,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices
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
from core.filesystem_monitoring import is_external_change
from ui import a11y
from ui.i18n import t
from .dialogs import AutoArrangeSettingsDialog, CleanSettingsDialog, MoreColumnsDialog
from .shared import (
    DEFAULT_AUTO_OPS,
    DEFAULT_COL_WIDTHS,
    MAGIC_OP_DEFS,
    PAGE_EMPTY,
    PAGE_FOLDER,
    PAGE_TRACKS,
    bold_font,
    btn_style,
    compact_clear_button_qss,
    dim_hex,
    op_row_qss,
    primary_btn_style,
    tag_editor_colors,
)
from .widgets import (
    ElidedLabel,
    ElidedPushButton,
    ElidedToolButton,
    OpRow,
    ResponsiveButtonFlow,
)


class InspectorBuildMixin:
    """Inspector page construction: the empty/folder/tracks pages, the"""

    def _build_inspector_empty(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignCenter)
        lbl = QLabel(t("meta_select_files_prompt"))
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color: {tag_editor_colors().text_tertiary}; font-size: 13px;")
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
        note.setStyleSheet(f"color: {tag_editor_colors().text_secondary}; font-size: 12px;")
        layout.addWidget(note)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        return scroll

    def _build_inspector_tracks(self) -> QScrollArea:
        """The Edit > Fields page.

        Artwork, lyrics, ReplayGain and properties used to be stacked below
        this in one long scroll; they are now sibling tabs, so this page is
        the fields alone.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        # Every inspector body uses this padding; keeping it identical across
        # the pages is what stops the panel shifting as you switch tabs.
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)


        self._insp_tracks_title = QLabel(t("meta_tracks_selected_count", n=0))
        self._insp_tracks_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._insp_tracks_title.setVisible(False)
        layout.addWidget(self._insp_tracks_title)

        self._insp_capability = QLabel()
        self._insp_capability.setWordWrap(True)
        self._insp_capability.setObjectName("tagEditorInspectorNote")
        layout.addWidget(self._insp_capability)

        layout.addWidget(self._build_fields_group())
        layout.addStretch()
        return self._inspector_scroll(w)

    @staticmethod
    def _inspector_scroll(inner: QWidget) -> QScrollArea:
        """Every inspector page scrolls the same way."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        return scroll

    def _build_fields_group(self) -> QGroupBox:
        """The 21 metadata fields, their per-field Clear buttons, and the button
        that turns the draft into proposals."""
        fields_grp = QGroupBox("")
        fields_grp.setObjectName("tagEditorFields")
        # The application-wide scroll-area skin is light gray.  The reference
        # field list is a continuous white surface, so establish that surface
        # here instead of letting it show through between the controls.
        fields_grp.setStyleSheet(
            f"QGroupBox#tagEditorFields {{ background: {tag_editor_colors().surface}; border: none; }}")
        fields_layout = QVBoxLayout(fields_grp)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.setSpacing(6)

        self._insp_fields: dict[str, QLineEdit] = {}
        self._insp_clear_buttons: dict[str, QToolButton] = {}
        self._insp_advanced_rows: list[QWidget] = []

        def _field(field_name: str, label: str, *, advanced: bool = False) -> QLineEdit:
            # Field captions are UI labels, not prose: the colon is visual
            # clutter here and makes Hebrew labels look offset from the input.
            display_label = label.rstrip().rstrip(":").rstrip()
            row_widget = QWidget()
            row_widget.setObjectName("tagEditorFieldRow")
            row_widget.setStyleSheet(
                f"QWidget#tagEditorFieldRow {{ background: {tag_editor_colors().surface}; border: none; }}")
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(7)
            # Fixed so the caption column stays aligned down the page; it
            # elides rather than widening when a label outgrows the column.
            lbl = ElidedLabel(display_label)
            lbl.setFixedWidth(88)
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignRight | Qt.AlignAbsolute)
            lbl.setStyleSheet(
                f"background: transparent; border: none; color: {tag_editor_colors().text_secondary};"
                " font-size: 11.5px; font-weight: 700;")
            edit = QLineEdit()
            edit.setFixedHeight(29)
            # Right alignment applies to values in every language, including
            # LTR values such as artist names and genres.
            edit.setAlignment(Qt.AlignVCenter | Qt.AlignRight | Qt.AlignAbsolute)
            edit.setPlaceholderText(t("meta_mixed_placeholder"))
            # The visible QLabel is a sibling, not a buddy, so the field itself
            # must carry the name a screen reader announces on focus.
            lbl.setBuddy(edit)
            a11y.describe(edit, display_label)
            edit.textEdited.connect(
                lambda text, name=field_name: self._mark_insp_field_dirty(name, text)
            )
            clear_btn = QToolButton()
            clear_btn.setText(t("meta_inspector_clear_short"))
            clear_btn.setFixedSize(30, 29)
            # Every field row has a button reading just "Clear"; only the
            # accessible name can say which field this one clears.
            a11y.describe(
                clear_btn,
                t("meta_a11y_clear_named_field", field=display_label),
                tooltip=t("meta_inspector_clear_field"))
            clear_btn.clicked.connect(
                lambda _checked=False, name=field_name, target=edit: self._clear_insp_field(name, target)
            )
            row.addWidget(lbl)
            row.addWidget(edit)
            row.addWidget(clear_btn)
            fields_layout.addWidget(row_widget)
            if advanced:
                row_widget.setVisible(False)
                self._insp_advanced_rows.append(row_widget)
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
        for index, (field_name, label_key) in enumerate(field_specs):
            _field(field_name, t(label_key), advanced=index >= 10)

        # Compatibility aliases used by existing panel tests and handlers.
        self._insp_title = self._insp_fields["title"]
        self._insp_artist = self._insp_fields["artist"]
        self._insp_album = self._insp_fields["album"]
        self._insp_album_artist = self._insp_fields["album_artist"]
        self._insp_track = self._insp_fields["track_num"]

        self._insp_advanced_btn = ElidedToolButton()
        self._insp_advanced_btn.setObjectName("tagEditorAdvancedFields")
        self._insp_advanced_btn.setText(t("meta_show_additional_fields"))
        self._insp_advanced_btn.setIcon(FluentIcon.ADD.icon(color=tag_editor_colors().accent))
        self._insp_advanced_btn.setIconSize(QSize(14, 14))
        self._insp_advanced_btn.setCheckable(True)
        # LTR is intentional: it pins the plus/minus at the physical left
        # edge, while the Hebrew label stays aligned at the right edge.
        self._insp_advanced_btn.setLayoutDirection(Qt.LeftToRight)
        self._insp_advanced_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._insp_advanced_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._insp_advanced_btn.setFixedHeight(31)
        self._insp_advanced_btn.clicked.connect(self._toggle_advanced_fields)
        fields_layout.addWidget(self._insp_advanced_btn)

        btn_apply_fields = ElidedPushButton(t("meta_apply_to_selection"))
        btn_apply_fields.setProperty("accentRole", "primary")
        btn_apply_fields.setStyleSheet(primary_btn_style())
        btn_apply_fields.clicked.connect(self._on_insp_apply_fields)
        self._selection_scope_buttons.append(btn_apply_fields)
        fields_layout.addWidget(btn_apply_fields)

        # Copy one file's tags onto others.  Deliberately two steps rather than
        # a "copy from the row above" shortcut: the copied set is shown in the
        # status line before it can be pasted anywhere.
        transfer = ResponsiveButtonFlow(fill=True)
        self._insp_copy_tags_btn = ElidedPushButton(t("meta_copy_tags"))
        self._insp_copy_tags_btn.setStyleSheet(btn_style())
        a11y.describe(self._insp_copy_tags_btn, t("meta_copy_tags"),
                      description=t("meta_copy_tags_hint"))
        self._insp_copy_tags_btn.clicked.connect(self._on_copy_tags)
        self._insp_paste_tags_btn = ElidedPushButton(t("meta_paste_tags"))
        self._insp_paste_tags_btn.setStyleSheet(btn_style())
        a11y.describe(self._insp_paste_tags_btn, t("meta_paste_tags"),
                      description=t("meta_paste_tags_hint"))
        self._insp_paste_tags_btn.clicked.connect(self._on_paste_tags)
        self._insp_paste_tags_btn.setEnabled(False)
        for button in (self._insp_copy_tags_btn, self._insp_paste_tags_btn):
            transfer.add_button(button)
            self._selection_scope_buttons.append(button)
        fields_layout.addWidget(transfer)

        self._insp_tag_clipboard_lbl = QLabel("")
        self._insp_tag_clipboard_lbl.setWordWrap(True)
        self._insp_tag_clipboard_lbl.setObjectName("tagEditorDialogNote")
        fields_layout.addWidget(self._insp_tag_clipboard_lbl)
        return fields_grp

    def _toggle_advanced_fields(self, shown: bool) -> None:
        for row in self._insp_advanced_rows:
            row.setVisible(bool(shown))
        # Both halves have to follow ``shown``: the label used to be pinned to
        # "hide", so once the rows were collapsed the button claimed it would
        # hide fields that were already hidden.
        self._insp_advanced_btn.setText(t(
            "meta_hide_additional_fields" if shown else "meta_show_additional_fields"))
        self._insp_advanced_btn.setIcon(
            (FluentIcon.REMOVE if shown else FluentIcon.ADD).icon(
                color=tag_editor_colors().accent))

    def _build_lyrics_group(self) -> QGroupBox:
        """Embedded lyrics with their language and descriptor.

        An empty editor never clears lyrics on its own -- removing them is an
        explicit action, so a stray keystroke cannot silently discard a file's
        words."""
        lyrics_grp = QGroupBox("")
        lyrics_layout = QVBoxLayout(lyrics_grp)
        lyrics_layout.setContentsMargins(0, 0, 0, 0)
        lyrics_layout.setSpacing(8)
        lyrics_note = QLabel(t("meta_lyrics_editor_note"))
        lyrics_note.setObjectName("tagEditorInspectorNote")
        lyrics_note.setWordWrap(True)
        lyrics_layout.addWidget(lyrics_note)
        self._insp_lyrics_state = QLabel()
        self._insp_lyrics_state.setWordWrap(True)
        self._insp_lyrics_state.setObjectName("tagEditorDialogNote")
        lyrics_layout.addWidget(self._insp_lyrics_state)
        self._insp_lyrics = QTextEdit()
        self._insp_lyrics.setAcceptRichText(False)
        self._insp_lyrics.setFixedHeight(190)
        self._insp_lyrics.setObjectName("tagEditorLyrics")
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
        lyrics_buttons = ResponsiveButtonFlow(fill=True)
        self._insp_lyrics_set_btn = ElidedPushButton(t("meta_lyrics_propose_replace"))
        self._insp_lyrics_set_btn.setProperty("accentRole", "primary")
        self._insp_lyrics_set_btn.clicked.connect(self._on_lyrics_propose_set)
        self._insp_lyrics_clear_btn = ElidedPushButton(t("meta_lyrics_propose_clear"))
        self._insp_lyrics_clear_btn.setProperty("dangerRole", True)
        self._insp_lyrics_clear_btn.clicked.connect(self._on_lyrics_propose_clear)
        self._insp_lyrics_revert_btn = ElidedPushButton(t("meta_lyrics_revert_pending"))
        self._insp_lyrics_revert_btn.clicked.connect(self._on_lyrics_revert)
        for button in (self._insp_lyrics_set_btn, self._insp_lyrics_clear_btn, self._insp_lyrics_revert_btn):
            lyrics_buttons.add_button(button)
            self._selection_scope_buttons.append(button)
        lyrics_layout.addWidget(lyrics_buttons)
        return lyrics_grp

    def _build_artwork_group(self) -> QGroupBox:
        """Current and proposed cover art. Add and Replace stay distinct: one
        appends a picture, the other overwrites, and merging them would make
        the destructive case the default."""
        artwork_grp = QGroupBox("")
        artwork_layout = QVBoxLayout(artwork_grp)
        artwork_layout.setContentsMargins(0, 0, 0, 0)
        artwork_layout.setSpacing(9)
        artwork_note = QLabel(t("meta_artwork_proposal_note"))
        artwork_note.setObjectName("tagEditorInspectorNote")
        artwork_note.setWordWrap(True)
        artwork_layout.addWidget(artwork_note)

        # Two 132px covers side by side need 273px before anything else on the
        # page gets a say, which is more than the pane's own floor. In a flow
        # they sit abreast while there is room and stack once there is not.
        cover_grid = ResponsiveButtonFlow(fill=True)

        def _cover_column(caption: str, object_name: str) -> tuple[QWidget, QLabel, ArtworkDropPreview]:
            column = QWidget()
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(5)
            label = ElidedLabel(caption)
            label.setObjectName("tagEditorSectionTitle")
            preview = ArtworkDropPreview(self._on_artwork_drop)
            preview.setFixedSize(132, 132)
            preview.setObjectName(object_name)
            preview.setAlignment(Qt.AlignCenter)
            column_layout.addWidget(label)
            column_layout.addWidget(preview, alignment=Qt.AlignHCenter)
            return column, label, preview

        current_col, self._insp_artwork_current_label, self._insp_artwork_preview = _cover_column(
            t("meta_artwork_current"), "tagEditorCoverCurrent")
        proposed_col, self._insp_artwork_proposed_label, self._insp_artwork_proposed_preview = _cover_column(
            t("meta_artwork_proposed"), "tagEditorCoverProposed")
        cover_grid.add_widget(current_col)
        cover_grid.add_widget(proposed_col)
        artwork_layout.addWidget(cover_grid)
        self._insp_artwork_state = QLabel()
        self._insp_artwork_state.setWordWrap(True)
        self._insp_artwork_state.setObjectName("tagEditorDialogNote")
        artwork_layout.addWidget(self._insp_artwork_state)
        artwork_buttons = ResponsiveButtonFlow(fill=True)
        self._insp_artwork_add_btn = ElidedPushButton(t("meta_artwork_choose_image"))
        self._insp_artwork_replace_btn = ElidedPushButton(t("meta_artwork_replace"))
        self._insp_artwork_remove_btn = ElidedPushButton(t("meta_artwork_remove"))
        self._insp_artwork_paste_btn = ElidedPushButton(t("meta_artwork_paste"))
        self._insp_artwork_export_btn = ElidedPushButton(t("meta_artwork_export"))
        self._insp_artwork_revert_btn = ElidedPushButton(t("meta_artwork_revert"))
        self._insp_artwork_add_btn.clicked.connect(self._on_artwork_choose_adaptive)
        self._insp_artwork_replace_btn.clicked.connect(self._on_artwork_replace_choose)
        self._insp_artwork_remove_btn.clicked.connect(self._on_artwork_remove)
        self._insp_artwork_paste_btn.clicked.connect(self._on_artwork_paste)
        self._insp_artwork_export_btn.clicked.connect(self._on_artwork_export)
        self._insp_artwork_revert_btn.clicked.connect(self._on_artwork_revert)
        # Add and Replace stay separate rows rather than one adaptive button:
        # guessing which one the user meant is fine as a default, but it must
        # not be the only way to say "append this as a second picture".
        # Revert is here for the same reason Lyrics has one -- a pending
        # artwork proposal was otherwise only undoable by reverting the whole
        # file from the footer.
        self._insp_artwork_remove_btn.setProperty("dangerRole", True)
        for button in (
            self._insp_artwork_add_btn, self._insp_artwork_paste_btn,
            self._insp_artwork_export_btn, self._insp_artwork_replace_btn,
            self._insp_artwork_revert_btn, self._insp_artwork_remove_btn,
        ):
            artwork_buttons.add_button(button)
            self._selection_scope_buttons.append(button)
        artwork_layout.addWidget(artwork_buttons)
        artwork_support = QLabel(t("meta_artwork_support_note"))
        artwork_support.setObjectName("tagEditorDialogNote")
        artwork_support.setWordWrap(True)
        artwork_layout.addWidget(artwork_support)
        return artwork_grp

    def _build_replaygain_group(self) -> QGroupBox:
        """ReplayGain values and analysis.

        Analysis never touches the audio -- it only produces pending tags.
        Track and album are cleared separately because an album value is
        shared across files and a track value is not."""
        replay_grp = QGroupBox("")
        replay_layout = QVBoxLayout(replay_grp)
        replay_layout.setContentsMargins(0, 0, 0, 0)
        replay_layout.setSpacing(8)
        replay_note = QLabel(t("meta_replaygain_plain_explanation"))
        replay_note.setWordWrap(True)
        replay_note.setObjectName("tagEditorInspectorNote")
        replay_layout.addWidget(replay_note)
        self._insp_replay_values: dict[str, QLabel] = {}
        for field_name, key in (
            (REPLAYGAIN_TRACK_GAIN, "meta_replaygain_track_gain"),
            (REPLAYGAIN_TRACK_PEAK, "meta_replaygain_track_peak"),
            (REPLAYGAIN_ALBUM_GAIN, "meta_replaygain_album_gain"),
            (REPLAYGAIN_ALBUM_PEAK, "meta_replaygain_album_peak"),
            (REPLAYGAIN_REFERENCE_LOUDNESS, "meta_replaygain_reference_loudness"),
        ):
            row_frame = QWidget()
            row_frame.setObjectName("tagEditorFieldRow")
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(7)
            field_label = ElidedLabel(t(key))
            field_label.setFixedWidth(120)
            row.addWidget(field_label)
            value_label = ElidedLabel(t("meta_inspector_empty_value"))
            value_label.setObjectName("tagEditorReadOnlyValue")
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(value_label, stretch=1)
            replay_layout.addWidget(row_frame)
            self._insp_replay_values[field_name] = value_label
        replay_actions = ResponsiveButtonFlow(fill=True)
        self._insp_rg_track_btn = ElidedPushButton(t("meta_replaygain_analyze_track"))
        self._insp_rg_album_btn = ElidedPushButton(t("meta_replaygain_analyze_album"))
        self._insp_rg_cancel_btn = ElidedPushButton(t("meta_replaygain_cancel"))
        self._insp_rg_cancel_btn.setVisible(False)
        self._insp_rg_track_btn.clicked.connect(self._on_replaygain_track)
        self._insp_rg_album_btn.clicked.connect(self._on_replaygain_album)
        self._insp_rg_cancel_btn.clicked.connect(self.replaygain_cancel_requested)
        self._insp_rg_track_btn.setProperty("accentRole", "primary")
        self._insp_rg_clear_track_btn = ElidedPushButton(t("meta_replaygain_clear_track"))
        self._insp_rg_clear_album_btn = ElidedPushButton(t("meta_replaygain_clear_album"))
        self._insp_rg_revert_btn = ElidedPushButton(t("meta_replaygain_revert"))
        self._insp_rg_clear_all_btn = ElidedPushButton(t("meta_replaygain_propose_clear"))
        self._insp_rg_clear_all_btn.setProperty("dangerRole", True)
        self._insp_rg_clear_all_btn.clicked.connect(self._on_replaygain_clear_all)
        self._insp_rg_clear_track_btn.clicked.connect(self._on_replaygain_clear_track)
        self._insp_rg_clear_album_btn.clicked.connect(self._on_replaygain_clear_album)
        self._insp_rg_revert_btn.clicked.connect(self._on_replaygain_revert)
        # The separate track/album clears are the whole point of the docstring
        # above: an album value is shared across files and a track value is
        # not, so collapsing them into one "clear everything" button takes the
        # safe choice away rather than simplifying it.
        for button in (
            self._insp_rg_track_btn, self._insp_rg_album_btn,
            self._insp_rg_clear_track_btn, self._insp_rg_clear_album_btn,
            self._insp_rg_clear_all_btn, self._insp_rg_revert_btn,
            self._insp_rg_cancel_btn,
        ):
            replay_actions.add_button(button)
            self._selection_scope_buttons.append(button)
        replay_layout.addWidget(replay_actions)
        self._insp_rg_progress = QProgressBar()
        self._insp_rg_progress.setVisible(False)
        replay_layout.addWidget(self._insp_rg_progress)
        replay_support = QLabel(t("meta_replaygain_album_note"))
        replay_support.setObjectName("tagEditorDialogNote")
        replay_support.setWordWrap(True)
        replay_layout.addWidget(replay_support)
        return replay_grp

    def _build_properties_group(self) -> QGroupBox:
        """Read-only file facts, plus the entry point for reviewing a file that
        changed on disk."""
        props_grp = QGroupBox("")
        props_layout = QVBoxLayout(props_grp)
        props_layout.setContentsMargins(0, 0, 0, 0)
        props_layout.setSpacing(8)
        self._insp_property_table = QFrame()
        self._insp_property_table.setObjectName("tagEditorPropertyTable")
        property_layout = QVBoxLayout(self._insp_property_table)
        property_layout.setContentsMargins(0, 0, 0, 0)
        property_layout.setSpacing(0)
        self._insp_property_values: dict[str, QLabel] = {}
        for key, label_key in (
            ("path", "meta_property_path"), ("format_id", "meta_property_format"),
            ("size_bytes", "meta_property_size"), ("duration_seconds", "meta_property_duration"),
            ("bitrate", "meta_property_bitrate"), ("sample_rate", "meta_property_sample_rate"),
            ("channels", "meta_property_channels"), ("modified_time", "meta_property_modified"),
            ("capability", "meta_property_capability"),
        ):
            row = QFrame()
            row.setObjectName("tagEditorPropertyRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(9, 7, 9, 7)
            row_layout.setSpacing(8)
            label = ElidedLabel(t(label_key))
            label.setObjectName("tagEditorPropertyName")
            label.setFixedWidth(100)
            # The path row holds a full absolute path: as a plain QLabel its
            # minimum width was the path itself, which dragged the whole page
            # past the pane and off the right-hand edge.
            value = ElidedLabel("", mode=Qt.TextElideMode.ElideMiddle)
            value.setObjectName("tagEditorPropertyValue")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setLayoutDirection(Qt.LeftToRight)
            row_layout.addWidget(label)
            row_layout.addWidget(value, 1)
            property_layout.addWidget(row)
            self._insp_property_values[key] = value
        props_layout.addWidget(self._insp_property_table)
        self._insp_properties = QLabel()
        self._insp_properties.setWordWrap(True)
        self._insp_properties.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._insp_properties.setVisible(False)
        props_layout.addWidget(self._insp_properties)
        prop_actions = ResponsiveButtonFlow(fill=True)
        self._insp_property_open_btn = ElidedPushButton(t("meta_property_open"))
        self._insp_property_reveal_btn = ElidedPushButton(t("meta_property_reveal"))
        self._insp_property_copy_btn = ElidedPushButton(t("meta_property_copy_path"))
        self._insp_property_open_btn.clicked.connect(self._on_property_open)
        self._insp_property_reveal_btn.clicked.connect(self._on_property_reveal)
        self._insp_property_copy_btn.clicked.connect(self._on_property_copy_path)
        for button in (self._insp_property_open_btn, self._insp_property_reveal_btn,
                       self._insp_property_copy_btn):
            prop_actions.add_button(button)
            # All three act on exactly one selected file.  Without this they
            # stayed enabled after a deselect and their handlers just returned,
            # so the click did nothing and said nothing.
            self._selection_scope_buttons.append(button)
        props_layout.addWidget(prop_actions)
        self._insp_external_status = QLabel()
        self._insp_external_status.setWordWrap(True)
        props_layout.addWidget(self._insp_external_status)
        self._insp_external_review_btn = ElidedPushButton(t("meta_external_review_action"))
        a11y.describe(self._insp_external_review_btn, t("meta_external_review_action"),
                      description=t("meta_external_review_title"))
        self._insp_external_review_btn.clicked.connect(
            self._review_selected_external_conflict)
        self._insp_external_review_btn.setVisible(False)
        props_layout.addWidget(self._insp_external_review_btn)
        return props_grp

    def _on_property_open(self) -> None:
        tracks = self._get_selected_tracks()
        if len(tracks) == 1:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(tracks[0].path)))

    def _on_property_reveal(self) -> None:
        tracks = self._get_selected_tracks()
        if len(tracks) == 1:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(tracks[0].path.parent)))

    def _on_property_copy_path(self) -> None:
        tracks = self._get_selected_tracks()
        if len(tracks) == 1:
            QApplication.clipboard().setText(str(tracks[0].path))

    def _build_edit_artwork_page(self) -> QScrollArea:
        """Edit > Artwork."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)
        layout.addWidget(self._build_artwork_group())
        layout.addStretch()
        return self._inspector_scroll(w)

    def _build_edit_lyrics_page(self) -> QScrollArea:
        """Edit > Lyrics."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)
        layout.addWidget(self._build_lyrics_group())
        layout.addStretch()
        return self._inspector_scroll(w)

    def _build_edit_gain_page(self) -> QScrollArea:
        """Edit > ReplayGain."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)
        layout.addWidget(self._build_replaygain_group())
        layout.addStretch()
        return self._inspector_scroll(w)

    def _build_edit_properties_page(self) -> QScrollArea:
        """Edit > File properties."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)
        layout.addWidget(self._build_properties_group())
        layout.addStretch()
        return self._inspector_scroll(w)
    def _build_tools_auto_page(self) -> QScrollArea:
        """Tools > Auto arrange.

        Auto arrange runs only the operations explicitly enabled in its
        settings, so the page lists them: the button is otherwise a black box
        that edits files by rules the user cannot see from here.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)

        note = QLabel(t("meta_auto_header"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {tag_editor_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        layout.addWidget(self._make_auto_toolbar_action())

        album_note = QLabel(t("meta_auto_album_note"))
        album_note.setWordWrap(True)
        album_note.setObjectName("tagEditorDialogNote")
        layout.addWidget(album_note)

        heading = QLabel(t("meta_auto_enabled_heading"))
        heading.setWordWrap(True)
        heading.setStyleSheet(
            f"color: {tag_editor_colors().text_tertiary}; font-size: 11px; font-weight: bold;")
        layout.addWidget(heading)

        self._auto_enabled_list = QLabel("")
        self._auto_enabled_list.setWordWrap(True)
        self._auto_enabled_list.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._auto_enabled_list)
        self._refresh_auto_enabled_list()

        layout.addStretch()
        return self._inspector_scroll(w)

    def _refresh_auto_enabled_list(self) -> None:
        """Mirror the auto-arrange settings into the page that runs them.

        The album-from-folder step is listed first and marked as always on.
        It runs on every Auto-Order regardless of the settings, so leaving it
        out made the list of "operations this runs" quietly incomplete.
        """
        if not hasattr(self, "_auto_enabled_list"):
            return
        labels = [t("meta_auto_always_album")]
        labels += [
            t(label_key)
            for op_id, label_key, _desc in MAGIC_OP_DEFS
            if op_id in self._auto_ops
        ]
        self._auto_enabled_list.setText("\n".join(f"•  {label}" for label in labels))

    def _build_check_pending_page(self) -> QScrollArea:
        """Check > Pending changes.

        A summary and a way into the full review. Apply covers every included,
        unblocked change regardless of what is selected or filtered, so the
        page says so rather than letting the table imply otherwise.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)

        note = QLabel(t("meta_pending_scope_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {tag_editor_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        self._pending_summary = QLabel("")
        self._pending_summary.setWordWrap(True)
        layout.addWidget(self._pending_summary)

        self._pending_items = QWidget()
        self._pending_items_layout = QVBoxLayout(self._pending_items)
        self._pending_items_layout.setContentsMargins(0, 0, 0, 0)
        self._pending_items_layout.setSpacing(6)
        layout.addWidget(self._pending_items)

        self._pending_review_btn = ElidedPushButton(t("meta_review_changes").strip())
        self._pending_review_btn.setStyleSheet(btn_style())
        self._pending_review_btn.clicked.connect(self._on_review_changes)
        layout.addWidget(self._pending_review_btn)

        layout.addStretch()
        return self._inspector_scroll(w)

    def _build_check_external_page(self) -> QScrollArea:
        """Check > External changes and blockers.

        A file that changed on disk since the scan is never written silently:
        it stays pending and out of the batch until the conflict is resolved.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignTop)

        note = QLabel(t("meta_external_blockers_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {tag_editor_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        self._external_summary = QLabel("")
        self._external_summary.setWordWrap(True)
        layout.addWidget(self._external_summary)

        self._external_items = QWidget()
        self._external_items_layout = QVBoxLayout(self._external_items)
        self._external_items_layout.setContentsMargins(0, 0, 0, 0)
        self._external_items_layout.setSpacing(7)
        layout.addWidget(self._external_items)

        self._external_review_all_btn = ElidedPushButton(t("meta_external_review_action"))
        self._external_review_all_btn.setStyleSheet(btn_style())
        self._external_review_all_btn.clicked.connect(
            self._review_selected_external_conflict)
        layout.addWidget(self._external_review_all_btn)

        layout.addStretch()
        return self._inspector_scroll(w)

    @staticmethod
    def _clear_dynamic_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _exclude_pending_item(self, item_id: int) -> None:
        self._workspace.set_apply_excluded_ids([item_id], True)
        self._model.refresh_all()
        self._refresh_checked_scope_state()

    def _open_external_item(self, item) -> None:
        identity = self._workspace.item_id(item)
        record_like = type("ExternalSelection", (), {"item_id": identity})()
        self._navigate_review_record(record_like)
        self._review_selected_external_conflict()

    def _render_pending_items(self, records) -> None:
        if not hasattr(self, "_pending_items_layout"):
            return
        self._clear_dynamic_layout(self._pending_items_layout)
        for record in records[:20]:
            item = self._workspace.track_for_id(record.item_id)
            row = QFrame()
            row.setObjectName("tagEditorPendingItem")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 7, 8, 7)
            row_layout.setSpacing(8)
            copy = QVBoxLayout()
            copy.setSpacing(2)
            # Elided, not plain: a plain QLabel reports its whole text as its
            # minimum width, so one long file name was enough to push this
            # page past the pane and hide the rest of the row.
            filename = ElidedLabel(item.path.name if item is not None else str(record.item_id),
                                   mode=Qt.TextElideMode.ElideMiddle)
            filename.setObjectName("tagEditorPendingFile")
            filename.setLayoutDirection(Qt.LeftToRight)
            filename.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            change = QLabel(
                f"{self._review_value(record.original_value)}  →  "
                f"{self._review_value(record.proposed_value)}")
            change.setObjectName("tagEditorPendingChange")
            change.setWordWrap(True)
            copy.addWidget(filename)
            copy.addWidget(change)
            row_layout.addLayout(copy, 1)
            exclude = QToolButton()
            exclude.setText("×")
            exclude.setFixedSize(30, 30)
            exclude.setToolTip(t("meta_pending_exclude"))
            exclude.clicked.connect(
                lambda _checked=False, identity=record.item_id: self._exclude_pending_item(identity))
            row_layout.addWidget(exclude)
            self._pending_items_layout.addWidget(row)

    def _render_external_items(self, stale) -> None:
        if not hasattr(self, "_external_items_layout"):
            return
        self._clear_dynamic_layout(self._external_items_layout)
        for track in stale[:20]:
            row = QFrame()
            row.setObjectName("tagEditorProblemItem")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 7, 8, 7)
            row_layout.setSpacing(8)
            severity = ElidedLabel(t("meta_external_blocker_badge"))
            severity.setObjectName("tagEditorSeverityError")
            row_layout.addWidget(severity)
            copy = QVBoxLayout()
            copy.setSpacing(2)
            name = ElidedLabel(track.path.name, mode=Qt.TextElideMode.ElideMiddle)
            name.setStyleSheet("font-weight: 800; font-size: 11px;")
            state = ElidedLabel(t(f"meta_external_state_{getattr(track, 'external_state', 'current')}"))
            state.setObjectName("tagEditorProblemCopy")
            copy.addWidget(name)
            copy.addWidget(state)
            row_layout.addLayout(copy, 1)
            review = ElidedPushButton(t("meta_external_review_short"))
            review.clicked.connect(
                lambda _checked=False, target=track: self._open_external_item(target))
            row_layout.addWidget(review)
            self._external_items_layout.addWidget(row)

    def _refresh_check_pages(self) -> None:
        """Keep the Check tabs honest about the current workspace."""
        if hasattr(self, "_pending_summary"):
            changed = self._workspace.changed_tracks()
            candidates = self._workspace.apply_candidates()
            records = self._workspace.change_set.records()
            self._pending_summary.setText(
                t("meta_pending_summary", files=len(changed), applying=len(candidates))
                if changed else t("meta_pending_none")
            )
            self._render_pending_items(records)
            self._pending_review_btn.setEnabled(bool(changed))
        if hasattr(self, "_external_summary"):
            blockers = self._workspace.apply_blockers()
            stale = [item for item in self._workspace.tracks
                     if is_external_change(getattr(item, "external_state", "current"))]
            self._external_summary.setText(
                t("meta_external_summary", stale=len(stale), blocked=len(blockers))
                if stale else t("meta_external_none")
            )
            self._render_external_items(stale)
            self._external_review_all_btn.setEnabled(bool(stale))

    def _make_op_side_button(self, glyph: str, tooltip: str, accent: bool = False) -> QPushButton:
        """Small icon button shown on the trailing edge of an op row.

        ``accent=True`` gives the button a tinted, always-visible look so
        configurable actions (the settings gear) are easy to spot next to
        the subtler info icon.
        """
        c = tag_editor_colors()
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
        compact_qss = compact_clear_button_qss()
        for row in (*self._op_rows, *getattr(self, "_action_catalog_rows", ())):
            row.setStyleSheet(compact_qss if row.property("cleanupCompact") else qss)

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
            "rename_from_title": lambda tracks: self.rename_from_title.emit(tracks),
        }
        # Parameterised operations cannot run from a single click, so they hand
        # off to the action engine with that action already selected. Their
        # labels end with an ellipsis to say so before the click.
        parameterised_ops: dict[str, str] = {
            "replace_text":  "tag.replace_text.v1",
            "change_case":   "tag.change_case.v1",
            "number_tracks": "tag.number_tracks.v1",
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
            # The row is focusable, so it is announced on its own; the "what
            # does this do" paragraph behind the info button is its description
            # rather than something only a mouse user can reach.
            a11y.describe(row, label, description=desc)
            if key in parameterised_ops:
                row.clicked.connect(
                    lambda action_id=parameterised_ops[key]: self._on_action_engine(action_id))
            elif key in op_handlers:
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

        def add_compact_clear_button(key: str, flow: ResponsiveButtonFlow) -> None:
            """Add one small, consistently-red field-clear action button."""
            label_key, desc_key = op_defs_by_key[key]
            label = t(label_key)
            desc = t(desc_key)
            button = ElidedPushButton(label)
            button.setProperty("cleanupCompact", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            a11y.describe(button, label, description=desc, tooltip=desc)
            button.clicked.connect(lambda _=False, handler=op_handlers[key]: run_for_checked(handler))
            # Compact controls remain in the common registry so scope refresh
            # and accessibility checks cover every available operation.
            self._op_rows.append(button)
            flow.add_button(button)

        if sections is not None:
            for i, (subheader_key, keys) in enumerate(sections):
                # The title plus rule makes the two cleanup groups read as
                # categories instead of one uninterrupted list.
                header = QWidget()
                header_layout = QHBoxLayout(header)
                header_layout.setContentsMargins(0, 5 if i else 0, 0, 0)
                header_layout.setSpacing(7)
                title = QLabel(t(subheader_key))
                title.setStyleSheet(
                    f"color: {tag_editor_colors().text_secondary}; font-size: 11px; font-weight: 800;")
                rule = QFrame()
                rule.setFrameShape(QFrame.Shape.HLine)
                rule.setFixedHeight(1)
                rule.setStyleSheet(f"background: {tag_editor_colors().border}; border: none;")
                header_layout.addWidget(title)
                header_layout.addWidget(rule, 1)
                grp_layout.addWidget(header)
                if subheader_key == "meta_section_clear_fields":
                    compact_flow = ResponsiveButtonFlow()
                    for key in keys:
                        add_compact_clear_button(key, compact_flow)
                    grp_layout.addWidget(compact_flow)
                else:
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
        prompts.show_info(self, title, desc)
