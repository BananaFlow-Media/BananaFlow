"""
ui/theme_manager.py  –  Application-wide theme engine  (v3)
============================================================
Changelog v3
------------
* Vibrant Light Theme: completely redesigned with soft gradient surfaces,
  colorful pastel cards, and rich accent-driven highlights — no more gray.
* Custom Accent Colors: ThemeManager.set_accent(name_or_hex) lets the user
  choose from a curated palette (Amber, Emerald, Violet, Rose, Ocean) or
  supply any hex code.  All QSS overlays are rebuilt dynamically on change.
* Accent palette is exported as ACCENT_PALETTE (name → hex) so the Settings
  panel can render a swatch picker without hard-coding colors.
* ThemeManager.apply() signature unchanged — no callers need updating.
* Dead code removed.  Strict type hints.  Modular QSS builders.

Design Token Summary
--------------------
Dark  : deep cool-purple near-black surfaces + accent-driven highlights
Light : warm ivory/lavender base, colorful gradient cards, vivid accents

Light Theme Palette (default Amber accent)
------------------------------------------
  _L_BG        = #f5f7f6  – warm ivory-lavender base
  _L_SURFACE   = #ffffff  – pure-white card surface
  _L_SURFACE2  = #f1f4f2  – soft lavender hover / elevated
  _L_BORDER    = #e6ebe8  – delicate periwinkle border
  _L_TEXT      = #16201c  – deep indigo-black primary text
  _L_TEXT2     = #66706a  – medium muted purple-gray
  _L_TEXT3     = #9aa49d  – light disabled text
  _L_ACCENT    = (dynamic) – user-chosen accent
  _L_GRAD_A    = #eef1ef  – gradient card start (soft violet)
  _L_GRAD_B    = #eef1ef  – gradient card end   (warm peach)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import Theme, setTheme, setThemeColor
from qfluentwidgets.common.config import qconfig

from config import AppConfig

# ──────────────────────────────────────────────────────────────────────────────
# Accent palette  (name → hex)
# ──────────────────────────────────────────────────────────────────────────────
ACCENT_PALETTE: Final[dict[str, str]] = {
    "BananaFlow": "#10A37F",
    "Amber": "#F5A623",  # original brand colour
    "Emerald": "#10b981",
    "Violet": "#7c3aed",
    "Rose": "#f43f5e",
    "Ocean": "#0ea5e9",
    "Coral": "#ff6b6b",
    "Mint": "#06d6a0",
    "Gold": "#f59e0b",
}

# Default accent (brand colour)
ACCENT_COLOR: str = ACCENT_PALETTE["BananaFlow"]
ACCENT_COLOR_DIM: str = "#0B7A5F"  # dimmed variant – recomputed on accent change

# Semantic colours (theme-independent)
SUCCESS_COLOR: str = "#10b981"
ERROR_COLOR: str = "#ef4444"
WARNING_COLOR: str = "#f59e0b"
PROCESSING_COLOR: str = "#8b5cf6"

# Dark-mode design token exports (consumed by component files)
BG_DARK: str = "#0d0d12"
SURFACE_DARK: str = "#16161f"
SURFACE2_DARK: str = "#1e1e2a"
BORDER_DARK: str = "#252533"
TEXT_DARK: str = "#eeeef5"
TEXT2_DARK: str = "#8888a8"
TEXT3_DARK: str = "#4a4a66"

# Light-mode design token exports
BG_LIGHT: str = "#f5f7f6"
SURFACE_LIGHT: str = "#ffffff"
SURFACE2_LIGHT: str = "#f1f4f2"
BORDER_LIGHT: str = "#e6ebe8"
TEXT_LIGHT: str = "#16201c"
TEXT2_LIGHT: str = "#66706a"
TEXT3_LIGHT: str = "#9aa49d"
_CYCLE_ORDER: list[str] = ["dark", "light"]


# ------------------------------------------------------------------------------
# Theme-aware colour helper
# ------------------------------------------------------------------------------
@dataclass
class ThemeColors:
    bg: str
    surface: str
    surface2: str
    border: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    accent: str


def get_colors() -> "ThemeColors":
    """Return the correct colour set for the current theme (dark or light)."""
    inst = ThemeManager._instance  # noqa: SLF001
    dark = (inst._current == "dark") if inst else True  # noqa: SLF001
    accent = inst._accent if inst else ACCENT_COLOR  # noqa: SLF001
    return ThemeColors(
        bg=BG_DARK if dark else BG_LIGHT,
        surface=SURFACE_DARK if dark else SURFACE_LIGHT,
        surface2=SURFACE2_DARK if dark else SURFACE2_LIGHT,
        border=BORDER_DARK if dark else BORDER_LIGHT,
        text_primary=TEXT_DARK if dark else TEXT_LIGHT,
        text_secondary=TEXT2_DARK if dark else TEXT2_LIGHT,
        text_tertiary=TEXT3_DARK if dark else TEXT3_LIGHT,
        accent=accent,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _dim_hex(hex_color: str, factor: float = 0.75) -> str:
    """Return a darkened version of a hex color (for hover/dim states)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r2 = max(0, int(r * factor))
    g2 = max(0, int(g * factor))
    b2 = max(0, int(b * factor))
    return f"#{r2:02x}{g2:02x}{b2:02x}"


def _lighten_hex(hex_color: str, alpha_hex: str = "22") -> str:
    """Return color + alpha suffix for rgba simulation via QSS hex8."""
    return f"{hex_color}{alpha_hex}"


