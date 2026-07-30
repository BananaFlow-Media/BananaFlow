"""
ui/panels/metadata_editor/shared.py  –  Tag Editor shared constants & styling
==============================================================================
Theme-aware QSS builders, the magic-operation catalogue, default column
widths, inspector page indices, and the single Win11 check-mark painter
used by both the header 'Select All' toggle and the standalone row checkboxes.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from ui.models.metadata_table_model import (
    COL_CHECK, COL_FILENAME, COL_TITLE_CUR, COL_TITLE_NEW,
    COL_ARTIST_CUR, COL_ARTIST_NEW, COL_ALBUM_CUR, COL_ALBUM_NEW,
    COL_TRACK_CUR, COL_TRACK_NEW,
    COL_FILENAME_NEW, COL_GENRE_CUR, COL_GENRE_NEW,
    COL_COMMENT_CUR, COL_COMMENT_NEW, COL_STATUS, COL_GUTTER, COL_END_GUTTER,
)
from ui.theme_manager import get_colors


@dataclass(frozen=True)
class TagEditorColors:
    """The HTML prototype's design tokens, with a dark-theme counterpart.

    The light values intentionally match
    ``docs/design/tag-editor/BananaFlow_Tag_Editor_FINAL_COMPLETE.html``.
    The accent remains the user's live application accent, whose fresh-install
    default is the prototype green.
    """

    bg: str
    surface: str
    surface2: str
    surface3: str
    border: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    accent: str
    accent_dark: str
    accent_soft: str
    warn: str
    warn_soft: str
    danger: str
    danger_soft: str
    info: str
    info_soft: str


def tag_editor_colors() -> TagEditorColors:
    """Return the exact light prototype palette or an equivalent dark one."""
    c = get_colors()
    is_dark = c.bg.lower() == "#0d0d12"
    accent_color = QColor(c.accent)
    accent_dark = (
        "#0B7A5F" if c.accent.upper() == "#10A37F"
        else accent_color.darker(125).name() if accent_color.isValid()
        else c.accent
    )
    if is_dark:
        return TagEditorColors(
            bg=c.bg, surface=c.surface, surface2=c.surface2, surface3="#191923",
            border=c.border, text_primary=c.text_primary,
            text_secondary=c.text_secondary, text_tertiary=c.text_tertiary,
            accent=c.accent, accent_dark=accent_dark,
            accent_soft=f"rgba({accent_color.red()}, {accent_color.green()}, {accent_color.blue()}, 0.14)",
            warn="#F2A640", warn_soft="rgba(242, 166, 64, 0.14)",
            danger="#E06B54", danger_soft="rgba(224, 107, 84, 0.14)",
            info="#6F9FD1", info_soft="rgba(111, 159, 209, 0.14)",
        )
    return TagEditorColors(
        bg="#EAEEEC", surface="#FFFFFF", surface2="#F5F7F6", surface3="#F1F4F2",
        border="#E1E7E3", text_primary="#16201C", text_secondary="#66706A",
        text_tertiary="#9AA49D", accent=c.accent, accent_dark=accent_dark,
        accent_soft=f"rgba({accent_color.red()}, {accent_color.green()}, {accent_color.blue()}, 0.10)",
        warn="#C77700", warn_soft="rgba(199, 119, 0, 0.10)",
        danger="#B54832", danger_soft="rgba(181, 72, 50, 0.10)",
        info="#3A6EA5", info_soft="rgba(58, 110, 165, 0.10)",
    )


def dim_hex(hex_color: str, factor: float = 0.85) -> str:
    """Return a darkened/dimmed variant of a hex color for hover states."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r = max(0, int(int(h[0:2], 16) * factor))
    g = max(0, int(int(h[2:4], 16) * factor))
    b = max(0, int(int(h[4:6], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


# Inspector page indices
PAGE_EMPTY  = 0
PAGE_FOLDER = 1
PAGE_TRACKS = 2

# All magic operations: (op_id, label_translation_key, desc_translation_key)
# Labels/descriptions are looked up via t() at display time so they follow the
# user's current language.
MAGIC_OP_DEFS: list[tuple[str, str, str]] = [
    ("title_strip",              "meta_op_title_strip_label",              "meta_op_title_strip_desc"),
    ("title_full",               "meta_op_title_full_label",               "meta_op_title_full_desc"),
    ("normalize_spaces",         "meta_op_normalize_spaces_label",         "meta_op_normalize_spaces_desc"),
    ("track_num",                "meta_op_track_num_label",                "meta_op_track_num_desc"),
    ("split_at",                 "meta_op_split_at_label",                 "meta_op_split_at_desc"),
    ("album_artist",             "meta_op_album_artist_label",             "meta_op_album_artist_desc"),
    ("strip_junk",               "meta_op_strip_junk_label",               "meta_op_strip_junk_desc"),
    ("clear_comments",           "meta_op_clear_comments_label",           "meta_op_clear_comments_desc"),
    ("clear_track_num",          "meta_op_clear_track_num_label",          "meta_op_clear_track_num_desc"),
    ("clear_year",               "meta_op_clear_year_label",               "meta_op_clear_year_desc"),
    ("clear_genre",              "meta_op_clear_genre_label",              "meta_op_clear_genre_desc"),
    ("clear_title",              "meta_op_clear_title_label",              "meta_op_clear_title_desc"),
    ("clear_artist",             "meta_op_clear_artist_label",             "meta_op_clear_artist_desc"),
    ("clear_album",              "meta_op_clear_album_label",              "meta_op_clear_album_desc"),
    ("clear_album_artist",       "meta_op_clear_album_artist_label",       "meta_op_clear_album_artist_desc"),
    ("clean_filename",           "meta_op_clean_filename_label",           "meta_op_clean_filename_desc"),
    ("strip_filename_numbering", "meta_op_strip_filename_numbering_label", "meta_op_strip_filename_numbering_desc"),
]

# Which ops the auto-arrange button runs by default
DEFAULT_AUTO_OPS: frozenset[str] = frozenset({
    "title_strip", "track_num", "normalize_spaces",
})

DEFAULT_COL_WIDTHS: dict[int, int] = {
    COL_CHECK:        24,  # Fixed checkbox column: 4 px padding on each side
    COL_FILENAME:     260,
    COL_TITLE_CUR:    130,
    COL_TITLE_NEW:    130,
    COL_ARTIST_CUR:   110,
    COL_ARTIST_NEW:   110,
    COL_ALBUM_CUR:    120,
    COL_ALBUM_NEW:    120,
    COL_TRACK_CUR:    55,
    COL_TRACK_NEW:    55,
    COL_FILENAME_NEW: 220,
    COL_GENRE_CUR:    100,
    COL_GENRE_NEW:    100,
    COL_COMMENT_CUR:  150,
    COL_COMMENT_NEW:  150,
    COL_STATUS:       120,
    COL_GUTTER:       17,  # 60% of the old 28 px empty gutter
    COL_END_GUTTER:    9,  # About half of the leading empty gutter
}


def btn_style() -> str:
    """Standard op-button style (theme-aware, called fresh each time)."""
    c = tag_editor_colors()
    return (
        f"QPushButton {{ background: {c.surface}; color: {c.text_primary};"
        f"  border: 1px solid {c.border};"
        f"  border-radius: 9px; padding: 6px 10px; text-align: center; font-size: 12px;"
        f"  min-height: 18px; font-weight: 600; }}"
        f"QPushButton:hover {{ background: {c.surface3}; border-color: {c.border}; }}"
        f"QPushButton:pressed {{ background: {c.border}; }}"
        f"QPushButton:disabled {{ background: {c.bg}; color: {c.text_tertiary}; border-color: {c.border}; }}"
    )


def primary_btn_style() -> str:
    c = tag_editor_colors()
    return (
        f"QPushButton {{ background: {c.accent}; color: #ffffff; font-weight: 800;"
        f"  border: 1px solid {c.accent}; border-radius: 9px; padding: 7px 11px; font-size: 12px; }}"
        f"QPushButton:hover {{ background: {c.accent_dark}; border-color: {c.accent_dark}; }}"
        f"QPushButton:disabled {{ background: {c.bg}; color: {c.text_tertiary};"
        f"  border: 1px solid {c.border}; }}"
    )


def tag_dialog_qss() -> str:
    """Component-level dialog styling copied from the reference prototype."""
    c = tag_editor_colors()
    return (
        f"QDialog[tagEditorDialog=\"true\"] {{ background: {c.surface}; color: {c.text_primary};"
        f" border: 1px solid {c.border}; border-radius: 16px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QWidget {{ color: {c.text_primary}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QFrame#dialogHeaderFrame {{ background: {c.surface};"
        f" border: none; border-bottom: 1px solid {c.border}; padding-bottom: 11px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#dialogHeaderIcon {{ background: {c.accent_soft};"
        f" color: {c.accent}; border: none; border-radius: 10px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#dialogTitle {{ color: {c.text_primary};"
        f" font-size: 16px; font-weight: 800; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#dialogSubtitle {{ color: {c.text_secondary};"
        f" font-size: 11px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QFrame#dialogSection {{ background: {c.surface};"
        f" border: 1px solid {c.border}; border-radius: 11px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#dialogSectionTitle {{ color: {c.text_primary};"
        f" font-size: 12px; font-weight: 800; }}"
        f"QDialog[tagEditorDialog=\"true\"] QFrame#dialogSettingRow:hover {{ background: {c.surface3};"
        f" border-color: {c.border}; border-radius: 8px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QFrame#dialogFooter {{ background: {c.surface};"
        f" border: none; border-top: 1px solid {c.border}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLineEdit,"
        f"QDialog[tagEditorDialog=\"true\"] QComboBox,"
        f"QDialog[tagEditorDialog=\"true\"] QTextEdit,"
        f"QDialog[tagEditorDialog=\"true\"] QPlainTextEdit,"
        f"QDialog[tagEditorDialog=\"true\"] QSpinBox {{ background: {c.surface}; color: {c.text_primary};"
        f" border: 1px solid {c.border}; border-radius: 8px; padding: 6px 9px; min-height: 20px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLineEdit:focus,"
        f"QDialog[tagEditorDialog=\"true\"] QComboBox:focus,"
        f"QDialog[tagEditorDialog=\"true\"] QTextEdit:focus {{ border-color: {c.accent}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QPushButton {{ background: {c.surface};"
        f" color: {c.text_primary}; border: 1px solid {c.border}; border-radius: 9px;"
        f" padding: 0 11px; min-height: 34px; font-weight: 700; }}"
        f"QDialog[tagEditorDialog=\"true\"] QPushButton:hover {{ background: {c.surface3}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QPushButton[dialogRole=\"primary\"],"
        f"QDialog[tagEditorDialog=\"true\"] QPushButton[accentRole=\"primary\"] {{"
        f" background: {c.accent}; color: #ffffff; border-color: {c.accent}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QPushButton[dialogRole=\"danger\"] {{"
        f" background: {c.danger}; color: #ffffff; border-color: {c.danger}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QTableView,"
        f"QDialog[tagEditorDialog=\"true\"] QTableWidget {{ background: {c.surface};"
        f" alternate-background-color: {c.surface2}; color: {c.text_primary};"
        f" border: 1px solid {c.border}; border-radius: 11px; gridline-color: {c.surface3}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QHeaderView::section {{ background: {c.surface3};"
        f" color: {c.text_secondary}; border: none; border-bottom: 1px solid {c.border};"
        f" padding: 7px; font-size: 10.5px; font-weight: 700; }}"
        f"QDialog[tagEditorDialog=\"true\"] QTabBar::tab {{ background: transparent;"
        f" color: {c.text_secondary}; border: none; border-bottom: 2px solid transparent;"
        f" min-height: 34px; padding: 0 13px; font-weight: 800; }}"
        f"QDialog[tagEditorDialog=\"true\"] QTabBar::tab:selected {{ color: {c.accent_dark};"
        f" border-bottom-color: {c.accent}; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#tagDialogFormLabel {{ color: {c.text_secondary};"
        f" font-size: 11.5px; font-weight: 700; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#tagDialogFormValue {{ color: {c.text_primary};"
        f" font-size: 11.5px; font-weight: 800; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#tagDialogResultSuccess {{ background: {c.accent_soft};"
        f" color: {c.accent_dark}; border: none; border-radius: 11px; padding: 12px; font-weight: 700; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#tagDialogResultWarning {{ background: {c.warn_soft};"
        f" color: {c.warn}; border: none; border-radius: 11px; padding: 12px; font-weight: 700; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#tagDialogResultError {{ background: {c.danger_soft};"
        f" color: {c.danger}; border: none; border-radius: 11px; padding: 12px; font-weight: 700; }}"
        f"QDialog[tagEditorDialog=\"true\"] QLabel#dialogHint {{ background: {c.surface3};"
        f" color: {c.text_secondary}; border: none; border-radius: 9px; padding: 9px 11px; }}"
        f"QDialog[tagEditorDialog=\"true\"] QScrollArea {{ background: {c.surface}; border: none; }}"
        f"QDialog[tagEditorDialog=\"true\"] QScrollArea > QWidget > QWidget {{ background: {c.surface}; }}"
    )


def mark_tag_editor_dialog(dialog) -> None:
    """Opt a raw or StyledDialog instance into the Tag Editor modal skin."""
    dialog.setProperty("tagEditorDialog", True)
    dialog.setStyleSheet(dialog.styleSheet() + tag_dialog_qss())


def op_row_qss() -> str:
    """Flat list-row style for inspector action rows (theme-aware).

    Clean, minimal rows separated by a thin divider — no card outline or
    rounded corners — with a subtle hover highlight.
    """
    c = tag_editor_colors()
    return (
        f"QFrame#metaOpRow {{ background: {c.surface}; border: 1px solid {c.border};"
        f"  border-radius: 9px; }}"
        f"QFrame#metaOpRow:hover {{ background: {c.surface2}; border-color: {c.accent}; }}"
        f"QLabel#metaOpRowLabel {{ background: transparent; border: none;"
        f"  color: {c.text_primary}; font-size: 12px; }}"
        f"QFrame#metaOpRow[actionEnabled=\"false\"] {{ background: {c.bg}; border-color: {c.border}; }}"
        f"QFrame#metaOpRow[actionEnabled=\"false\"] QLabel#metaOpRowLabel {{ color: {c.text_tertiary}; }}"
    )


def compact_clear_button_qss() -> str:
    """Style destructive field-clear actions as compact red-outline pills."""
    c = tag_editor_colors()
    return (
        f"QPushButton[cleanupCompact=\"true\"] {{ background: {c.surface}; color: {c.danger};"
        f" border: 1px solid {c.danger}; border-radius: 8px; min-height: 27px;"
        f" padding: 0 10px; font-size: 10.5px; font-weight: 800; }}"
        f"QPushButton[cleanupCompact=\"true\"]:hover {{ background: {c.surface}; color: {c.danger};"
        f" border-color: {c.danger}; }}"
        f"QPushButton[cleanupCompact=\"true\"]:pressed {{ background: {c.surface}; color: {c.danger};"
        f" border-color: {c.danger}; }}"
        f"QPushButton[cleanupCompact=\"true\"]:disabled {{ background: {c.surface};"
        f" color: {c.text_tertiary}; border-color: {c.border}; }}"
    )


def bold_font() -> QFont:
    f = QFont()
    f.setBold(True)
    return f


CB_SIZE = 16   # Win11 rounded check-mark box, px


def paint_check_mark(painter: QPainter, cx: int, cy: int, checked: bool) -> None:
    """Draw the Win11-style rounded check mark centered at (cx, cy).

    Single source of truth for the row checkboxes and the header 'Select All'
    toggle so they stay pixel-identical.
    """
    colors = get_colors()
    r = CB_SIZE // 2
    box = QRect(cx - r, cy - r, CB_SIZE, CB_SIZE)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    if checked:
        accent = QColor(colors.accent)
        fill = QColor(accent)
        fill.setAlpha(82)
        border = QColor(accent)
        border.setAlpha(170)
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.4))
        painter.drawRoundedRect(box, 4, 4)
        mark_color = QColor(accent)
        mark_color.setAlpha(215)
        painter.setPen(QPen(mark_color, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(cx - 5, cy, cx - 2, cy + 4)
        painter.drawLine(cx - 2, cy + 4, cx + 6, cy - 5)
    else:
        border = QColor(colors.text_secondary)
        border.setAlpha(120)
        painter.setBrush(QColor(colors.surface))
        painter.setPen(QPen(border, 1.4))
        painter.drawRoundedRect(box.adjusted(1, 1, -1, -1), 4, 4)
    painter.restore()
