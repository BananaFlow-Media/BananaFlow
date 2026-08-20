"""
ui/panels/history_panel.py  –  Download history browser
=========================================================
Full-height panel showing the local download history from HistoryDB.

Features
--------
  - Scrollable list of HistoryRow widgets (newest first).
  - Live keyword search box (filters the visible rows without re-querying
    the DB on every keystroke — uses a 300 ms debounce timer).
  - "Export CSV" button that triggers a QFileDialog save-as.
  - "Clear History" button with a confirmation MessageBox.
  - Responds to HistoryDB updates from AppWindow: add_record() inserts a
    new row at the top without a full reload.

Signals emitted upward
----------------------
redownload_requested(DownloadRecord)  User clicked ↺ on a row.
open_folder_requested(DownloadRecord) User clicked 📁 on a row.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton, SearchLineEdit

from config import AppConfig
from core.history_db import DownloadRecord, HistoryDB
from ui.components.empty_state_icon import EmptyStateIcon
from ui.components.history_row import HistoryRow
from ui.dialogs.styled_dialog import confirm, show_info, show_warning
from ui.i18n import t
from ui.theme_manager import ThemeManager, get_colors

_ERROR = "#f87171"


# ──────────────────────────────────────────────────────────────────────────────
# HistoryPanel
# ──────────────────────────────────────────────────────────────────────────────

class HistoryPanel(QWidget):
    """
    Browsable download history panel.

    Parameters
    ----------
    db     : HistoryDB instance owned by AppWindow.
    config : AppConfig – used for export directory preference.
    parent : Optional Qt parent.
    """

    redownload_requested  = Signal(object)   # DownloadRecord
    open_folder_requested = Signal(object)   # DownloadRecord

    def __init__(
        self,
        db:     HistoryDB,
        config: AppConfig,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._db         = db
        self._config     = config
        self._rows:  list[HistoryRow] = []
        self._all_records: list[DownloadRecord] = []   # unfiltered master list

        # Debounce timer for search input
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)

        self._build()

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

        self.reload()

    # ── Public API ─────────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 – Qt override
        """Refresh history from DB every time the panel becomes visible."""
        super().showEvent(event)
        self.reload()

    def reload(self) -> None:
        """Re-fetch all records from DB and repopulate the list."""
        self._all_records = self._db.fetch_all(limit=500)
        self._populate(self._all_records)

    def add_record(self, record: DownloadRecord) -> None:
        """
        Prepend one new record to the top of the list without a full reload.
        Called by AppWindow after each successful download.
        """
        self._all_records.insert(0, record)
        self._empty_widget.setVisible(False)
        row = self._create_row(record)
        self._rows_layout.insertWidget(0, row)
        self._rows.insert(0, row)
        self._update_count()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setObjectName("historyPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self._title_lbl = QLabel(t("history_header_title"))
        self._title_lbl.setObjectName("historyHeaderTitle")
        root.addWidget(self._title_lbl)

        self._subtitle_lbl = QLabel(t("history_header_subtitle"))
        self._subtitle_lbl.setObjectName("historyHeaderSubtitle")
        root.addWidget(self._subtitle_lbl)

        # ── Header ────────────────────────────────────────────────────────────
        self._header_frame = QFrame()
        self._header_frame.setObjectName("panelHeaderFrame")
        self._header_frame.setFixedHeight(40)
        h_row = QHBoxLayout(self._header_frame)
        h_row.setContentsMargins(0, 0, 0, 0)
        h_row.setSpacing(8)

        self._search_box = SearchLineEdit()
        self._search_box.setObjectName("searchBox")
        self._search_box.setPlaceholderText(t("search_history_placeholder"))
        self._search_box.setFixedHeight(34)
        self._search_box.setMinimumWidth(260)
        self._search_box.textChanged.connect(self._on_search_changed)
        h_row.addWidget(self._search_box)
        h_row.addStretch()

        self._count_lbl = CaptionLabel(t("records_count", n=0, plural="s"))
        self._count_lbl.setObjectName("historyCountLabel")
        h_row.addWidget(self._count_lbl)
        h_row.addSpacing(8)

        self._export_btn = PushButton(t("export_csv"))
        self._export_btn.setObjectName("historyExportBtn")
        self._export_btn.setFixedHeight(34)
        self._export_btn.clicked.connect(self._on_export_csv)
        h_row.addWidget(self._export_btn)

        self._clear_btn = PushButton(t("clear_history"))
        self._clear_btn.setObjectName("historyClearBtn")
        self._clear_btn.setFixedHeight(34)
        self._clear_btn.clicked.connect(self._on_clear_history)
        h_row.addWidget(self._clear_btn)

        root.addWidget(self._header_frame)

        # ── Column headers ────────────────────────────────────────────────────
        self._col_header = QFrame()
        self._col_header.setObjectName("historyColHeaderFrame")
        self._col_header.setFixedHeight(34)
        c_row = QHBoxLayout(self._col_header)
        c_row.setContentsMargins(10, 0, 10, 0)
        c_row.setSpacing(0)

        # Widths are slightly larger to accommodate Hebrew text
        col_labels = [
            ("col_date", 140), ("col_title_artist", 0),
            ("col_platform", 80), ("col_type", 64), ("col_duration", 70),
            ("col_size", 80), ("col_actions", 110),
        ]
        self._col_labels: list[QLabel] = []
        for key, width in col_labels:
            lbl = QLabel(t(key))
            if width:
                lbl.setFixedWidth(width)
            else:
                lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            c_row.addWidget(lbl)
            self._col_labels.append(lbl)

        root.addWidget(self._col_header)

        # ── Scroll area ───────────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setObjectName("historyScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(4, 4, 4, 12)
        self._rows_layout.setSpacing(3)
        self._rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._empty_widget = self._build_empty_state()
        self._rows_layout.addWidget(self._empty_widget)

        self._scroll.setWidget(self._rows_container)
        root.addWidget(self._scroll, stretch=1)

        self._apply_theme()

    def _build_empty_state(self) -> QWidget:
        w = QWidget()
        w.setObjectName("historyEmptyWidget")
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(12)
        v.addWidget(EmptyStateIcon("history", w), alignment=Qt.AlignmentFlag.AlignCenter)
        self._empty_title = BodyLabel(t("history_empty_title"))
        self._empty_title.setObjectName("historyEmptyTitle")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._empty_title)

        self._empty_hint = BodyLabel(t("history_empty_subtitle"))
        self._empty_hint.setObjectName("historyEmptyHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._empty_hint)
        return w

    # ── Theme ──────────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        c = get_colors()
        self.setStyleSheet(f"""
            HistoryPanel {{
                background: {c.bg};
            }}
            QLabel#historyHeaderTitle {{
                color: {c.text_primary};
                background: transparent;
                font-size: 20px;
                font-weight: 800;
            }}
            QLabel#historyHeaderSubtitle {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 12px;
            }}
            QFrame#panelHeaderFrame {{
                background: transparent;
                border: none;
            }}
            SearchLineEdit#searchBox {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 10px;
                color: {c.text_primary};
                font-size: 12px;
                padding: 0 12px;
            }}
            SearchLineEdit#searchBox:focus {{
                border-color: {c.accent};
            }}
            QLabel#historyCountLabel {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 11px;
            }}
            QFrame#historyColHeaderFrame {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-bottom: none;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }}
            QFrame#historyColHeaderFrame QLabel {{
                color: {c.text_secondary};
                background: transparent;
                font-size: 10px;
                font-weight: 700;
            }}
            QScrollArea#historyScrollArea {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-top: none;
                border-bottom-left-radius: 10px;
                border-bottom-right-radius: 10px;
            }}
            QWidget#historyEmptyWidget {{
                background: transparent;
            }}
            QLabel#historyEmptyTitle {{
                color: {c.text_primary};
                background: transparent;
                font-size: 13px;
                font-weight: 700;
            }}
            QLabel#historyEmptyHint {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 11px;
            }}
        """)
        self._export_btn.setStyleSheet(self._btn_style(c))
        self._clear_btn.setStyleSheet(self._btn_style(c, _ERROR))

    # ── Populate / filter ──────────────────────────────────────────────────────

    def _populate(self, records: list[DownloadRecord]) -> None:
        """Clear the list and rebuild with `records`."""
        for row in self._rows:
            row.deleteLater()
        self._rows.clear()

        if not records:
            self._empty_widget.setVisible(True)
            self._update_count()
            return

        self._empty_widget.setVisible(False)
        for record in records:
            row = self._create_row(record)
            self._rows_layout.addWidget(row)
            self._rows.append(row)

        self._update_count()

    def _create_row(self, record: DownloadRecord) -> HistoryRow:
        row = HistoryRow(record, parent=None)
        row.open_folder_requested.connect(self.open_folder_requested)
        row.redownload_requested.connect(self.redownload_requested)
        row.delete_requested.connect(self._on_delete_row)
        return row

    def _apply_filter(self) -> None:
        query = self._search_box.text().strip()
        if not query:
            self._populate(self._all_records)
            return
        filtered = self._db.search(query, limit=500)
        self._populate(filtered)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_search_changed(self, _text: str) -> None:
        self._search_timer.start()   # restart debounce

    def _on_delete_row(self, record: DownloadRecord) -> None:
        self._db.delete(record.id)
        try:
            self._all_records.remove(record)
        except ValueError:
            pass
        for row in self._rows:
            if row.record_id() == record.id:
                self._rows.remove(row)
                row.deleteLater()
                break
        if not self._rows:
            self._empty_widget.setVisible(True)
        self._update_count()

    def _on_export_csv(self) -> None:
        default_path = str(Path.home() / "Downloads" / "bananaflow_history.csv")
        path, _ = QFileDialog.getSaveFileName(
            self,
            t("export_dialog_title"),
            default_path,
            "CSV files (*.csv);;All files (*.*)",
        )
        if not path:
            return
        try:
            count = self._db.export_csv(path)
            show_info(
                self,
                t("export_complete"),
                t("export_complete_msg", count=count, path=path),
            )
        except Exception as exc:
            show_warning(
                self,
                t("export_failed"),
                t("export_failed_msg", error=exc),
            )

    def _on_clear_history(self) -> None:
        if confirm(
            self,
            t("clear_history_title"),
            t("clear_history_confirm"),
            accept_text=t("clear_history"),
            cancel_text=t("cancel_btn"),
            danger=True,
        ):
            self._db.clear_all()
            self._all_records.clear()
            self._populate([])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_count(self) -> None:
        n = len(self._rows)
        self._count_lbl.setText(t("records_count", n=n, plural=("s" if n != 1 else "")))

    @staticmethod
    def _btn_style(c, hover_color: str | None = None) -> str:
        hover = hover_color or c.accent
        return f"""
            PushButton {{
                background: transparent;
                border: 1px solid {c.border};
                border-radius: 8px;
                color: {c.text_secondary};
                font-size: 11px;
                padding: 0 10px;
            }}
            PushButton:hover {{
                border-color: {hover};
                color: {hover};
            }}
        """