# ──────────────────────────────────────────────────────────────────────────────
# QSS builders  (dynamic – rebuilt when accent changes)
# ──────────────────────────────────────────────────────────────────────────────
def _build_dark_qss(accent: str) -> str:
    dim = _dim_hex(accent)
    return f"""
/* ══════════════════════════════════════════════════════════════════════════
   BananaFlow Dark Theme  v3  (deep purple-tinted premium palette)
   ══════════════════════════════════════════════════════════════════════════ */
QWidget {{
    background-color: #0d0d12;
    color: #eeeef5;
    selection-background-color: {accent};
    selection-color: #000000;
}}

#navigationInterface, #navigationPanel, #navigationPanel #scrollWidget {{
    background: #0d0d12;
    border-left: 1px solid #36364f;
    border-right: 1px solid #36364f;
}}

QScrollArea, QScrollArea > QWidget > QWidget, QAbstractScrollArea {{
    background-color: #0d0d12;
    border: none;
}}

/* Thin modern scrollbars */
QScrollBar:vertical {{
    background: #0d0d12; width: 6px; border-radius: 3px; margin: 0;
}}

QScrollBar::handle:vertical {{
    background: #252533; border-radius: 3px; min-height: 28px;
}}

QScrollBar::handle:vertical:hover {{ background: {accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: #0d0d12; height: 6px; border-radius: 3px; margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: #252533; border-radius: 3px; min-width: 28px;
}}

QScrollBar::handle:horizontal:hover {{ background: {accent}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QToolTip {{
    background-color: #1e1e2a;
    color: #eeeef5;
    border: 1px solid {accent};
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
}}

QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: #16161f;
    color: #eeeef5;
    border: 1px solid #252533;
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: {accent};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {accent};
}}

QComboBox {{
    background-color: #16161f;
    color: #eeeef5;
    border: 1px solid #252533;
    border-radius: 7px;
    padding: 5px 30px 5px 12px;
}}

QComboBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{
    border: none;
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
}}

QComboBox QAbstractItemView {{
    background-color: #16161f;
    color: #eeeef5;
    border: 1px solid #252533;
    selection-background-color: {accent};
    selection-color: #000000;
}}

QCheckBox {{ color: #eeeef5; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 2px solid #252533;
    border-radius: 4px;
    background: #16161f;
}}

QCheckBox::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
}}

QGroupBox {{
    border: 1px solid #252533;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 8px;
    color: #8888a8;
    font-size: 11px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top;
    padding: 0 8px;
    color: {accent};
}}

QProgressBar {{
    background-color: #1e1e2a;
    border: none;
    border-radius: 3px;
    color: transparent;
    height: 6px;
}}

QProgressBar::chunk {{
    background-color: {accent};
    border-radius: 3px;
}}

QStatusBar {{
    background-color: #0d0d12;
    color: #8888a8;
    border-top: 1px solid #252533;
}}

QMenu {{
    background-color: #16161f;
    color: #eeeef5;
    border: 1px solid #252533;
    border-radius: 8px;
    padding: 4px;
}}

QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: #1e1e2a; color: {accent}; }}
QMenu::separator {{ background-color: #252533; height: 1px; margin: 4px 8px; }}
QSpinBox, QDoubleSpinBox {{
    background-color: #16161f;
    color: #eeeef5;
    border: 1px solid #252533;
    border-radius: 6px;
    padding: 4px 8px;
}}

QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {accent}; }}
QSlider::groove:horizontal {{
    background: #252533; height: 4px; border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {accent};
    width: 14px; height: 14px;
    margin: -5px 0; border-radius: 7px;
}}

QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}

/* ── qfluentwidgets SettingCard overrides (dark) ──────────────────────── */
SettingCard, PushSettingCard, SwitchSettingCard, ComboBoxSettingCard,
HyperlinkCard, ExpandSettingCard, RangeSettingCard, OptionsSettingCard,
ColorSettingCard, FolderListSettingCard, CustomColorSettingCard,
_AccentPickerCard, _SpinnerSettingCard, _TextSettingCard, _LanguageSettingCard {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 8px;
}}

SettingCard:hover, PushSettingCard:hover, SwitchSettingCard:hover,
ComboBoxSettingCard:hover, HyperlinkCard:hover, ExpandSettingCard:hover,
_AccentPickerCard:hover, _SpinnerSettingCard:hover, _TextSettingCard:hover,
_LanguageSettingCard:hover {{
    background-color: #1e1e2a;
}}

SettingCardGroup {{
    background-color: transparent;
    border: none;
}}

SettingCardGroup > QLabel {{
    color: #eeeef5;
    font-weight: 700;
    font-size: 15px;
    background: transparent;
}}

SettingCard > QLabel, PushSettingCard > QLabel, SwitchSettingCard > QLabel,
ComboBoxSettingCard > QLabel, HyperlinkCard > QLabel,
ExpandSettingCard > QLabel, RangeSettingCard > QLabel,
_AccentPickerCard > QLabel, _SpinnerSettingCard > QLabel,
_TextSettingCard > QLabel, _LanguageSettingCard > QLabel {{
    color: #eeeef5;
    background: transparent;
}}

SettingCard #titleLabel, PushSettingCard #titleLabel, SwitchSettingCard #titleLabel,
ComboBoxSettingCard #titleLabel, HyperlinkCard #titleLabel,
ExpandSettingCard #titleLabel, RangeSettingCard #titleLabel,
_AccentPickerCard #titleLabel, _SpinnerSettingCard #titleLabel,
_TextSettingCard #titleLabel, _LanguageSettingCard #titleLabel {{
    color: #eeeef5;
    background: transparent;
    font-weight: 600;
}}

SettingCard #contentLabel, PushSettingCard #contentLabel, SwitchSettingCard #contentLabel,
ComboBoxSettingCard #contentLabel, HyperlinkCard #contentLabel,
ExpandSettingCard #contentLabel, RangeSettingCard #contentLabel,
_AccentPickerCard #contentLabel, _SpinnerSettingCard #contentLabel,
_TextSettingCard #contentLabel, _LanguageSettingCard #contentLabel {{
    color: #8888a8;
    background: transparent;
}}

/* -- Custom Cards and Dialogs (Dark Additions) -- */

/* TrackCard */
QFrame#trackCard {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 12px;
}}

QFrame#trackCard:hover {{
    border-color: {accent}44;
}}

QFrame#trackCard QLabel#trackCardThumb {{
    border-radius: 6px;
    border: 1px solid #252533;
}}

QFrame#trackCard QLabel#trackCardTitle {{
    color: #eeeef5;
    background: transparent;
}}

QFrame#trackCard QLabel#trackCardArtist {{
    color: #8888a8;
    background: transparent;
}}

QFrame#trackCard QLabel#trackCardSpeed {{
    color: #4a4a66;
    background: transparent;
    font-size: 9px;
}}

QFrame#trackCard QProgressBar#trackCardProgressBar {{
    background: #252533;
    border: none;
    border-radius: 1px;
}}

QFrame#trackCard QProgressBar#trackCardProgressBar::chunk {{
    background: {accent};
    border-radius: 1px;
}}

QFrame#trackCard QLabel#trackCardDot {{
    background: transparent;
    font-size: 10px;
}}

/* SearchResultCard */
SearchResultCard {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 8px;
}}

SearchResultCard:hover {{
    background-color: #1e1e2a;
    border: 1px solid {accent};
}}

SearchResultCard QLabel#resultRank {{
    color: #4a4a66;
    font-size: 10px;
    background: transparent;
}}

SearchResultCard QLabel#resultTitle {{
    color: #eeeef5;
    background: transparent;
}}

SearchResultCard QLabel#resultSub {{
    color: #8888a8;
    background: transparent;
}}

SearchResultCard QLabel#resultThumb {{
    border: 1px solid #252533;
    background: #16161f;
}}

/* HistoryRow */
HistoryRow {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 8px;
}}

HistoryRow:hover {{
    background-color: #1e1e2a;
    border: 1px solid {accent};
}}

HistoryRow QLabel#historyDate {{
    color: #4a4a66;
    background: transparent;
}}

HistoryRow QLabel#historyTitle {{
    color: #eeeef5;
    background: transparent;
    font-size: 11px;
}}

HistoryRow QLabel#historyArtist {{
    color: #8888a8;
    background: transparent;
}}

HistoryRow QLabel#historyDur {{
    color: #8888a8;
    background: transparent;
}}

HistoryRow QLabel#historySize {{
    color: #8888a8;
    background: transparent;
}}

HistoryRow ToolButton#historyBtn {{
    background: transparent;
    border: none;
    color: #4a4a66;
    font-size: 12px;
}}

HistoryRow ToolButton#historyBtn:hover {{
    color: {accent};
}}

HistoryRow ToolButton#historyDelBtn {{
    background: transparent;
    border: none;
    color: #4a4a66;
    font-size: 11px;
}}

HistoryRow ToolButton#historyDelBtn:hover {{
    color: #ef4444;
}}

/* Dialogs */
QDialog {{
    background-color: #101018;
    color: #eeeef5;
}}

QDialog QScrollArea {{
    background: #101018;
    border: 1px solid #252533;
    border-radius: 0px;
}}

QDialog QScrollArea > QWidget > QWidget {{
    background: #101018;
    border: none;
}}

QDialog QWidget {{
    background: transparent;
    border: none;
}}

QDialog QLineEdit {{
    background-color: #16161f;
    color: #eeeef5;
    border: 1px solid #303044;
    border-radius: 0px;
    padding: 7px 10px;
    selection-background-color: {accent};
}}

QDialog QLineEdit:focus {{
    border: 1px solid {accent};
    background-color: #1b1b27;
}}

QDialog QFrame#dialogHeaderFrame {{
    background: transparent;
    border: none;
    padding-bottom: 2px;
}}

QDialog QLabel#dialogHeaderIcon {{
    background-color: #1e1e2a;
    border: 1px solid #303044;
    border-radius: 0px;
}}

QDialog QLabel#dialogTitle {{
    color: #f6f6fb;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}}

QDialog QLabel#dialogSubtitle {{
    color: #9a9ab8;
    font-size: 12px;
    line-height: 18px;
    background: transparent;
}}

QDialog QFrame#dialogSection {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 0px;
}}

QDialog QLabel#dialogSectionTitle {{
    color: #f6f6fb;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
}}

QDialog QFrame#dialogSettingRow {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 0px;
}}

QDialog QFrame#dialogSettingRow:hover {{
    background-color: #1e1e2a;
    border-color: #303044;
}}

QDialog QFrame#dialogFooter {{
    border: none;
    border-top: 1px solid #252533;
    background: transparent;
}}

QDialog QFrame#fileCard {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 0px;
}}

QDialog QFrame#fileCard:hover {{
    border: 1px solid {accent};
}}

QDialog QPushButton {{
    background-color: #1e1e2a;
    color: #eeeef5;
    font-weight: 600;
    border: 1px solid #303044;
    border-radius: 8px;
    padding: 0 14px;
}}

QDialog QPushButton:hover {{
    background-color: #252533;
    border-color: {accent};
}}

QDialog QPushButton:pressed {{
    background-color: #303044;
}}

QDialog QPushButton:disabled {{
    background-color: #1e1e2a;
    color: #4a4a66;
    border-color: #252533;
}}

QDialog QPushButton[dialogRole="primary"], QDialog PrimaryPushButton {{
    background-color: {accent};
    color: #111118;
    border: 1px solid {accent};
    font-weight: 700;
}}

QDialog QPushButton[dialogRole="primary"]:hover, QDialog PrimaryPushButton:hover {{
    background-color: {dim};
    border-color: {dim};
}}

QDialog QPushButton[dialogRole="danger"] {{
    background-color: #ef4444;
    color: #ffffff;
    border: 1px solid #ef4444;
    font-weight: 700;
}}

QDialog QPushButton[dialogRole="danger"]:hover {{
    background-color: #dc2626;
    border-color: #dc2626;
}}

QDialog QPushButton#cancelBtn, QDialog QPushButton[dialogRole="cancel"] {{
    background-color: #1e1e2a;
    color: #eeeef5;
    border: 1px solid #252533;
}}

QDialog QPushButton#cancelBtn:hover, QDialog QPushButton[dialogRole="cancel"]:hover {{
    background-color: #252533;
}}

QDialog QPushButton[dialogRole="icon"], QDialog QPushButton#dialogInfoBtn {{
    background-color: transparent;
    color: #9a9ab8;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0;
}}

QDialog QPushButton[dialogRole="icon"]:hover, QDialog QPushButton#dialogInfoBtn:hover {{
    background-color: #1e1e2a;
    color: {accent};
    border-color: #303044;
}}

QDialog QCheckBox {{
    background: transparent;
    border: none;
    color: #eeeef5;
    spacing: 8px;
}}

QDialog QFrame#fileCard QLabel {{
    border: none;
    background: transparent;
}}

QDialog QFrame#fileCard QLabel#fileCardName {{
    color: #eeeef5;
}}

QDialog QFrame#fileCard QLabel#fileCardPath {{
    font-size: 11px;
    color: #8888a8;
}}

QDialog QFrame#fileCard QLabel#fileCardMeta {{
    font-size: 11px;
    color: #4a4a66;
}}

QDialog _DuplicateCard {{
    background-color: #1e1e2a;
    border: 1px solid #252533;
    border-radius: 0px;
}}

QDialog _AppearanceRow {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 0px;
}}

QDialog QLabel#dialogWarnIcon {{
    color: #f59e0b;
    font-size: 14px;
    background: transparent;
}}

QDialog QLabel#duplicateCardTitle {{
    color: #eeeef5;
    font-weight: 600;
    background: transparent;
}}

QDialog QLabel#duplicateCardCount {{
    color: #4a4a66;
    background: transparent;
}}

QDialog QFrame#duplicateCardDivider {{
    color: #252533;
}}

QDialog QLabel#duplicateCardHeader {{
    color: #4a4a66;
    background: transparent;
    font-size: 10px;
}}

QDialog QLabel#duplicateCardEmpty {{
    color: #4a4a66;
    background: transparent;
}}

QDialog QLabel#dialogMainIcon {{
    font-size: 22px;
    background: transparent;
}}

QDialog QLabel#dialogMainTitle {{
    color: #eeeef5;
    background: transparent;
    font-weight: bold;
}}

QDialog QLabel#dialogMainDesc {{
    color: #8888a8;
    background: transparent;
}}

QDialog QScrollArea#dialogNoBorderScroll {{
    background: transparent;
    border: none;
}}

QDialog QScrollArea#dialogNoBorderScroll > QWidget > QWidget {{
    background: transparent;
    border: none;
}}

QDialog QLabel#dialogSpinner {{
    font-size: 32px;
    background: transparent;
}}

QDialog QProgressBar#dialogProgressBar {{
    background: #252533;
    border-radius: 0px;
    border: none;
}}

QDialog QProgressBar#dialogProgressBar::chunk {{
    background: {accent};
    border-radius: 0px;
}}

QDialog QLabel#dialogProgressLabel {{
    color: #8888a8;
    background: transparent;
}}

QDialog QLabel#duplicateGroupLabel {{
    color: {accent};
    padding: 4px 0 2px 0;
    border: none;
    border-bottom: 1px solid #252533;
}}

QDialog QLabel#dialogHeader {{
    font-size: 15px;
    color: #f6f6fb;
    font-weight: 700;
    padding: 4px 0;
    border: none;
}}

QDialog QLabel#dialogHint {{
    font-size: 12px;
    color: #9a9ab8;
    border: none;
    padding-bottom: 2px;
}}

_TabCard {{
    background-color: #16161f;
    border: 2px solid #252533;
    border-radius: 0px;
}}

_TabCard[checked="true"] {{
    background-color: #1e1e2a;
    border-color: {accent};
}}

_TabCard QLabel#tabCardIcon {{
    font-size: 26px;
    background: transparent;
}}

_TabCard QLabel#tabCardName {{
    color: #eeeef5;
    background: transparent;
    font-weight: 600;
}}

_TabCard QLabel#tabCardCount {{
    color: #8888a8;
    background: transparent;
}}

_TabCard QLabel#tabCardCheck {{
    color: #10b981;
    background: transparent;
    font-size: 13px;
    font-weight: bold;
}}

/* -- Panel Styling (Dark) -- */
SearchPanel, HistoryPanel, QueuePanel, ConverterPanel, SettingsPanel {{
    background-color: #0d0d12;
}}

QFrame#panelHeaderFrame {{
    background-color: #16161f;
    border: none;
    border-bottom: 1px solid #252533;
}}

SearchPanel SearchLineEdit, HistoryPanel SearchLineEdit {{
    background-color: #0d0d12;
    border: 1px solid #252533;
    border-radius: 8px;
    color: #eeeef5;
    font-size: 12px;
}}

SearchPanel SearchLineEdit:focus, HistoryPanel SearchLineEdit:focus {{
    border-color: {accent};
}}

QLabel#searchResultsLabel {{
    color: #8888a8;
    background: transparent;
}}

QLabel#historyCountLabel, QLabel#queueCountLabel, QLabel#queueStatsLabel {{
    color: #4a4a66;
    background: transparent;
}}

QLabel#converterHeaderLabel {{
    color: #eeeef5;
    font-size: 20px;
    font-weight: 700;
    background: transparent;
}}

QLabel#converterSubLabel {{
    color: #8888a8;
    font-size: 12px;
    background: transparent;
}}

QLabel#converterDropHintLabel {{
    color: #4a4a66;
    font-size: 14px;
    background: transparent;
}}

QFrame#historyColHeaderFrame {{
    background-color: #16161f;
    border-bottom: 1px solid #252533;
}}

QFrame#historyColHeaderFrame QLabel {{
    color: #4a4a66;
    font-size: 10px;
    background: transparent;
}}

PushButton#searchClearBtn, PushButton#historyExportBtn, PushButton#historyClearBtn {{
    background: transparent;
    border: 1px solid #252533;
    border-radius: 6px;
    color: #8888a8;
    font-size: 11px;
    padding: 0 8px;
}}

PushButton#searchClearBtn:hover, PushButton#historyExportBtn:hover {{
    border-color: {accent};
    color: {accent};
}}

PushButton#historyClearBtn:hover {{
    border-color: #ef4444;
    color: #ef4444;
}}

ToolButton#queuePauseResumeBtn {{
    background: transparent;
    border: 1px solid #252533;
    border-radius: 6px;
    color: {accent};
}}

ToolButton#queuePauseResumeBtn:hover {{
    background: rgba(128,128,128,0.10);
}}

DropDownPushButton#queueCleanupBtn {{
    background: transparent;
    border: 1px solid #252533;
    border-radius: 6px;
    color: #8888a8;
    font-size: 11px;
    min-width: 78px;
    padding: 0 26px 0 10px;
}}

DropDownPushButton#queueCleanupBtn:hover {{
    border-color: {accent};
    color: {accent};
}}

DropDownPushButton#searchPlatformBtn {{
    padding: 0 30px 0 12px;
    font-size: 12px;
}}

QFrame#converterControlsFrame {{
    background: #16161f;
    border: 1px solid #252533;
    border-radius: 10px;
}}

QFrame#converterControlsFrame QLabel {{
    color: #8888a8;
    background: transparent;
}}

PushButton#converterConvertBtn {{
    background-color: {accent};
    color: #000000;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 700;
}}

PushButton#converterConvertBtn:hover {{
    background-color: {dim};
}}

PushButton#converterConvertBtn:disabled {{
    background-color: #252533;
    color: #4a4a66;
}}

QScrollArea#converterScrollArea {{
    background: #0d0d12;
    border: 2px dashed #252533;
    border-radius: 12px;
}}

QListWidget#converterListWidget {{
    background: #0d0d12;
    border: none;
}}

QLabel#historyEmptyHint {{
    color: #4a4a66;
    background: transparent;
}}

QWidget#queueDropArea {{
    background-color: #0d0d12;
}}

/* --- Added rules for instant Dark Mode styling --- */
_DownloadBar {{
    background-color: #16161f;
    border-top: 1px solid #252533;
}}

_DownloadBar QLabel#downloadBarCount {{
    color: #8888a8;
    font-size: 12px;
    background: transparent;
}}

_DownloadBar PrimaryPushButton#downloadBarBtn {{
    background-color: {accent};
    color: #000000;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: bold;
    padding-left: 38px;
    padding-right: 14px;
}}

_DownloadBar PrimaryPushButton#downloadBarBtn:hover {{
    background-color: {dim};
}}

_DownloadBar PrimaryPushButton#downloadBarBtn:disabled {{
    background-color: #1e1e2a;
    color: #4a4a66;
}}

QLabel#searchSectionHeader {{
    color: #8888a8;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 1px;
    padding-left: 10px;
    border-left: 3px solid {accent};
    background: transparent;
}}

SearchResultCard PushButton#browseBtn {{
    background: transparent;
    border: 1px solid {accent};
    border-radius: 6px;
    color: {accent};
    font-size: 11px;
}}

SearchResultCard PushButton#browseBtn:hover {{
    background: {accent};
    color: #000000;
}}

QFrame#trackCard ToolButton#trackCardRemoveBtn {{
    color: #f87171;
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 13px;
}}

QFrame#trackCard ToolButton#trackCardRemoveBtn:hover {{
    background: rgba(255,255,255,0.07);
}}

QFrame#trackCard ToolButton#trackCardPauseBtn {{
    color: {accent};
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 13px;
}}

QFrame#trackCard ToolButton#trackCardPauseBtn:hover {{
    background: rgba(255,255,255,0.07);
}}

QFrame#trackCard ToolButton#trackCardResumeBtn {{
    color: #10b981;
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 13px;
}}

QFrame#trackCard ToolButton#trackCardResumeBtn:hover {{
    background: rgba(255,255,255,0.07);
}}

QFrame#trackCard QLabel#trackCardDot[status="queued"] {{ color: #4a4a66; }}
QFrame#trackCard QLabel#trackCardDot[status="downloading"] {{ color: {accent}; }}
QFrame#trackCard QLabel#trackCardDot[status="matching"] {{ color: #38bdf8; }}
QFrame#trackCard QLabel#trackCardDot[status="waiting"] {{ color: #94a3b8; }}
QFrame#trackCard QLabel#trackCardDot[status="starting"] {{ color: #38bdf8; }}
QFrame#trackCard QLabel#trackCardDot[status="processing"] {{ color: #8b5cf6; }}
QFrame#trackCard QLabel#trackCardDot[status="done"] {{ color: #10b981; }}
QFrame#trackCard QLabel#trackCardDot[status="error"] {{ color: #ef4444; }}
QFrame#trackCard QLabel#trackCardDot[status="cancelled"] {{ color: #f59e0b; }}
QFrame#trackCard QLabel#trackCardDot[status="paused"] {{ color: #f59e0b; }}
QFrame#trackCard QLabel#trackCardPlatBadge {{
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: 600;
}}

QFrame#trackCard QLabel#trackCardPlatBadge[platform="youtube"] {{ background: #ff4444; color: #ffffff; }}
QFrame#trackCard QLabel#trackCardPlatBadge[platform="ytmusic"] {{ background: #ff4444; color: #ffffff; }}
QFrame#trackCard QLabel#trackCardPlatBadge[platform="spotify"] {{ background: #1db954; color: #ffffff; }}
QFrame#trackCard QLabel#trackCardPlatBadge[platform="unknown"] {{ background: #252533; color: #8888a8; }}
QFrame#converterFileRow {{
    background-color: #16161f;
    border: 1px solid #252533;
    border-radius: 8px;
}}

QFrame#converterFileRow QLabel#converterFileName {{
    color: #eeeef5;
    background: transparent;
    font-size: 12px;
}}

QFrame#converterFileRow QLabel#converterFileDir {{
    color: #4a4a66;
    background: transparent;
}}

QFrame#converterFileRow QProgressBar#converterFileBar {{
    background: #252533;
    border: none;
    border-radius: 3px;
}}

QFrame#converterFileRow QProgressBar#converterFileBar::chunk {{
    background: {accent};
    border-radius: 3px;
}}

QFrame#converterFileRow QLabel#converterFileStatus {{
    background: transparent;
    font-size: 11px;
}}

QFrame#converterFileRow QLabel#converterFileStatus[state="converting"] {{ color: {accent}; }}
QFrame#converterFileRow QLabel#converterFileStatus[state="done"] {{ color: #10b981; }}
QFrame#converterFileRow QLabel#converterFileStatus[state="error"] {{ color: #ef4444; }}
QFrame#converterFileRow ToolButton#converterFileRemoveBtn {{
    color: #4a4a66;
    background: transparent;
    border: none;
    font-size: 11px;
}}

QFrame#converterFileRow ToolButton#converterFileRemoveBtn:hover {{
    color: #ef4444;
}}

QDialog QPushButton#dialogInfoBtn {{
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    font-size: 14px;
    color: #8888a8;
    padding: 0;
}}

QDialog QPushButton#dialogInfoBtn:hover {{
    background-color: #1e1e2a;
    border-color: #303044;
    color: {accent};
}}

QDialog QLineEdit,
QDialog QFrame,
QDialog QScrollArea,
QDialog QTableView,
QDialog QListView,
QDialog QTreeView,
QDialog QGroupBox,
QDialog QProgressBar,
QDialog _TabCard,
QDialog _DuplicateCard,
QDialog _AppearanceRow {{
    border-radius: 0px;
}}

QDialog QPushButton,
QDialog PrimaryPushButton,
QDialog PushButton,
QDialog ToolButton,
QDialog QPushButton[dialogRole="icon"],
QDialog QPushButton#dialogInfoBtn {{
    border-radius: 8px;
}}
"""


