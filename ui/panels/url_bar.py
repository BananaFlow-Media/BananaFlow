"""
ui/panels/url_bar.py  –  URL entry bar
=======================================
Contains the main URL input field plus all associated controls:
  - Paste, batch import, scrape, and Fetch Info buttons
  - A batch import button (opens a .txt file picker)
  - A clipboard-monitor indicator dot
  - A page-scrape shortcut that hands the current URL to ScraperWorker

Signals emitted upward to AppWindow
------------------------------------
fetch_requested(str)        User pressed Enter or "Fetch Info".
batch_import_requested(str) User selected a .txt file; payload is file path.
scrape_requested(str)       User clicked the scrape button; payload is the URL.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QByteArray, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout,
    QLabel, QSizePolicy, QWidget,
)
from qfluentwidgets import (
    FluentIcon, LineEdit, PrimaryPushButton, ToolButton,
)

from config import AppConfig
from ui.direction import force_ltr_input
from ui.theme_manager import ThemeManager, get_colors
from ui.i18n import t

_SUCCESS = "#34d399"

# Classic chain-link glyph (replaces the "external link" square-arrow icon
# that FluentIcon.LINK actually renders as).
_LINK_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
     stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
  <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
</svg>
"""


def _svg_pixmap(svg_template: str, color: str, size: int) -> QPixmap:
    """Render an inline SVG template (with a {color} placeholder) to a pixmap."""
    svg = svg_template.format(color=color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


# ──────────────────────────────────────────────────────────────────────────────
# UrlBar
# ──────────────────────────────────────────────────────────────────────────────

class UrlBar(QFrame):
    """
    The URL-input bar shown at the top of the Queue view.

    Parameters
    ----------
    config : AppConfig – used to read/write batch_import_dir.
    parent : Optional Qt parent.
    """

    fetch_requested        = Signal(str)   # URL string
    batch_import_requested = Signal(str)   # absolute path to .txt file
    scrape_requested       = Signal(str)   # URL string

    def __init__(self, config: AppConfig, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._config          = config
        self._clipboard_active = False
        self._tool_btns: list[ToolButton] = []
        self._build()

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_url(self) -> str:
        """Return the current text in the URL field, stripped."""
        return self._url_entry.text().strip()

    def set_url(self, url: str) -> None:
        """Programmatically set the URL field (e.g., from ClipboardWorker)."""
        self._url_entry.setText(url)
        self._url_entry.setFocus()

    def set_fetching(self, fetching: bool) -> None:
        """Toggle the Fetch button between active and loading state."""
        if fetching:
            self._fetch_btn.setText(t("fetching_button"))
            self._fetch_btn.setEnabled(False)
        else:
            self._fetch_btn.setText(t("fetch_info_button"))
            self._fetch_btn.setEnabled(True)

    def clear_url(self) -> None:
        """Clear the URL field and reset the fetch button."""
        self._url_entry.clear()
        self.set_fetching(False)

    def set_clipboard_monitor_active(self, active: bool) -> None:
        """Update the clipboard indicator dot colour."""
        self._clipboard_active = active
        c = get_colors()
        if active:
            self._clip_dot.setStyleSheet(
                f"color: {_SUCCESS}; background: transparent; font-size: 10px;"
            )
            self._clip_dot.setToolTip(t("clipboard_on_tooltip"))
        else:
            self._clip_dot.setStyleSheet(
                f"color: {c.text_tertiary}; background: transparent; font-size: 10px;"
            )
            self._clip_dot.setToolTip(t("clipboard_off_tooltip"))

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(60)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        # Clipboard indicator dot — kept for set_clipboard_monitor_active(),
        # but no longer shown in the bar (status lives elsewhere).
        self._clip_dot = QLabel("●", self)
        self.set_clipboard_monitor_active(False)   # sets initial style + tooltip
        self._clip_dot.hide()

        # ── Input container (link icon + entry + inline tool buttons) ─────────
        self._input_frame = QFrame()
        self._input_frame.setObjectName("urlInputFrame")
        self._input_frame.setFixedHeight(54)
        cont = QHBoxLayout(self._input_frame)
        cont.setContentsMargins(16, 0, 8, 0)
        cont.setSpacing(6)

        v_center = Qt.AlignmentFlag.AlignVCenter

        self._link_icon = QLabel(self._input_frame)
        self._link_icon.setFixedSize(18, 18)
        self._link_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cont.addWidget(self._link_icon, 0, v_center)

        self._url_entry = LineEdit()
        self._url_entry.setObjectName("urlEntry")
        self._url_entry.setPlaceholderText(t("url_placeholder"))
        # Fixed (not minimum) height: a stretch-to-fill height inside the
        # 54px frame threw off the QLineEdit's own vertical text centering.
        self._url_entry.setFixedHeight(40)
        force_ltr_input(self._url_entry)  # URLs must read L→R even in Hebrew mode
        self._url_entry.returnPressed.connect(self._on_fetch)
        cont.addWidget(self._url_entry, 1, v_center)

        # Inline tool buttons (paste / batch import / scrape) live inside the input.
        paste_btn = self._tool_btn(FluentIcon.PASTE, t("paste_tooltip"))
        paste_btn.clicked.connect(self._on_paste)
        cont.addWidget(paste_btn, 0, v_center)

        batch_btn = self._tool_btn(FluentIcon.DOCUMENT, t("batch_import_tooltip"))
        batch_btn.clicked.connect(self._on_batch_import)
        cont.addWidget(batch_btn, 0, v_center)

        scrape_btn = self._tool_btn(FluentIcon.SEARCH, t("scrape_tooltip"))
        scrape_btn.clicked.connect(self._on_scrape)
        cont.addWidget(scrape_btn, 0, v_center)

        row.addWidget(self._input_frame, stretch=1)

        # ── Fetch button (trailing / left edge in RTL) ────────────────────────
        self._fetch_btn = PrimaryPushButton(t("fetch_info_button"))
        self._fetch_btn.setMinimumSize(140, 54)
        self._fetch_btn.clicked.connect(self._on_fetch)
        row.addWidget(self._fetch_btn)

        self._apply_theme()

    def _tool_btn(self, icon, tooltip: str) -> ToolButton:
        btn = ToolButton(icon) if isinstance(icon, FluentIcon) else ToolButton()
        if not isinstance(icon, FluentIcon):
            btn.setText(icon)
        btn.setFixedSize(32, 32)
        btn.setToolTip(tooltip)
        self._tool_btns.append(btn)
        return btn

    # ── Theme ──────────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        c = get_colors()
        accent = c.accent

        # qfluentwidgets controls (LineEdit, ToolButton, ...) set their own
        # stylesheet directly on themselves in __init__, which always wins
        # over a rule aimed at them from an ancestor's descendant selector
        # (Qt's cascade gives a widget's own stylesheet priority regardless
        # of selector specificity). So LineEdit/ToolButton must be styled
        # via setStyleSheet() on the control itself; plain QFrame is fine
        # to style from here since it has no competing stylesheet.
        self.setStyleSheet(f"""
            UrlBar {{ background: transparent; border: none; }}
            QFrame#urlInputFrame {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 16px;
            }}
            QFrame#urlInputFrame:hover {{
                border-color: {accent};
            }}
        """)

        self._url_entry.setStyleSheet(f"""
            LineEdit {{
                background: transparent;
                border: none;
                color: {c.text_primary};
                font-size: 14px;
                padding: 0 4px;
            }}
        """)

        tool_qss = f"""
            ToolButton {{
                background: {c.surface2};
                border: none;
                border-radius: 9px;
                font-size: 14px;
                color: {c.text_secondary};
            }}
            ToolButton:hover {{
                background: {_dim_color(c.surface2, 0.9)};
                color: {accent};
            }}
        """
        for btn in self._tool_btns:
            btn.setStyleSheet(tool_qss)

        self._link_icon.setStyleSheet("background: transparent; border: none;")
        self._link_icon.setPixmap(_svg_pixmap(_LINK_SVG, c.text_secondary, 18))

        dim_accent = _dim_color(accent)
        self._fetch_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background-color: {accent};
                color: #ffffff;
                border: none;
                border-radius: 16px;
                font-size: 14px;
                font-weight: 700;
            }}
            PrimaryPushButton:hover {{
                background-color: {dim_accent};
            }}
            PrimaryPushButton:disabled {{
                background-color: {c.surface2};
                color: {c.text_tertiary};
            }}
        """)

        # Re-apply clipboard dot with correct off-state color
        self.set_clipboard_monitor_active(self._clipboard_active)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_fetch(self) -> None:
        url = self.get_url()
        if url:
            self.fetch_requested.emit(url)

    def _on_paste(self) -> None:
        from PySide6.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        if clipboard:
            text = clipboard.text().strip()
            if text:
                self._url_entry.setText(text)
                self._url_entry.setFocus()

    def _on_batch_import(self) -> None:
        start_dir = self._config.batch_import_dir or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select URL batch file",
            start_dir,
            "Text files (*.txt);;All files (*.*)",
        )
        if path:
            self._config.batch_import_dir = str(Path(path).parent)
            self._config.save()
            self.batch_import_requested.emit(path)

    def _on_scrape(self) -> None:
        url = self.get_url()
        if url:
            self.scrape_requested.emit(url)


def _dim_color(hex_color: str, factor: float = 0.82) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r = max(0, int(int(h[0:2], 16) * factor))
    g = max(0, int(int(h[2:4], 16) * factor))
    b = max(0, int(int(h[4:6], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"
