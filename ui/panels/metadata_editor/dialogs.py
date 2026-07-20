"""
ui/panels/metadata_editor/dialogs.py  –  Tag Editor dialogs
=============================================================
MoreColumnsDialog        — Explorer 'Choose details…' column picker
AutoArrangeSettingsDialog — which magic ops the auto-arrange button runs
CleanSettingsDialog      — aggressiveness of the cleaning operations
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon

from ui.dialogs.styled_dialog import (
    CheckMarkBox,
    StyledDialog,
    add_header,
    make_button,
    make_footer,
    make_root_layout,
    make_section,
    make_setting_row,
    section_layout,
    set_button_role,
    show_info,
)
from ui.i18n import t
from ui.models.metadata_table_model import (
    COLUMN_COUNT,
    COL_CHECK,
    COL_FILENAME,
    _HEADER_KEYS,
)

from .shared import MAGIC_OP_DEFS


class MoreColumnsDialog(StyledDialog):
    """Scrollable, searchable list of all table columns — mirrors Windows
    Explorer's 'Choose details…' dialog that appears from 'More…' in the
    column header context menu."""

    def __init__(self, table_view: QTableView, parent=None) -> None:
        super().__init__(parent, minimum_size=(400, 460), resize_to=(440, 540))
        self.setWindowTitle(t("mt_more_columns_title"))
        self._table = table_view

        layout = make_root_layout(self)
        add_header(
            layout,
            t("mt_more_columns_title"),
            t("mt_search_columns"),
            icon=FluentIcon.LAYOUT.icon(),
        )
        section = make_section()
        section_lay = section_layout(section)

        # Search bar
        self._search = QLineEdit()
        self._search.setPlaceholderText(t("mt_search_columns"))
        self._search.textChanged.connect(self._on_search)
        section_lay.addWidget(self._search)

        # Scrollable column list
        scroll = QScrollArea()
        scroll.setObjectName("dialogNoBorderScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        self._list_layout = QVBoxLayout(content)
        self._list_layout.setSpacing(6)
        self._list_layout.setContentsMargins(0, 8, 0, 0)

        # Always-visible columns that can't be hidden
        ALWAYS_VISIBLE = {COL_FILENAME}
        # Columns never offered in any menu
        NO_MENU = {COL_CHECK}

        self._rows: list[tuple[int, str, QCheckBox]] = []
        for col in range(COLUMN_COUNT):
            if col in NO_MENU:
                continue
            key = _HEADER_KEYS[col] if col < len(_HEADER_KEYS) else ""
            label = t(key) if key else ""
            if not label:
                continue
            cb = CheckMarkBox(label)
            cb.setChecked(not table_view.isColumnHidden(col))
            if col in ALWAYS_VISIBLE:
                cb.setEnabled(False)
            self._rows.append((col, label, cb))
            self._list_layout.addWidget(make_setting_row(cb))

        self._list_layout.addStretch()
        scroll.setWidget(content)
        section_lay.addWidget(scroll, stretch=1)
        layout.addWidget(section, stretch=1)

        # OK / Cancel
        cancel_btn = make_button(t("meta_cancel"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = make_button(t("meta_ok"), "primary")
        ok_btn.clicked.connect(self._accept)
        layout.addWidget(make_footer(cancel_btn, ok_btn))

    def _on_search(self, text: str) -> None:
        needle = text.casefold()
        for _, label, cb in self._rows:
            cb.setVisible(not needle or needle in label.casefold())

    def _accept(self) -> None:
        for col, _, cb in self._rows:
            self._table.setColumnHidden(col, not cb.isChecked())
        self.accept()


class AutoArrangeSettingsDialog(StyledDialog):
    """Choose which magic operations the 🪄 auto-arrange button runs."""

    def __init__(self, enabled: set[str], parent=None) -> None:
        super().__init__(parent, minimum_size=(420, 500), resize_to=(480, 580))
        self.setWindowTitle(t("meta_auto_settings_title"))

        self._result = set(enabled)
        layout = make_root_layout(self)
        add_header(
            layout,
            t("meta_auto_settings_title"),
            t("meta_auto_header"),
        )

        note = QLabel(t("meta_auto_album_note"))
        note.setObjectName("dialogHint")
        note.setWordWrap(True)
        layout.addWidget(note)

        scroll = QScrollArea()
        scroll.setObjectName("dialogNoBorderScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(6)

        # Same four buckets as the inspector rail, so the settings dialog
        # reads as "which of these already-organised actions run automatically"
        # rather than a flat, unordered wall of checkboxes.
        op_defs_by_key = {key: (label_key, desc_key) for key, label_key, desc_key in MAGIC_OP_DEFS}
        op_sections = (
            ("meta_group_from_filename", ("title_strip", "title_full", "track_num", "split_at")),
            ("meta_section_text_cleanup", ("normalize_spaces", "strip_junk", "album_artist")),
            ("meta_section_clear_fields", (
                "clear_title", "clear_artist", "clear_album", "clear_album_artist",
                "clear_track_num", "clear_year", "clear_genre", "clear_comments",
            )),
            ("meta_rename_group", ("clean_filename", "strip_filename_numbering")),
        )

        self._cbs: dict[str, QCheckBox] = {}
        for i, (subheader_key, keys) in enumerate(op_sections):
            header = QLabel(t(subheader_key))
            header.setObjectName("dialogSectionTitle")
            if i:
                header.setStyleSheet("margin-top: 6px;")
            scroll_layout.addWidget(header)
            for key in keys:
                label_key, desc_key = op_defs_by_key[key]
                label = t(label_key)
                desc  = t(desc_key)
                row = QHBoxLayout()
                row.setSpacing(6)
                cb = CheckMarkBox(label)
                cb.setChecked(key in enabled)
                self._cbs[key] = cb
                row.addWidget(cb)

                info_btn = QPushButton("")
                info_btn.setObjectName("dialogInfoBtn")
                info_btn.setIcon(FluentIcon.INFO.icon())
                info_btn.setFixedSize(30, 30)
                info_btn.setToolTip(desc)
                set_button_role(info_btn, "icon")
                info_btn.clicked.connect(lambda _, l=label, d=desc: self._show_info(l, d))
                row.addWidget(info_btn)
                row.addStretch()
                scroll_layout.addLayout(row)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, stretch=1)

        cancel_btn = make_button(t("meta_cancel"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = make_button(t("meta_ok"), "primary")
        ok_btn.clicked.connect(self._accept)
        layout.addWidget(make_footer(cancel_btn, ok_btn))

    def _accept(self) -> None:
        self._result = {k for k, cb in self._cbs.items() if cb.isChecked()}
        self.accept()

    def _show_info(self, title: str, desc: str) -> None:
        show_info(self, title, desc)

    @property
    def result_ops(self) -> set[str]:
        return self._result


class CleanSettingsDialog(StyledDialog):
    """Choose how aggressive the cleaning features should be."""

    def __init__(self, cfg, parent=None) -> None:
        super().__init__(parent, minimum_size=(430, 440), resize_to=(480, 520))
        self.setWindowTitle(t("meta_clean_settings_title"))
        self._cfg = cfg

        layout = make_root_layout(self)
        add_header(
            layout,
            t("meta_clean_settings_title"),
            "",
            icon=FluentIcon.ERASE_TOOL.icon(),
        )

        # Title clean settings
        title_grp = make_section(t("meta_clean_title_group"))
        title_lay = section_layout(title_grp)

        self.cb_title_brackets = CheckMarkBox(t("meta_clean_brackets"))
        self.cb_title_brackets.setChecked(getattr(self._cfg, "tag_clean_title_remove_brackets", True))

        self.cb_title_english = CheckMarkBox(t("meta_clean_english_junk"))
        self.cb_title_english.setChecked(getattr(self._cfg, "tag_clean_title_remove_web_junk", True))

        self.cb_title_hebrew = CheckMarkBox(t("meta_clean_hebrew_junk"))
        self.cb_title_hebrew.setChecked(getattr(self._cfg, "tag_clean_title_remove_hebrew", True))

        self.cb_title_punc = CheckMarkBox(t("meta_clean_punctuation"))
        self.cb_title_punc.setChecked(getattr(self._cfg, "tag_clean_title_fix_punctuation", True))

        title_lay.addWidget(make_setting_row(self.cb_title_brackets))
        title_lay.addWidget(make_setting_row(self.cb_title_english))
        title_lay.addWidget(make_setting_row(self.cb_title_hebrew))
        title_lay.addWidget(make_setting_row(self.cb_title_punc))
        layout.addWidget(title_grp)

        # Filename clean settings
        fn_grp = make_section(t("meta_clean_filename_group"))
        fn_lay = section_layout(fn_grp)

        self.cb_fn_brackets = CheckMarkBox(t("meta_clean_filename_brackets"))
        self.cb_fn_brackets.setChecked(getattr(self._cfg, "tag_clean_filename_smart_brackets", True))
        self.cb_fn_brackets.setToolTip(t("meta_clean_filename_brackets_tooltip"))

        self.cb_fn_domains = CheckMarkBox(t("meta_clean_filename_domains"))
        self.cb_fn_domains.setChecked(getattr(self._cfg, "tag_clean_filename_remove_domains", True))

        self.cb_fn_emojis = CheckMarkBox(t("meta_clean_filename_emojis"))
        self.cb_fn_emojis.setChecked(getattr(self._cfg, "tag_clean_filename_remove_emojis", True))

        self.cb_fn_spaces = CheckMarkBox(t("meta_clean_filename_spaces"))
        self.cb_fn_spaces.setChecked(getattr(self._cfg, "tag_clean_filename_fix_spaces", True))

        fn_lay.addWidget(make_setting_row(self.cb_fn_brackets))
        fn_lay.addWidget(make_setting_row(self.cb_fn_domains))
        fn_lay.addWidget(make_setting_row(self.cb_fn_emojis))
        fn_lay.addWidget(make_setting_row(self.cb_fn_spaces))
        layout.addWidget(fn_grp)

        layout.addStretch()

        cancel_btn = make_button(t("meta_cancel"), "cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = make_button(t("meta_save_ok"), "primary")
        ok_btn.clicked.connect(self._accept)
        layout.addWidget(make_footer(cancel_btn, ok_btn))

    def _accept(self) -> None:
        if self._cfg:
            self._cfg.tag_clean_title_remove_brackets = self.cb_title_brackets.isChecked()
            self._cfg.tag_clean_title_remove_web_junk = self.cb_title_english.isChecked()
            self._cfg.tag_clean_title_remove_hebrew = self.cb_title_hebrew.isChecked()
            self._cfg.tag_clean_title_fix_punctuation = self.cb_title_punc.isChecked()
            
            self._cfg.tag_clean_filename_smart_brackets = self.cb_fn_brackets.isChecked()
            self._cfg.tag_clean_filename_remove_domains = self.cb_fn_domains.isChecked()
            self._cfg.tag_clean_filename_remove_emojis = self.cb_fn_emojis.isChecked()
            self._cfg.tag_clean_filename_fix_spaces = self.cb_fn_spaces.isChecked()
            self._cfg.save()
        self.accept()
