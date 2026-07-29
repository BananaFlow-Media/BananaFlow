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
from ui.theme_manager import get_colors
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
    dim_hex,
    op_row_qss,
    primary_btn_style,
)
from .widgets import OpRow


class InspectorBuildMixin:
    """Inspector page construction: the empty/folder/tracks pages, the"""

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
        """The Edit > Fields page.

        Artwork, lyrics, ReplayGain and properties used to be stacked below
        this in one long scroll; they are now sibling tabs, so this page is
        the fields alone.
        """
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
        return fields_grp

    def _build_lyrics_group(self) -> QGroupBox:
        """Embedded lyrics with their language and descriptor.

        An empty editor never clears lyrics on its own -- removing them is an
        explicit action, so a stray keystroke cannot silently discard a file's
        words."""
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
        return lyrics_grp

    def _build_artwork_group(self) -> QGroupBox:
        """Current and proposed cover art. Add and Replace stay distinct: one
        appends a picture, the other overwrites, and merging them would make
        the destructive case the default."""
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
        return artwork_grp

    def _build_replaygain_group(self) -> QGroupBox:
        """ReplayGain values and analysis.

        Analysis never touches the audio -- it only produces pending tags.
        Track and album are cleared separately because an album value is
        shared across files and a track value is not."""
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
        return replay_grp

    def _build_properties_group(self) -> QGroupBox:
        """Read-only file facts, plus the entry point for reviewing a file that
        changed on disk."""
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
        return props_grp

    def _build_rename_group(self) -> QGroupBox:
        """Physical rename derived from the title. Still only a proposal: nothing
        reaches the filesystem before Apply."""
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
        return rename_grp

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
    def _build_apply_value_group(self) -> QGroupBox:
        """Set one artist or album across the whole selection at once.

        The controller has supported this from the start and it is covered by
        tests, but the two handlers read line edits that were never built and
        nothing ever called them -- the feature had no reachable UI at all.
        This is that missing entry point, not a new capability.
        """
        group = QGroupBox(t("meta_apply_value_group"))
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        note = QLabel(t("meta_apply_value_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        for field_key, label_key, button_key, handler in (
            ("_insp_folder_artist", "meta_field_artist",
             "meta_apply_artist_to_selection", self._on_insp_folder_artist),
            ("_insp_folder_album", "meta_field_album",
             "meta_apply_album_to_selection", self._on_insp_folder_album),
        ):
            row = QHBoxLayout()
            row.setSpacing(5)
            edit = QLineEdit()
            edit.setPlaceholderText(t(label_key))
            a11y.describe(edit, t(button_key))
            edit.returnPressed.connect(handler)
            setattr(self, field_key, edit)
            row.addWidget(edit, stretch=1)

            button = QPushButton(t(button_key))
            button.setStyleSheet(btn_style())
            a11y.describe(button, t(button_key))
            button.clicked.connect(handler)
            self._selection_scope_buttons.append(button)
            row.addWidget(button)
            layout.addLayout(row)

        return group

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
        note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        layout.addWidget(self._make_auto_toolbar_action(), alignment=Qt.AlignHCenter)

        album_note = QLabel(t("meta_auto_album_note"))
        album_note.setWordWrap(True)
        album_note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(album_note)

        heading = QLabel(t("meta_auto_enabled_heading"))
        heading.setStyleSheet(
            f"color: {get_colors().text_tertiary}; font-size: 11px; font-weight: bold;")
        layout.addWidget(heading)

        self._auto_enabled_list = QLabel("")
        self._auto_enabled_list.setWordWrap(True)
        self._auto_enabled_list.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._auto_enabled_list)
        self._refresh_auto_enabled_list()

        layout.addStretch()
        return self._inspector_scroll(w)

    def _refresh_auto_enabled_list(self) -> None:
        """Mirror the auto-arrange settings into the page that runs them."""
        if not hasattr(self, "_auto_enabled_list"):
            return
        labels = [
            t(label_key)
            for op_id, label_key, _desc in MAGIC_OP_DEFS
            if op_id in self._auto_ops
        ]
        self._auto_enabled_list.setText(
            "\n".join(f"•  {label}" for label in labels)
            if labels else t("meta_auto_none_enabled")
        )

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
        note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        self._pending_summary = QLabel("")
        self._pending_summary.setWordWrap(True)
        layout.addWidget(self._pending_summary)

        self._pending_review_btn = QPushButton(t("meta_review_changes").strip())
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
        note.setStyleSheet(f"color: {get_colors().text_secondary}; font-size: 11px;")
        layout.addWidget(note)

        self._external_summary = QLabel("")
        self._external_summary.setWordWrap(True)
        layout.addWidget(self._external_summary)

        self._external_review_all_btn = QPushButton(t("meta_external_review_action"))
        self._external_review_all_btn.setStyleSheet(btn_style())
        self._external_review_all_btn.clicked.connect(
            self._review_selected_external_conflict)
        layout.addWidget(self._external_review_all_btn)

        layout.addStretch()
        return self._inspector_scroll(w)

    def _refresh_check_pages(self) -> None:
        """Keep the Check tabs honest about the current workspace."""
        if hasattr(self, "_pending_summary"):
            changed = self._workspace.changed_tracks()
            candidates = self._workspace.apply_candidates()
            self._pending_summary.setText(
                t("meta_pending_summary", files=len(changed), applying=len(candidates))
                if changed else t("meta_pending_none")
            )
            self._pending_review_btn.setEnabled(bool(changed))
        if hasattr(self, "_external_summary"):
            blockers = self._workspace.apply_blockers()
            stale = [item for item in self._workspace.tracks
                     if is_external_change(getattr(item, "external_state", "current"))]
            self._external_summary.setText(
                t("meta_external_summary", stale=len(stale), blocked=len(blockers))
                if stale else t("meta_external_none")
            )
            self._external_review_all_btn.setEnabled(bool(stale))

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
        prompts.show_info(self, title, desc)