def _build_light_qss(accent: str) -> str:
    """
    Build the vibrant Light Mode QSS overlay.
    Design vision
    -------------
    * Warm ivory-lavender base (#f5f7f6) – feels airy, not sterile
    * Pure-white cards that pop off the base
    * Soft periwinkle borders that fade into the background
    * Accent colour drives every interactive element: focus rings,
      progress chunks, hover tints, selection highlights
    * Gradient header stripe on key containers (lavender → warm peach)
    * Colorful platform badges, vivid status indicators
    The goal: looks like a modern music app with a light, Material-inspired
    personality.
    """
    dim = _dim_hex(accent)
    faint = accent + "1a"  # 10 % opacity QSS hex8 approximation (not CSS)
    return f"""
/* ══════════════════════════════════════════════════════════════════════════
   BananaFlow Light Theme  v3  (vibrant, colorful, premium)
   ══════════════════════════════════════════════════════════════════════════ */

/* ── Global ────────────────────────────────────────────────────────────── */
QWidget {{
    background-color: #f5f7f6;
    color: #16201c;
    selection-background-color: {accent};
    selection-color: #ffffff;
    font-family: "Segoe UI", "SF Pro Display", system-ui, sans-serif;
}}

QMainWindow, QDialog, QDockWidget {{
    background-color: #f5f7f6;
}}

/* ── Scroll areas ─────────────────────────────────────────────────────── */
QScrollArea, QScrollArea > QWidget > QWidget, QAbstractScrollArea {{
    background-color: #f5f7f6;
    border: none;
}}

/* ── Scrollbars (thin, colorful) ──────────────────────────────────────── */
QScrollBar:vertical {{
    background: #eef1ef;
    width: 7px;
    border-radius: 3px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: #cbd4ce;
    border-radius: 3px;
    min-height: 28px;
}}

QScrollBar::handle:vertical:hover {{ background: {accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{
    background: #eef1ef;
    height: 7px;
    border-radius: 3px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: #cbd4ce;
    border-radius: 3px;
    min-width: 28px;
}}

QScrollBar::handle:horizontal:hover {{ background: {accent}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Tooltips ─────────────────────────────────────────────────────────── */
QToolTip {{
    background-color: #ffffff;
    color: #16201c;
    border: 1.5px solid {accent};
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
}}

/* ── Cards / Frames ───────────────────────────────────────────────────── */
.QFrame {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 12px;
}}

/* ── Inputs ───────────────────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: #ffffff;
    color: #16201c;
    border: 1.5px solid #e6ebe8;
    border-radius: 9px;
    padding: 7px 12px;
    selection-background-color: {accent};
    selection-color: #ffffff;
}}

QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {{
    border-color: #cbd4ce;
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 2px solid {accent};
    background-color: #ffffff;
}}

/* ── ComboBox ─────────────────────────────────────────────────────────── */
QComboBox {{
    background-color: #ffffff;
    color: #16201c;
    border: 1.5px solid #e6ebe8;
    border-radius: 9px;
    padding: 6px 30px 6px 12px;
    min-width: 80px;
}}

QComboBox:hover {{ border-color: #cbd4ce; }}
QComboBox:focus {{ border: 2px solid {accent}; }}
QComboBox::drop-down {{
    border: none;
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
}}

QComboBox QAbstractItemView {{
    background-color: #ffffff;
    color: #16201c;
    border: 1.5px solid #e6ebe8;
    border-radius: 8px;
    selection-background-color: {accent};
    selection-color: #ffffff;
    padding: 4px;
}}

/* ── CheckBox ─────────────────────────────────────────────────────────── */
QCheckBox {{ color: #16201c; spacing: 8px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 2px solid #cbd4ce;
    border-radius: 5px;
    background: #ffffff;
}}

QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{
    background-color: {accent};
    border-color: {accent};
}}

/* ── GroupBox ─────────────────────────────────────────────────────────── */
QGroupBox {{
    background-color: #f1f4f2;
    border: 1.5px solid #e6ebe8;
    border-radius: 12px;
    margin-top: 14px;
    padding-top: 10px;
    font-size: 11px;
    font-weight: 600;
    color: #66706a;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top;
    padding: 0 10px;
    color: {accent};
    font-weight: 700;
    font-size: 12px;
}}

/* ── Progress bars  (vivid) ───────────────────────────────────────────── */
QProgressBar {{
    background-color: #eef1ef;
    border: none;
    border-radius: 5px;
    color: transparent;
    height: 8px;
}}

QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {dim});
    border-radius: 5px;
}}

/* ── Status bar ───────────────────────────────────────────────────────── */
QStatusBar {{
    background-color: #f1f4f2;
    color: #66706a;
    border-top: 1px solid #e6ebe8;
    font-size: 12px;
}}

/* ── Menu ─────────────────────────────────────────────────────────────── */
QMenu {{
    background-color: #ffffff;
    color: #16201c;
    border: 1.5px solid #e6ebe8;
    border-radius: 10px;
    padding: 5px;
}}

QMenu::item {{
    padding: 7px 22px;
    border-radius: 6px;
    font-size: 13px;
}}

QMenu::item:selected {{
    background-color: #f1f4f2;
    color: {accent};
    font-weight: 600;
}}

QMenu::separator {{
    background-color: #e6ebe8;
    height: 1px;
    margin: 4px 10px;
}}

/* ── SpinBox ──────────────────────────────────────────────────────────── */
QSpinBox, QDoubleSpinBox {{
    background-color: #ffffff;
    color: #16201c;
    border: 1.5px solid #e6ebe8;
    border-radius: 7px;
    padding: 5px 10px;
}}

QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {accent}; }}

/* ── Slider ───────────────────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    background: #e6ebe8;
    height: 5px;
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {accent};
    width: 15px; height: 15px;
    margin: -5px 0;
    border-radius: 8px;
}}

QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {dim});
    border-radius: 2px;
}}

/* ── Table / List views ───────────────────────────────────────────────── */
QTableView, QListView, QTreeView {{
    background-color: #ffffff;
    alternate-background-color: #f8faf9;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
    gridline-color: #f1f4f2;
    color: #16201c;
}}

QHeaderView::section {{
    background-color: #f1f4f2;
    color: #66706a;
    border: none;
    border-bottom: 2px solid {accent};
    padding: 6px 10px;
    font-weight: 600;
    font-size: 12px;
}}

QTableView::item:selected, QListView::item:selected, QTreeView::item:selected {{
    background-color: {accent};
    color: #ffffff;
    border-radius: 4px;
}}

/* ── Navigation panel ─────────────────────────────────────────────────── */
#navigationInterface, #navigationPanel, #navigationPanel #scrollWidget {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #eef1ef, stop:1 #eef1ef);
    border-left: 1px solid #d7ded9;
    border-right: 1px solid #d7ded9;
}}

/* ── Tab widget ───────────────────────────────────────────────────────── */
QTabBar::tab {{
    background: #f1f4f2;
    color: #66706a;
    border: 1px solid #e6ebe8;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 8px 18px;
    font-weight: 500;
    margin-right: 2px;
}}

QTabBar::tab:selected {{
    background: #ffffff;
    color: {accent};
    font-weight: 700;
    border-bottom: 3px solid {accent};
}}

QTabBar::tab:hover:!selected {{
    background: #eaf3ef;
    color: {accent};
}}

QTabWidget::pane {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 0 8px 8px 8px;
}}

/* ── Push buttons ─────────────────────────────────────────────────────── */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {dim});
    color: #ffffff;
    border: none;
    border-radius: 9px;
    padding: 8px 20px;
    font-weight: 600;
    font-size: 13px;
}}

QPushButton:hover {{
    background: {dim};
}}

QPushButton:pressed {{
    background: {_dim_hex(accent, 0.60)};
}}

QPushButton:disabled {{
    background: #e6ebe8;
    color: #9aa49d;
}}

QPushButton[flat="true"] {{
    background: transparent;
    color: {accent};
    border: 1.5px solid {accent};
    border-radius: 9px;
}}

QPushButton[flat="true"]:hover {{
    background: #f1f4f2;
}}

/* ── qfluentwidgets SettingCard overrides (light) ─────────────────────── */
SettingCard, PushSettingCard, SwitchSettingCard, ComboBoxSettingCard,
HyperlinkCard, ExpandSettingCard, RangeSettingCard, OptionsSettingCard,
ColorSettingCard, FolderListSettingCard, CustomColorSettingCard,
_AccentPickerCard, _SpinnerSettingCard, _TextSettingCard, _LanguageSettingCard {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
}}

SettingCard:hover, PushSettingCard:hover, SwitchSettingCard:hover,
ComboBoxSettingCard:hover, HyperlinkCard:hover, ExpandSettingCard:hover,
_AccentPickerCard:hover, _SpinnerSettingCard:hover, _TextSettingCard:hover,
_LanguageSettingCard:hover {{
    background-color: #f5f7f6;
    border-color: #cbd4ce;
}}

SettingCardGroup {{
    background-color: transparent;
    border: none;
}}

SettingCardGroup > QLabel {{
    color: #16201c;
    font-weight: 700;
    font-size: 15px;
    background: transparent;
}}

SettingCard > QLabel, PushSettingCard > QLabel, SwitchSettingCard > QLabel,
ComboBoxSettingCard > QLabel, HyperlinkCard > QLabel,
ExpandSettingCard > QLabel, RangeSettingCard > QLabel,
_AccentPickerCard > QLabel, _SpinnerSettingCard > QLabel,
_TextSettingCard > QLabel, _LanguageSettingCard > QLabel {{
    color: #16201c;
    background: transparent;
}}

SettingCard #titleLabel, PushSettingCard #titleLabel, SwitchSettingCard #titleLabel,
ComboBoxSettingCard #titleLabel, HyperlinkCard #titleLabel,
ExpandSettingCard #titleLabel, RangeSettingCard #titleLabel,
_AccentPickerCard #titleLabel, _SpinnerSettingCard #titleLabel,
_TextSettingCard #titleLabel, _LanguageSettingCard #titleLabel {{
    color: #16201c;
    background: transparent;
    font-weight: 600;
}}

SettingCard #contentLabel, PushSettingCard #contentLabel, SwitchSettingCard #contentLabel,
ComboBoxSettingCard #contentLabel, HyperlinkCard #contentLabel,
ExpandSettingCard #contentLabel, RangeSettingCard #contentLabel,
_AccentPickerCard #contentLabel, _SpinnerSettingCard #contentLabel,
_TextSettingCard #contentLabel, _LanguageSettingCard #contentLabel {{
    color: #66706a;
    background: transparent;
}}

/* -- Custom Cards and Dialogs (Light Additions) -- */

/* TrackCard */
QFrame#trackCard {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 12px;
}}

QFrame#trackCard:hover {{
    border-color: {accent}44;
}}

QFrame#trackCard QLabel#trackCardThumb {{
    border-radius: 6px;
    border: 1px solid #e6ebe8;
}}

QFrame#trackCard QLabel#trackCardTitle {{
    color: #16201c;
    background: transparent;
}}

QFrame#trackCard QLabel#trackCardArtist {{
    color: #66706a;
    background: transparent;
}}

QFrame#trackCard QLabel#trackCardSpeed {{
    color: #9aa49d;
    background: transparent;
    font-size: 9px;
}}

QFrame#trackCard QProgressBar#trackCardProgressBar {{
    background: #e6ebe8;
    border: none;
    border-radius: 1px;
}}

QFrame#trackCard QProgressBar#trackCardProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {dim});
    border-radius: 1px;
}}

QFrame#trackCard QLabel#trackCardDot {{
    background: transparent;
    font-size: 10px;
}}

/* SearchResultCard */
SearchResultCard {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
}}

SearchResultCard:hover {{
    background-color: #f5f7f6;
    border: 1px solid {accent};
}}

SearchResultCard QLabel#resultRank {{
    color: #9aa49d;
    font-size: 10px;
    background: transparent;
}}

SearchResultCard QLabel#resultTitle {{
    color: #16201c;
    background: transparent;
}}

SearchResultCard QLabel#resultSub {{
    color: #66706a;
    background: transparent;
}}

SearchResultCard QLabel#resultThumb {{
    border: 1px solid #e6ebe8;
    background: #ffffff;
}}

/* HistoryRow */
HistoryRow {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
}}

HistoryRow:hover {{
    background-color: #f5f7f6;
    border: 1px solid {accent};
}}

HistoryRow QLabel#historyDate {{
    color: #9aa49d;
    background: transparent;
}}

HistoryRow QLabel#historyTitle {{
    color: #16201c;
    background: transparent;
    font-size: 11px;
}}

HistoryRow QLabel#historyArtist {{
    color: #66706a;
    background: transparent;
}}

HistoryRow QLabel#historyDur {{
    color: #66706a;
    background: transparent;
}}

HistoryRow QLabel#historySize {{
    color: #66706a;
    background: transparent;
}}

HistoryRow ToolButton#historyBtn {{
    background: transparent;
    border: none;
    color: #9aa49d;
    font-size: 12px;
}}

HistoryRow ToolButton#historyBtn:hover {{
    color: {accent};
}}

HistoryRow ToolButton#historyDelBtn {{
    background: transparent;
    border: none;
    color: #9aa49d;
    font-size: 11px;
}}

HistoryRow ToolButton#historyDelBtn:hover {{
    color: #ef4444;
}}

/* Dialogs */
QDialog {{
    background-color: #fafcfb;
    color: #16201c;
}}

QDialog QScrollArea {{
    background: #fafcfb;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
}}

QDialog QScrollArea > QWidget > QWidget {{
    background: #fafcfb;
    border: none;
}}

QDialog QWidget {{
    background: transparent;
    border: none;
}}

QDialog QLineEdit {{
    background-color: #ffffff;
    color: #16201c;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
    padding: 7px 10px;
    selection-background-color: {accent};
}}

QDialog QLineEdit:focus {{
    border: 1px solid {accent};
    background-color: #ffffff;
}}

QDialog QFrame#dialogHeaderFrame {{
    background: transparent;
    border: none;
    padding-bottom: 2px;
}}

QDialog QLabel#dialogHeaderIcon {{
    background-color: #f1f4f2;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
}}

QDialog QLabel#dialogTitle {{
    color: #16201c;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}}

QDialog QLabel#dialogSubtitle {{
    color: #66706a;
    font-size: 12px;
    line-height: 18px;
    background: transparent;
}}

QDialog QFrame#dialogSection {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
}}

QDialog QLabel#dialogSectionTitle {{
    color: #16201c;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
}}

QDialog QFrame#dialogSettingRow {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 0px;
}}

QDialog QFrame#dialogSettingRow:hover {{
    background-color: #f1f4f2;
    border-color: #e6ebe8;
}}

QDialog QFrame#dialogFooter {{
    border: none;
    border-top: 1px solid #e6ebe8;
    background: transparent;
}}

QDialog QFrame#fileCard {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
}}

QDialog QFrame#fileCard:hover {{
    border: 1px solid {accent};
}}

QDialog QPushButton {{
    background-color: #f1f4f2;
    color: #16201c;
    font-weight: 600;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
    padding: 0 14px;
}}

QDialog QPushButton:hover {{
    background-color: #ebe6f7;
    border-color: {accent};
}}

QDialog QPushButton:pressed {{
    background-color: #e6ebe8;
}}

QDialog QPushButton:disabled {{
    background: #ece8f6;
    color: #9aa49d;
    border-color: #e6ebe8;
}}

QDialog QPushButton[dialogRole="primary"], QDialog PrimaryPushButton {{
    background-color: {accent};
    color: #ffffff;
    border: 1px solid {accent};
    font-weight: 700;
}}

QDialog QPushButton[dialogRole="primary"]:hover, QDialog PrimaryPushButton:hover {{
    background-color: {dim};
    border-color: {dim};
}}

QDialog QPushButton[dialogRole="danger"] {{
    background-color: #ef4444;
    color: #ffffff;
    border: 1px solid #ef4444;
    font-weight: 700;
}}

QDialog QPushButton[dialogRole="danger"]:hover {{
    background-color: #dc2626;
    border-color: #dc2626;
}}

QDialog QPushButton#cancelBtn, QDialog QPushButton[dialogRole="cancel"] {{
    background-color: #f1f4f2;
    color: #16201c;
    border: 1px solid #e6ebe8;
}}

QDialog QPushButton#cancelBtn:hover, QDialog QPushButton[dialogRole="cancel"]:hover {{
    background-color: #e8e3f4;
}}

QDialog QPushButton[dialogRole="icon"], QDialog QPushButton#dialogInfoBtn {{
    background-color: transparent;
    color: #66706a;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0;
}}

QDialog QPushButton[dialogRole="icon"]:hover, QDialog QPushButton#dialogInfoBtn:hover {{
    background-color: #f1f4f2;
    color: {accent};
    border-color: #e6ebe8;
}}

QDialog QCheckBox {{
    background: transparent;
    border: none;
    color: #16201c;
    spacing: 8px;
}}

QDialog QFrame#fileCard QLabel {{
    border: none;
    background: transparent;
}}

QDialog QFrame#fileCard QLabel#fileCardName {{
    color: #16201c;
}}

QDialog QFrame#fileCard QLabel#fileCardPath {{
    font-size: 11px;
    color: #66706a;
}}

QDialog QFrame#fileCard QLabel#fileCardMeta {{
    font-size: 11px;
    color: #9aa49d;
}}

QDialog _DuplicateCard {{
    background-color: #f1f4f2;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
}}

QDialog _AppearanceRow {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 0px;
}}

QDialog QLabel#dialogWarnIcon {{
    color: #f59e0b;
    font-size: 14px;
    background: transparent;
}}

QDialog QLabel#duplicateCardTitle {{
    color: #16201c;
    font-weight: 600;
    background: transparent;
}}

QDialog QLabel#duplicateCardCount {{
    color: #9aa49d;
    background: transparent;
}}

QDialog QFrame#duplicateCardDivider {{
    color: #e6ebe8;
}}

QDialog QLabel#duplicateCardHeader {{
    color: #9aa49d;
    background: transparent;
    font-size: 10px;
}}

QDialog QLabel#duplicateCardEmpty {{
    color: #9aa49d;
    background: transparent;
}}

QDialog QLabel#dialogMainIcon {{
    font-size: 22px;
    background: transparent;
}}

QDialog QLabel#dialogMainTitle {{
    color: #16201c;
    background: transparent;
    font-weight: bold;
}}

QDialog QLabel#dialogMainDesc {{
    color: #66706a;
    background: transparent;
}}

QDialog QScrollArea#dialogNoBorderScroll {{
    background: transparent;
    border: none;
}}

QDialog QScrollArea#dialogNoBorderScroll > QWidget > QWidget {{
    background: transparent;
    border: none;
}}

QDialog QLabel#dialogSpinner {{
    font-size: 32px;
    background: transparent;
}}

QDialog QProgressBar#dialogProgressBar {{
    background: #e6ebe8;
    border-radius: 0px;
    border: none;
}}

QDialog QProgressBar#dialogProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {dim});
    border-radius: 0px;
}}

QDialog QLabel#dialogProgressLabel {{
    color: #66706a;
    background: transparent;
}}

QDialog QLabel#duplicateGroupLabel {{
    color: {accent};
    padding: 4px 0 2px 0;
    border: none;
    border-bottom: 1px solid #e6ebe8;
}}

QDialog QLabel#dialogHeader {{
    font-size: 15px;
    color: #16201c;
    font-weight: 700;
    padding: 4px 0;
    border: none;
}}

QDialog QLabel#dialogHint {{
    font-size: 12px;
    color: #66706a;
    border: none;
    padding-bottom: 2px;
}}

_TabCard {{
    background-color: #ffffff;
    border: 2px solid #e6ebe8;
    border-radius: 0px;
}}

_TabCard[checked="true"] {{
    background-color: #f1f4f2;
    border-color: {accent};
}}

_TabCard QLabel#tabCardIcon {{
    font-size: 26px;
    background: transparent;
}}

_TabCard QLabel#tabCardName {{
    color: #16201c;
    background: transparent;
    font-weight: 600;
}}

_TabCard QLabel#tabCardCount {{
    color: #66706a;
    background: transparent;
}}

_TabCard QLabel#tabCardCheck {{
    color: #10b981;
    background: transparent;
    font-size: 13px;
    font-weight: bold;
}}

/* -- Panel Styling (Light) -- */
SearchPanel, HistoryPanel, QueuePanel, ConverterPanel, SettingsPanel {{
    background-color: #f5f7f6;
}}

QFrame#panelHeaderFrame {{
    background-color: #ffffff;
    border: none;
    border-bottom: 1px solid #e6ebe8;
}}

SearchPanel SearchLineEdit, HistoryPanel SearchLineEdit {{
    background-color: #f5f7f6;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
    color: #16201c;
    font-size: 12px;
}}

SearchPanel SearchLineEdit:focus, HistoryPanel SearchLineEdit:focus {{
    border-color: {accent};
}}

QLabel#searchResultsLabel {{
    color: #66706a;
    background: transparent;
}}

QLabel#historyCountLabel, QLabel#queueCountLabel, QLabel#queueStatsLabel {{
    color: #9aa49d;
    background: transparent;
}}

QLabel#converterHeaderLabel {{
    color: #16201c;
    font-size: 20px;
    font-weight: 700;
    background: transparent;
}}

QLabel#converterSubLabel {{
    color: #66706a;
    font-size: 12px;
    background: transparent;
}}

QLabel#converterDropHintLabel {{
    color: #9aa49d;
    font-size: 14px;
    background: transparent;
}}

QFrame#historyColHeaderFrame {{
    background-color: #ffffff;
    border-bottom: 1px solid #e6ebe8;
}}

QFrame#historyColHeaderFrame QLabel {{
    color: #9aa49d;
    font-size: 10px;
    background: transparent;
}}

PushButton#searchClearBtn, PushButton#historyExportBtn, PushButton#historyClearBtn {{
    background: transparent;
    border: 1px solid #e6ebe8;
    border-radius: 6px;
    color: #66706a;
    font-size: 11px;
    padding: 0 8px;
}}

PushButton#searchClearBtn:hover, PushButton#historyExportBtn:hover {{
    border-color: {accent};
    color: {accent};
}}

PushButton#historyClearBtn:hover {{
    border-color: #ef4444;
    color: #ef4444;
}}

ToolButton#queuePauseResumeBtn {{
    background: transparent;
    border: 1px solid #e6ebe8;
    border-radius: 6px;
    color: {accent};
}}

ToolButton#queuePauseResumeBtn:hover {{
    background: rgba(128,128,128,0.10);
}}

DropDownPushButton#queueCleanupBtn {{
    background: transparent;
    border: 1px solid #e6ebe8;
    border-radius: 6px;
    color: #66706a;
    font-size: 11px;
    min-width: 78px;
    padding: 0 26px 0 10px;
}}

DropDownPushButton#queueCleanupBtn:hover {{
    border-color: {accent};
    color: {accent};
}}

DropDownPushButton#searchPlatformBtn {{
    padding: 0 30px 0 12px;
    font-size: 12px;
}}

QFrame#converterControlsFrame {{
    background: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 10px;
}}

QFrame#converterControlsFrame QLabel {{
    color: #66706a;
    background: transparent;
}}

PushButton#converterConvertBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 {dim});
    color: #ffffff;
    border: none;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 700;
}}

PushButton#converterConvertBtn:hover {{
    background: {dim};
}}

PushButton#converterConvertBtn:disabled {{
    background-color: #e6ebe8;
    color: #9aa49d;
}}

QScrollArea#converterScrollArea {{
    background: #f5f7f6;
    border: 2px dashed #e6ebe8;
    border-radius: 12px;
}}

QListWidget#converterListWidget {{
    background: #f5f7f6;
    border: none;
}}

QLabel#historyEmptyHint {{
    color: #9aa49d;
    background: transparent;
}}

QWidget#queueDropArea {{
    background-color: #f5f7f6;
}}

/* --- Added rules for instant Light Mode styling --- */
_DownloadBar {{
    background-color: #ffffff;
    border-top: 1px solid #e6ebe8;
}}

_DownloadBar QLabel#downloadBarCount {{
    color: #66706a;
    font-size: 12px;
    background: transparent;
}}

_DownloadBar PrimaryPushButton#downloadBarBtn {{
    background-color: {accent};
    color: #ffffff;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: bold;
    padding-left: 38px;
    padding-right: 14px;
}}

_DownloadBar PrimaryPushButton#downloadBarBtn:hover {{
    background-color: {dim};
}}

_DownloadBar PrimaryPushButton#downloadBarBtn:disabled {{
    background-color: #f1f4f2;
    color: #9aa49d;
}}

QLabel#searchSectionHeader {{
    color: #66706a;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 1px;
    padding-left: 10px;
    border-left: 3px solid {accent};
    background: transparent;
}}

SearchResultCard PushButton#browseBtn {{
    background: transparent;
    border: 1px solid {accent};
    border-radius: 6px;
    color: {accent};
    font-size: 11px;
}}

SearchResultCard PushButton#browseBtn:hover {{
    background: {accent};
    color: #ffffff;
}}

QFrame#trackCard ToolButton#trackCardRemoveBtn {{
    color: #ef4444;
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 13px;
}}

QFrame#trackCard ToolButton#trackCardRemoveBtn:hover {{
    background: rgba(0,0,0,0.05);
}}

QFrame#trackCard ToolButton#trackCardPauseBtn {{
    color: {accent};
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 13px;
}}

QFrame#trackCard ToolButton#trackCardPauseBtn:hover {{
    background: rgba(0,0,0,0.05);
}}

QFrame#trackCard ToolButton#trackCardResumeBtn {{
    color: #10b981;
    background: transparent;
    border: none;
    border-radius: 4px;
    font-size: 13px;
}}

QFrame#trackCard ToolButton#trackCardResumeBtn:hover {{
    background: rgba(0,0,0,0.05);
}}

QFrame#trackCard QLabel#trackCardDot[status="queued"] {{ color: #9aa49d; }}
QFrame#trackCard QLabel#trackCardDot[status="downloading"] {{ color: {accent}; }}
QFrame#trackCard QLabel#trackCardDot[status="matching"] {{ color: #38bdf8; }}
QFrame#trackCard QLabel#trackCardDot[status="waiting"] {{ color: #94a3b8; }}
QFrame#trackCard QLabel#trackCardDot[status="starting"] {{ color: #38bdf8; }}
QFrame#trackCard QLabel#trackCardDot[status="processing"] {{ color: #8b5cf6; }}
QFrame#trackCard QLabel#trackCardDot[status="done"] {{ color: #10b981; }}
QFrame#trackCard QLabel#trackCardDot[status="error"] {{ color: #ef4444; }}
QFrame#trackCard QLabel#trackCardDot[status="cancelled"] {{ color: #f59e0b; }}
QFrame#trackCard QLabel#trackCardDot[status="paused"] {{ color: #f59e0b; }}
QFrame#trackCard QLabel#trackCardPlatBadge {{
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: 600;
}}

QFrame#trackCard QLabel#trackCardPlatBadge[platform="youtube"] {{ background: #ff4444; color: #ffffff; }}
QFrame#trackCard QLabel#trackCardPlatBadge[platform="ytmusic"] {{ background: #ff4444; color: #ffffff; }}
QFrame#trackCard QLabel#trackCardPlatBadge[platform="spotify"] {{ background: #1db954; color: #ffffff; }}
QFrame#trackCard QLabel#trackCardPlatBadge[platform="unknown"] {{ background: #eef1ef; color: #66706a; }}
QFrame#converterFileRow {{
    background-color: #ffffff;
    border: 1px solid #e6ebe8;
    border-radius: 8px;
}}

QFrame#converterFileRow QLabel#converterFileName {{
    color: #16201c;
    background: transparent;
    font-size: 12px;
}}

QFrame#converterFileRow QLabel#converterFileDir {{
    color: #9aa49d;
    background: transparent;
}}

QFrame#converterFileRow QProgressBar#converterFileBar {{
    background: #eef1ef;
    border: none;
    border-radius: 3px;
}}

QFrame#converterFileRow QProgressBar#converterFileBar::chunk {{
    background: {accent};
    border-radius: 3px;
}}

QFrame#converterFileRow QLabel#converterFileStatus {{
    background: transparent;
    font-size: 11px;
}}

QFrame#converterFileRow QLabel#converterFileStatus[state="converting"] {{ color: {accent}; }}
QFrame#converterFileRow QLabel#converterFileStatus[state="done"] {{ color: #10b981; }}
QFrame#converterFileRow QLabel#converterFileStatus[state="error"] {{ color: #ef4444; }}
QFrame#converterFileRow ToolButton#converterFileRemoveBtn {{
    color: #9aa49d;
    background: transparent;
    border: none;
    font-size: 11px;
}}

QFrame#converterFileRow ToolButton#converterFileRemoveBtn:hover {{
    color: #ef4444;
}}

QDialog QPushButton#dialogInfoBtn {{
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    font-size: 14px;
    color: #66706a;
    padding: 0;
}}

QDialog QPushButton#dialogInfoBtn:hover {{
    background-color: #f1f4f2;
    border-color: #e6ebe8;
    color: {accent};
}}

QDialog QLineEdit,
QDialog QFrame,
QDialog QScrollArea,
QDialog QTableView,
QDialog QListView,
QDialog QTreeView,
QDialog QGroupBox,
QDialog QProgressBar,
QDialog _TabCard,
QDialog _DuplicateCard,
QDialog _AppearanceRow {{
    border-radius: 0px;
}}

QDialog QPushButton,
QDialog PrimaryPushButton,
QDialog PushButton,
QDialog ToolButton,
QDialog QPushButton[dialogRole="icon"],
QDialog QPushButton#dialogInfoBtn {{
    border-radius: 8px;
}}
"""


