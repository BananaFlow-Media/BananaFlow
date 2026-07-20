"""
ui/panels/metadata_editor/shared.py  –  Tag Editor shared constants & styling
==============================================================================
Theme-aware QSS builders, the magic-operation catalogue, default column
widths, inspector page indices, and the single Win11 check-mark painter
used by both the header 'Select All' toggle and the per-row checkboxes.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from ui.models.metadata_table_model import (
    COL_CHECK, COL_FILENAME, COL_TITLE_CUR, COL_TITLE_NEW,
    COL_ARTIST_CUR, COL_ARTIST_NEW, COL_ALBUM_CUR, COL_ALBUM_NEW,
    COL_TRACK_CUR, COL_TRACK_NEW,
    COL_FILENAME_NEW, COL_GENRE_CUR, COL_GENRE_NEW,
    COL_COMMENT_CUR, COL_COMMENT_NEW,
)
from ui.theme_manager import get_colors


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
    COL_CHECK:        28,  # ExplorerFileListView._SIDE_EMPTY_GUTTER
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
}


def btn_style() -> str:
    """Standard op-button style (theme-aware, called fresh each time)."""
    c = get_colors()
    return (
        f"QPushButton {{ background: {c.surface}; color: {c.text_primary};"
        f"  border: 1px solid {c.border};"
        f"  border-radius: 8px; padding: 7px 10px; text-align: left; font-size: 12px; }}"
        f"QPushButton:hover {{ background: {c.surface2}; border-color: {c.accent}; }}"
        f"QPushButton:pressed {{ background: {c.border}; }}"
        f"QPushButton:disabled {{ background: {c.bg}; color: {c.text_tertiary}; border-color: {c.border}; }}"
    )


def primary_btn_style() -> str:
    c = get_colors()
    accent_dim = dim_hex(c.accent)
    return (
        f"QPushButton {{ background: {c.accent}; color: #000; font-weight: bold;"
        f"  border-radius: 8px; padding: 7px 10px; font-size: 12px; }}"
        f"QPushButton:hover {{ background: {accent_dim}; }}"
        f"QPushButton:disabled {{ background: {c.bg}; color: {c.text_tertiary};"
        f"  border: 1px solid {c.border}; }}"
    )


def op_row_qss() -> str:
    """Flat list-row style for inspector action rows (theme-aware).

    Clean, minimal rows separated by a thin divider — no card outline or
    rounded corners — with a subtle hover highlight.
    """
    c = get_colors()
    return (
        f"QFrame#metaOpRow {{ background: {c.surface}; border: 1px solid {c.border};"
        f"  border-radius: 9px; }}"
        f"QFrame#metaOpRow:hover {{ background: {c.surface2}; border-color: {c.accent}; }}"
        f"QLabel#metaOpRowLabel {{ background: transparent; border: none;"
        f"  color: {c.text_primary}; font-size: 12px; }}"
        f"QFrame#metaOpRow[actionEnabled=\"false\"] {{ background: {c.bg}; border-color: {c.border}; }}"
        f"QFrame#metaOpRow[actionEnabled=\"false\"] QLabel#metaOpRowLabel {{ color: {c.text_tertiary}; }}"
    )


def bold_font() -> QFont:
    f = QFont()
    f.setBold(True)
    return f


CB_SIZE = 16   # Win11 rounded check-mark box, px


def paint_check_mark(painter: QPainter, cx: int, cy: int, checked: bool) -> None:
    """Draw the Win11-style rounded check mark centered at (cx, cy).

    Single source of truth for the row checkboxes (FilenameDelegate) and the
    header 'Select All' toggle (MetadataHeaderView) so they stay pixel-identical.
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