# ------------------------------------------------------------------------------
# ThemeManager
# ------------------------------------------------------------------------------
_THEME_WINDOW_ATTRS: Final[tuple[str, str]] = ("navigationInterface", "stackedWidget")


class ThemeManager(QObject):
    """
    Manages Dark / Light theme switching with dynamic accent colours.
    Singleton: obtain via ThemeManager.instance() after the first construction.

    Signals
    -------
    theme_changed  emitted whenever the theme or accent changes.

    Usage
    -----
    >>> tm = ThemeManager(config)          # first call creates the singleton
    >>> ThemeManager.instance()            # subsequent calls return same object
    >>> tm.apply("light")
    >>> tm.set_accent("Violet")
    >>> tm.cycle()
    """

    theme_changed = Signal()
    _instance: "Optional[ThemeManager]" = None

    @classmethod
    def instance(cls) -> "Optional[ThemeManager]":
        return cls._instance

    def __init__(self, config: AppConfig, parent: "Optional[QObject]" = None) -> None:
        super().__init__(parent)
        ThemeManager._instance = self
        self._config = config
        self._current = config.theme
        saved_accent = getattr(config, "accent_color", None)
        self._accent = saved_accent if saved_accent else ACCENT_COLOR
        self._qss_cache: dict[tuple[str, str], str] = {}
        self._last_app_qss = ""
        # Accessibility (high-contrast) override. When non-empty it is
        # applied INSTEAD of the decorative theme QSS — see set_accessibility_qss.
        self._accessibility_qss = ""
        self._apply_fluent()

    # ---- Public API ----------------------------------------------------------
    @property
    def current(self) -> str:
        return self._current

    @property
    def accent(self) -> str:
        return self._accent

    def apply(self, theme_name: str) -> None:
        """Switch to theme_name immediately and persist to config."""
        if theme_name not in ("dark", "light"):
            theme_name = "dark"
        self._current = theme_name
        self._apply_fluent()
        self._apply_qss()
        self._config.theme = theme_name
        self._config.save()
        self.theme_changed.emit()

    def cycle(self) -> str:
        """Advance to the next theme (dark -> light -> dark) and apply."""
        try:
            idx = _CYCLE_ORDER.index(self._current)
        except ValueError:
            idx = 0
        next_theme = _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]
        self.apply(next_theme)
        return next_theme

    def set_accent(self, name_or_hex: str) -> None:
        """
        Change the active accent colour and rebuild all QSS immediately.

        Parameters
        ----------
        name_or_hex : A key from ACCENT_PALETTE (e.g. "Violet") or any valid
                      hex string (e.g. "#7c3aed").  Both "#" and bare hex
                      strings are accepted.
        """
        if name_or_hex in ACCENT_PALETTE:
            resolved = ACCENT_PALETTE[name_or_hex]
        elif name_or_hex.startswith("#") and len(name_or_hex) in (4, 7):
            resolved = name_or_hex
        elif len(name_or_hex) in (3, 6):
            resolved = f"#{name_or_hex}"
        else:
            return  # invalid – ignore silently
        self._accent = resolved
        # Persist if AppConfig has the field
        if hasattr(self._config, "accent_color"):
            self._config.accent_color = resolved
            self._config.save()
        self._apply_fluent()
        self._apply_qss()
        self.theme_changed.emit()

    def set_accessibility_qss(self, qss: str) -> None:
        """Enable/disable the high-contrast accessibility override.

        When ``qss`` is non-empty it is applied to the theme windows
        *in place of* the decorative theme stylesheet (high contrast is
        meant to replace the theme, not decorate it), and it is
        re-applied automatically after every theme/accent change so a
        theme switch can no longer silently wipe it. Pass "" to restore
        the normal theme.
        This must be the only way accessibility QSS reaches the app:
        appending it to the QApplication stylesheet (the previous
        approach) never worked, because Qt gives window-level
        stylesheets precedence over application-level ones and this
        manager styles the main window directly.
        """
        self._accessibility_qss = qss or ""
        self._apply_qss()

    def theme_display_label(self) -> str:
        return {"dark": "🌙  Dark", "light": "☀️  Light"}.get(self._current, "🌙  Dark")

    def next_theme_label(self) -> str:
        try:
            idx = _CYCLE_ORDER.index(self._current)
        except ValueError:
            idx = 0
        nxt = _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]
        return {"dark": "🌙  Dark", "light": "☀️  Light"}.get(nxt, "🌙  Dark")

    def is_dark_variant(self) -> bool:
        return self._current == "dark"

    # ---- Internal ------------------------------------------------------------
    def _apply_fluent(self) -> None:
        """Sync QFluentWidgets built-in theme + accent colour."""
        fluent_theme = Theme.LIGHT if self._current == "light" else Theme.DARK
        if qconfig.theme != fluent_theme:
            setTheme(fluent_theme, lazy=False)
        current_accent = QColor(qconfig.get(qconfig.themeColor)).name().lower()
        next_accent = QColor(self._accent).name().lower()
        if current_accent != next_accent:
            setThemeColor(self._accent, lazy=False)

    def _apply_qss(self) -> None:
        """Rebuild and apply the QSS overlay for the current theme + accent."""
        app = QApplication.instance()
        if app is None:
            return
        qss = self._accessibility_qss or self._current_qss()
        windows = self._theme_windows(app)
        if windows:
            if self._last_app_qss and app.styleSheet() == self._last_app_qss:
                app.setStyleSheet("")
                self._last_app_qss = ""
            for window in windows:
                if window.styleSheet() != qss:
                    window.setStyleSheet(qss)
                window.update()
            return
        if app.styleSheet() != qss:
            app.setStyleSheet(qss)
            self._last_app_qss = qss

    def _current_qss(self) -> str:
        key = (self._current, self._accent)
        qss = self._qss_cache.get(key)
        if qss is None:
            qss = (
                _build_light_qss(self._accent)
                if self._current == "light"
                else _build_dark_qss(self._accent)
            )
            self._qss_cache[key] = qss
        return qss

    @staticmethod
    def _theme_windows(app: QApplication) -> list[QWidget]:
        return [
            widget
            for widget in app.topLevelWidgets()
            if all(hasattr(widget, attr) for attr in _THEME_WINDOW_ATTRS)
        ]
