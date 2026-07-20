"""
ui/panels/converter_panel.py  –  Local File Format Converter
=============================================================
A completely separate panel (registered as its own navigation tab)
for converting local audio files between formats via FFmpeg/mutagen.

This panel has ZERO overlap with the online downloader UI.  It deals
only with files already present on the user's disk.

Features
--------
* Drag-and-drop or file-browser input (supports multi-file selection)
* Output format selection: MP3 / M4A / FLAC / OPUS / WAV
* Bitrate selection for lossy formats
* Batch conversion with per-file progress bars
* Output folder: same as input (default) or user-specified
* Conversion runs on a QThread (ConvertWorker) so the UI stays responsive

Signals emitted upward
----------------------
None – the panel is self-contained.  Errors are shown inline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal

from core.converter_service import (
    CancellationToken,
    CollisionPolicy,
    ConversionRequest,
    ConverterService,
    FileOutcome,
    format_preservation_warnings,
)
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel,
    QProgressBar, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, FluentIcon,
    PrimaryPushButton, PushButton, SwitchButton, ToolButton,
)

from ui.components.empty_state_icon import EmptyStateIcon
from ui.direction import force_ltr, force_ltr_label
from ui.i18n import t
from ui.panels.options_bar import _AnchoredComboBox, _PillFrame, _ARROW_ONLY_WIDTH
from ui.theme_manager import (
    get_colors,
    SUCCESS_COLOR, ERROR_COLOR,
)


def _dim_accent_conv(hex_color: str, factor: float = 0.85) -> str:
    """Return a darkened/dimmed variant of a hex color for hover states."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r = max(0, int(int(h[0:2], 16) * factor))
    g = max(0, int(int(h[2:4], 16) * factor))
    b = max(0, int(int(h[4:6], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"

_OUTPUT_FORMATS = ["mp3", "m4a", "flac", "opus", "wav"]

_BITRATE_PRESETS = [
    ("320k", "quality_best", "quality_audio_320"),
    ("256k", "quality_high", "quality_audio_256"),
    ("192k", "quality_balanced", "quality_audio_192"),
    ("128k", "quality_economical", "quality_audio_128"),
    ("96k", "quality_small_file", "quality_audio_96"),
]

from ui.direction import quality_display

_INPUT_EXTS     = {".mp3", ".m4a", ".flac", ".opus", ".wav", ".ogg", ".aac",
                   ".wma", ".alac", ".aiff", ".mp4", ".webm", ".mkv"}


# ──────────────────────────────────────────────────────────────────────────────
# ConvertWorker
# ──────────────────────────────────────────────────────────────────────────────

class ConvertWorker(QThread):
    """
    Background thread that drives :class:`core.converter_service.ConverterService`.

    All conversion logic (collision policy, atomic temp/verify/replace,
    FFmpeg process control, real progress) lives in the core service; this
    thread only forwards results as Qt signals.

    Signals
    -------
    file_started(str)            – source path now being converted
    file_progress(str, float)    – (source, ratio 0..1; -1.0 = indeterminate)
    file_done(str, str)          – (source, destination) on success
    file_error(str, str)         – (source, error message) on failure
    file_skipped(str, str)       – (source, reason)
    file_cancelled(str)          – source whose conversion was cancelled
    batch_progress(int, int)     – (1-based index, total)
    all_done(int, int, int, int) – (completed, failed, skipped, cancelled)
    """

    file_started   = Signal(str)
    file_progress  = Signal(str, float)
    file_done      = Signal(str, str)
    file_error     = Signal(str, str)
    file_skipped   = Signal(str, str)
    file_cancelled = Signal(str)
    batch_progress = Signal(int, int)
    all_done       = Signal(int, int, int, int)

    def __init__(self, requests: list[ConversionRequest], parent=None) -> None:
        super().__init__(parent)
        self._requests = requests
        self._token = CancellationToken()

    def cancel(self) -> None:
        """Stop the batch, terminating the live FFmpeg process."""
        self._token.cancel()

    def run(self) -> None:
        service = ConverterService()
        completed = failed = skipped = cancelled = 0
        total = len(self._requests)
        for index, request in enumerate(self._requests, start=1):
            self.batch_progress.emit(index, total)
            self.file_started.emit(str(request.source))
            result = service.convert_file(
                request,
                on_progress=lambda fp: self.file_progress.emit(
                    fp.source, fp.ratio if fp.ratio is not None else -1.0,
                ),
                cancel=self._token,
            )
            if result.outcome is FileOutcome.COMPLETED:
                completed += 1
                self.file_done.emit(result.source, result.destination or "")
            elif result.outcome is FileOutcome.SKIPPED:
                skipped += 1
                self.file_skipped.emit(result.source, result.message)
            elif result.outcome is FileOutcome.CANCELLED:
                cancelled += 1
                self.file_cancelled.emit(result.source)
            else:
                failed += 1
                self.file_error.emit(
                    result.source, result.message or result.error_kind.value,
                )
        self.all_done.emit(completed, failed, skipped, cancelled)


# ──────────────────────────────────────────────────────────────────────────────
# _FileRow  –  one row in the file list
# ──────────────────────────────────────────────────────────────────────────────

class _FileRow(QFrame):
    remove_requested = Signal(str)

    def __init__(self, file_path: str, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.file_path = file_path
        self._build()

    def _build(self) -> None:
        self.setFixedHeight(52)
        self.setObjectName("converterFileRow")

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 8, 6)
        row.setSpacing(10)

        # File icon
        icon_lbl = QLabel("🎵")
        icon_lbl.setStyleSheet("background: transparent; font-size: 18px;")
        icon_lbl.setFixedWidth(24)
        row.addWidget(icon_lbl)

        # Name + path
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        p = Path(self.file_path)
        self._name_lbl = BodyLabel(p.name[:60])
        self._name_lbl.setObjectName("converterFileName")
        self._dir_lbl  = CaptionLabel(str(p.parent)[:70])
        self._dir_lbl.setObjectName("converterFileDir")
        text_col.addWidget(self._name_lbl)
        text_col.addWidget(self._dir_lbl)
        row.addLayout(text_col, stretch=1)

        # Progress bar (hidden until conversion starts)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)   # indeterminate
        self._bar.setFixedSize(80, 6)
        self._bar.setTextVisible(False)
        self._bar.setVisible(False)
        self._bar.setObjectName("converterFileBar")
        row.addWidget(self._bar)

        # Status label
        self._status_lbl = QLabel("")
        self._status_lbl.setFixedWidth(60)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setObjectName("converterFileStatus")
        row.addWidget(self._status_lbl)

        # Remove button
        self._rm_btn = ToolButton()
        self._rm_btn.setText("✕")
        self._rm_btn.setFixedSize(24, 24)
        self._rm_btn.setObjectName("converterFileRemoveBtn")
        self._rm_btn.clicked.connect(lambda: self.remove_requested.emit(self.file_path))
        row.addWidget(self._rm_btn)

    def set_converting(self) -> None:
        self._bar.setVisible(True)
        self._bar.setRange(0, 0)   # indeterminate until real progress arrives
        self._status_lbl.setText("⏳")
        self._set_state("converting")

    def set_progress(self, ratio: float) -> None:
        """Real FFmpeg progress; ratio < 0 keeps the bar indeterminate."""
        if not self._bar.isVisible():
            self._bar.setVisible(True)
        if ratio < 0:
            self._bar.setRange(0, 0)
            return
        self._bar.setRange(0, 1000)
        self._bar.setValue(int(ratio * 1000))

    def set_done(self) -> None:
        self._bar.setVisible(False)
        self._status_lbl.setText("✅")
        self._set_state("done")

    def set_error(self, msg: str) -> None:
        self._bar.setVisible(False)
        self._status_lbl.setText("❌")
        self._status_lbl.setToolTip(msg)
        self._set_state("error")

    def set_skipped(self, reason: str) -> None:
        self._bar.setVisible(False)
        self._status_lbl.setText("⏭")
        self._status_lbl.setToolTip(reason or t("converter_status_skipped"))
        self._set_state("skipped")

    def set_cancelled(self) -> None:
        self._bar.setVisible(False)
        self._status_lbl.setText("⏹")
        self._status_lbl.setToolTip(t("converter_status_cancelled"))
        self._set_state("cancelled")

    def _set_state(self, state: str) -> None:
        self._status_lbl.setProperty("state", state)
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)


# ──────────────────────────────────────────────────────────────────────────────
# ConverterPanel
# ──────────────────────────────────────────────────────────────────────────────

class ConverterPanel(QWidget):
    """
    Standalone local-file audio converter panel.

    Registered as a top-level navigation item in AppWindow, completely
    separate from the downloader workflow.
    """

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._files:    dict[str, _FileRow] = {}   # path → row widget
        self._worker:   Optional[ConvertWorker] = None
        self._out_dir:  Optional[str] = None
        self._build()
        self.setAcceptDrops(True)
        from ui.theme_manager import ThemeManager
        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setObjectName("converterPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 16, 10, 16)
        root.setSpacing(14)

        # Header
        self._header_frame = QFrame()
        self._header_frame.setObjectName("converterHeaderFrame")
        header_row = QHBoxLayout(self._header_frame)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)

        header_text = QVBoxLayout()
        header_text.setContentsMargins(0, 0, 0, 0)
        header_text.setSpacing(3)
        self._header_lbl = QLabel(t("converter_header_title"))
        self._header_lbl.setObjectName("converterHeaderLabel")
        header_text.addWidget(self._header_lbl)

        self._sub_lbl = QLabel(t("converter_subtitle"))
        self._sub_lbl.setObjectName("converterSubLabel")
        self._sub_lbl.setWordWrap(True)
        header_text.addWidget(self._sub_lbl)
        header_row.addLayout(header_text, stretch=1)

        root.addWidget(self._header_frame)

        # Drop zone / file list
        self._scroll = QScrollArea()
        self._scroll.setObjectName("converterScrollArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setMinimumHeight(260)

        self._list_widget = QWidget()
        self._list_widget.setObjectName("converterListWidget")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(10, 10, 10, 10)
        self._list_layout.setSpacing(6)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._drop_hint = QWidget()
        self._drop_hint.setObjectName("converterDropHintWidget")
        drop_layout = QVBoxLayout(self._drop_hint)
        drop_layout.setContentsMargins(0, 0, 0, 0)
        drop_layout.setSpacing(12)
        drop_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(EmptyStateIcon("upload", self._drop_hint), alignment=Qt.AlignmentFlag.AlignCenter)

        self._drop_title = BodyLabel(t("converter_drop_title"))
        self._drop_title.setObjectName("converterDropTitle")
        self._drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(self._drop_title)

        self._drop_subtitle = CaptionLabel(t("converter_drop_subtitle"))
        self._drop_subtitle.setObjectName("converterDropSubtitle")
        self._drop_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_subtitle.setWordWrap(True)
        drop_layout.addWidget(self._drop_subtitle)

        self._list_layout.addWidget(self._drop_hint)

        self._scroll.setWidget(self._list_widget)
        root.addWidget(self._scroll, stretch=1)

        # Controls row
        self._controls_frame = QFrame()
        self._controls_frame.setObjectName("converterControlsFrame")
        ctrl_row = QHBoxLayout(self._controls_frame)
        ctrl_row.setContentsMargins(16, 10, 16, 10)
        ctrl_row.setSpacing(12)

        # Add files button
        self._add_btn = PushButton(FluentIcon.ADD, t("converter_add_files"))
        self._add_btn.setObjectName("converterAddBtn")
        self._add_btn.setFixedHeight(42)
        self._add_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._add_btn.clicked.connect(self._on_add_files)
        ctrl_row.addWidget(self._add_btn)

        # Clear button
        self._clear_btn = PushButton(FluentIcon.DELETE, t("converter_clear_all"))
        self._clear_btn.setObjectName("converterClearBtn")
        self._clear_btn.setFixedHeight(42)
        self._clear_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._clear_btn.clicked.connect(self._on_clear)
        ctrl_row.addWidget(self._clear_btn)

        # Vertical Separator
        self._sep = QFrame()
        self._sep.setFrameShape(QFrame.Shape.VLine)
        self._sep.setObjectName("converterDivider")
        ctrl_row.addWidget(self._sep)

        # Format selector
        self._fmt_lbl = QLabel(t("converter_output_format").rstrip(":").strip())
        self._fmt_lbl.setObjectName("converterFmtLabel")
        self._fmt_value_lbl = QLabel()
        self._fmt_value_lbl.setObjectName("converterFmtValueLabel")

        self._fmt_group, self._fmt_combo = self._make_group(self._fmt_lbl, self._fmt_value_lbl)
        for fmt in _OUTPUT_FORMATS:
            self._fmt_combo.addItem(fmt.upper(), userData=fmt)
        self._fmt_combo.setCurrentIndex(0)
        self._fmt_value_lbl.setText(self._fmt_combo.currentText())
        self._fmt_combo.currentTextChanged.connect(self._fmt_value_lbl.setText)
        self._fmt_combo.currentTextChanged.connect(self._on_fmt_changed)
        force_ltr(self._fmt_combo)
        force_ltr_label(self._fmt_value_lbl)
        ctrl_row.addWidget(self._fmt_group)

        # Quality selector
        self._br_lbl = QLabel(t("options_quality_label").rstrip(":").strip())
        self._br_lbl.setObjectName("converterBrLabel")
        self._br_value_lbl = QLabel()
        self._br_value_lbl.setObjectName("converterBrValueLabel")

        self._br_group, self._br_combo = self._make_group(self._br_lbl, self._br_value_lbl)
        for val, label_key, detail_key in _BITRATE_PRESETS:
            display_text = quality_display(label_key, detail_key)
            self._br_combo.addItem(display_text, userData=val)
        self._br_combo.setCurrentIndex(0)
        self._br_value_lbl.setText(self._br_combo.currentText())
        self._br_combo.currentTextChanged.connect(self._br_value_lbl.setText)
        force_ltr(self._br_combo)
        force_ltr_label(self._br_value_lbl)
        self._br_value_lbl.setMinimumWidth(120)
        ctrl_row.addWidget(self._br_group)

        ctrl_row.addSpacing(12)

        # Output folder toggle
        self._same_dir_lbl = QLabel(t("converter_same_folder"))
        self._same_dir_lbl.setObjectName("converterSameDirLabel")
        ctrl_row.addWidget(self._same_dir_lbl)

        self._same_dir_sw = SwitchButton()
        self._same_dir_sw.setOnText("")
        self._same_dir_sw.setOffText("")
        self._same_dir_sw.setChecked(True)
        self._same_dir_sw.checkedChanged.connect(self._on_same_dir_toggle)
        ctrl_row.addWidget(self._same_dir_sw)

        self._out_dir_btn = PushButton(FluentIcon.FOLDER, t("converter_output_folder"))
        self._out_dir_btn.setVisible(False)
        self._out_dir_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._out_dir_btn.clicked.connect(self._on_browse_out)
        ctrl_row.addWidget(self._out_dir_btn)

        ctrl_row.addStretch()

        # Live batch counter ("File 2 of 7" / final summary)
        self._count_lbl = CaptionLabel("")
        self._count_lbl.setObjectName("converterCountLabel")
        ctrl_row.addWidget(self._count_lbl)

        # Convert button
        self._convert_btn = PrimaryPushButton(FluentIcon.PLAY_SOLID, t("converter_convert_all_btn"))
        self._convert_btn.setObjectName("converterConvertBtn")
        self._convert_btn.setFixedSize(126, 42)
        self._convert_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._convert_btn.clicked.connect(self._on_convert)
        ctrl_row.addWidget(self._convert_btn)
        self._fmt_combo.setToolTip(t("converter_output_format"))
        self._br_combo.setToolTip(t("converter_bitrate"))

        root.addWidget(self._controls_frame)

        self._apply_theme()

    def _apply_theme(self) -> None:
        c = get_colors()
        dim = _dim_accent_conv(c.accent)

        # Style the ComboBox instances directly to prevent parent selectors from being ignored
        # and to cleanly eliminate any border boxes around the arrow icon.
        combo_qss = """
            ComboBox {
                background: transparent;
                border: none;
                color: transparent;
                padding: 0;
                min-height: 32px;
            }
            ComboBox:hover, ComboBox:pressed, ComboBox:focus, ComboBox:!hover {
                background: transparent;
                border: none;
                color: transparent;
            }
        """
        self._fmt_combo.setStyleSheet(combo_qss)
        self._br_combo.setStyleSheet(combo_qss)

        # Retint the button icons to match the theme's colors
        from PySide6.QtGui import QColor
        self._add_btn.setIcon(FluentIcon.ADD.icon(color=QColor(c.accent)))
        if self._worker and self._worker.isRunning():
            self._convert_btn.setIcon(FluentIcon.CLOSE.icon(color=QColor("#ffffff")))
        else:
            self._convert_btn.setIcon(FluentIcon.PLAY_SOLID.icon(color=QColor("#ffffff")))

        # Parse hex color for rgba format to avoid Qt's #AARRGGBB hex parsing issues
        h = c.accent.lstrip('#')
        r_val = int(h[0:2], 16)
        g_val = int(h[2:4], 16)
        b_val = int(h[4:6], 16)
        add_btn_bg = f"rgba({r_val}, {g_val}, {b_val}, 0.09)"

        # Style the buttons directly to ensure the custom colors override default styles
        # Note: Set specific padding-left and padding-right to keep icon and text separated in RTL layout
        add_btn_qss = f"""
            PushButton {{
                background: {add_btn_bg};
                border: 1px solid transparent;
                border-radius: 11px;
                color: {c.accent};
                font-size: 12px;
                font-weight: 700;
                padding-left: 38px;
                padding-right: 14px;
            }}
            PushButton:hover {{
                border-color: {c.accent};
                background: rgba({r_val}, {g_val}, {b_val}, 0.15);
            }}
        """
        self._add_btn.setStyleSheet(add_btn_qss)

        # Style convert_btn as a PrimaryPushButton and style its border-radius
        # Note: using PrimaryPushButton selector to inherit default icon positioning/margins
        convert_btn_qss = f"""
            PrimaryPushButton {{
                background: {c.accent};
                color: #ffffff;
                border: none;
                border-radius: 11px;
                font-size: 13px;
                font-weight: 800;
                padding-left: 36px;
                padding-right: 14px;
            }}
            PrimaryPushButton:hover {{
                background: {dim};
            }}
        """
        self._convert_btn.setStyleSheet(convert_btn_qss)

        # Reset clear and output browse buttons to default styles to not affect them
        self._clear_btn.setStyleSheet("")
        self._out_dir_btn.setStyleSheet("")

        self.setStyleSheet(f"""
            ConverterPanel {{
                background: {c.bg};
            }}
            QLabel#converterHeaderLabel {{
                color: {c.text_primary};
                background: transparent;
                font-size: 22px;
                font-weight: 800;
            }}
            QLabel#converterSubLabel {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 12px;
            }}
            QFrame#converterHeaderFrame {{
                background: transparent;
                border: none;
            }}
            QScrollArea#converterScrollArea {{
                background: {c.surface};
                border: 2px dashed {c.border};
                border-radius: 16px;
            }}
            QWidget#converterListWidget {{
                background: {c.surface};
                border: none;
            }}
            QWidget#converterDropHintWidget {{
                background: transparent;
            }}
            QLabel#converterDropTitle {{
                color: {c.text_primary};
                background: transparent;
                font-size: 15px;
                font-weight: 800;
            }}
            QLabel#converterDropSubtitle {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 12px;
            }}
            QFrame#converterControlsFrame {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 14px;
            }}
            QFrame#converterControlsFrame QLabel {{
                color: {c.text_secondary};
                background: transparent;
                font-size: 12px;
            }}
            QFrame#converterDivider {{
                border: none;
                background-color: {c.border};
                width: 1px;
                max-width: 1px;
                min-width: 1px;
            }}
            QFrame#optionPill {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 17px;
            }}
            QFrame#optionPill:disabled {{
                background: {c.surface2};
                border-color: {c.border};
            }}
            QFrame#optionPill:hover:enabled {{
                border-color: {c.accent};
            }}
            QFrame#optionPill QLabel#converterFmtLabel, QFrame#optionPill QLabel#converterBrLabel {{
                color: {c.text_tertiary};
                background: transparent;
                font-size: 12px;
            }}
            QFrame#optionPill QLabel#converterFmtLabel:disabled, QFrame#optionPill QLabel#converterBrLabel:disabled {{
                color: {c.text_tertiary};
            }}
            QFrame#optionPill QLabel#converterFmtValueLabel, QFrame#optionPill QLabel#converterBrValueLabel {{
                color: {c.text_primary};
                background: transparent;
                font-size: 12px;
                font-weight: 600;
            }}
            QFrame#optionPill QLabel#converterFmtValueLabel:disabled, QFrame#optionPill QLabel#converterBrValueLabel:disabled {{
                color: {c.text_tertiary};
            }}
            PushButton#converterClearBtn, PushButton {{
                background: {c.surface2};
                border: 1px solid {c.border};
                border-radius: 11px;
                color: {c.text_secondary};
                font-size: 12px;
                padding-left: 34px;
                padding-right: 12px;
            }}
            PushButton#converterClearBtn:hover, PushButton:hover {{
                border-color: {c.accent};
                color: {c.text_secondary};
            }}
            QFrame#converterFileRow {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 10px;
            }}
            QFrame#converterFileRow QLabel#converterFileName {{
                color: {c.text_primary};
                background: transparent;
                font-size: 12px;
            }}
            QFrame#converterFileRow QLabel#converterFileDir {{
                color: {c.text_tertiary};
                background: transparent;
            }}
            QProgressBar#converterFileBar {{
                background: {c.border};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar#converterFileBar::chunk {{
                background: {c.accent};
                border-radius: 3px;
            }}
            QLabel#converterFileStatus {{
                background: transparent;
                font-size: 11px;
            }}
            QLabel#converterFileStatus[state="converting"] {{ color: {c.accent}; }}
            QLabel#converterFileStatus[state="done"] {{ color: {SUCCESS_COLOR}; }}
            QLabel#converterFileStatus[state="error"] {{ color: {ERROR_COLOR}; }}
            QLabel#converterFileStatus[state="skipped"] {{ color: {c.text_tertiary}; }}
            QLabel#converterFileStatus[state="cancelled"] {{ color: {c.text_secondary}; }}
            CaptionLabel#converterCountLabel {{
                color: {c.text_secondary};
                background: transparent;
                font-size: 12px;
            }}
            ToolButton#converterFileRemoveBtn {{
                color: {c.text_tertiary};
                background: transparent;
                border: none;
                font-size: 11px;
            }}
            ToolButton#converterFileRemoveBtn:hover {{
                color: {ERROR_COLOR};
            }}
        """)

    # ── Drag & Drop ────────────────────────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).suffix.lower() in _INPUT_EXTS:
                self._add_file(path)
        event.acceptProposedAction()

    # ── Handlers ───────────────────────────────────────────────────────────────

    def _on_add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(_INPUT_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            t("converter_select_files_dialog"),
            "",
            f"{t('converter_audio_files_filter')} ({exts});;{t('converter_all_files_filter')} (*)",
        )
        for p in paths:
            self._add_file(p)

    def _on_clear(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        for row in list(self._files.values()):
            self._list_layout.removeWidget(row)
            row.deleteLater()
        self._files.clear()
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setVisible(True)

    def _on_same_dir_toggle(self, checked: bool) -> None:
        self._out_dir_btn.setVisible(not checked)
        if checked:
            self._out_dir = None

    def _on_browse_out(self) -> None:
        path = QFileDialog.getExistingDirectory(self, t("converter_select_output_dialog"))
        if path:
            self._out_dir = path
            self._out_dir_btn.setText(Path(path).name[:30])

    def _on_fmt_changed(self, _fmt_text: str) -> None:
        fmt = self._fmt_combo.currentData() or "mp3"
        is_lossy = fmt in ("mp3", "m4a", "opus")
        self._br_group.setVisible(is_lossy)

    def _on_convert(self) -> None:
        if not self._files:
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            return

        fmt     = self._fmt_combo.currentData() or "mp3"
        bitrate = self._br_combo.currentData() or "320k"
        files   = list(self._files.keys())

        out_dir = None if self._same_dir_sw.isChecked() else self._out_dir

        if format_preservation_warnings(fmt) and not self._confirm_lossy_target():
            return

        policy = self._resolve_collision_policy(files, fmt, out_dir)
        if policy is None:
            return  # user aborted

        requests = [
            ConversionRequest(
                source=Path(f),
                output_format=fmt,
                bitrate=bitrate if fmt in ("mp3", "m4a", "opus") else None,
                output_dir=Path(out_dir) if out_dir else None,
                collision_policy=policy,
            )
            for f in files
        ]

        self._worker = ConvertWorker(requests, parent=self)
        self._worker.file_started.connect(self._on_file_started)
        self._worker.file_progress.connect(self._on_file_progress)
        self._worker.file_done.connect(self._on_file_done)
        self._worker.file_error.connect(self._on_file_error)
        self._worker.file_skipped.connect(self._on_file_skipped)
        self._worker.file_cancelled.connect(self._on_file_cancelled)
        self._worker.batch_progress.connect(self._on_batch_progress)
        self._worker.all_done.connect(self._on_all_done)
        self._convert_btn.setText(t("converter_cancel_btn"))
        from PySide6.QtGui import QColor
        self._convert_btn.setIcon(FluentIcon.CLOSE.icon(color=QColor("#ffffff")))
        self._worker.start()

    def _confirm_lossy_target(self) -> bool:
        """Warn before converting into a format that drops tags/artwork."""
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(t("converter_wav_warning_title"))
        box.setText(t("converter_wav_warning_msg"))
        cont = box.addButton(t("converter_continue_btn"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(t("converter_abort_btn"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is cont

    def _resolve_collision_policy(
        self, files: list[str], fmt: str, out_dir: Optional[str],
    ) -> Optional[CollisionPolicy]:
        """Pre-scan for existing destinations and ask the user once.

        Returns the chosen policy, UNIQUE when nothing collides (so a file
        appearing mid-conversion still can't be overwritten), or None when
        the user aborted.
        """
        collisions = 0
        for f in files:
            src = Path(f)
            dest_dir = Path(out_dir) if out_dir else src.parent
            dest = dest_dir / f"{src.stem}.{fmt}"
            try:
                if dest.exists() and not dest.samefile(src):
                    collisions += 1
            except OSError:
                collisions += 1
        if collisions == 0:
            return CollisionPolicy.UNIQUE

        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(t("converter_collision_title"))
        box.setText(t("converter_collision_msg", n=collisions))
        skip_btn = box.addButton(t("converter_collision_skip"), QMessageBox.ButtonRole.ActionRole)
        unique_btn = box.addButton(t("converter_collision_unique"), QMessageBox.ButtonRole.ActionRole)
        overwrite_btn = box.addButton(
            t("converter_collision_overwrite"), QMessageBox.ButtonRole.DestructiveRole,
        )
        abort_btn = box.addButton(t("converter_collision_abort"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(unique_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip_btn:
            return CollisionPolicy.SKIP
        if clicked is unique_btn:
            return CollisionPolicy.UNIQUE
        if clicked is overwrite_btn:
            return CollisionPolicy.OVERWRITE
        return None if clicked is abort_btn or clicked is None else None

    def _on_file_started(self, path: str) -> None:
        if path in self._files:
            self._files[path].set_converting()

    def _on_file_progress(self, path: str, ratio: float) -> None:
        if path in self._files:
            self._files[path].set_progress(ratio)

    def _on_file_done(self, in_path: str, _out_path: str) -> None:
        if in_path in self._files:
            self._files[in_path].set_done()

    def _on_file_error(self, in_path: str, msg: str) -> None:
        if in_path in self._files:
            self._files[in_path].set_error(msg)

    def _on_file_skipped(self, in_path: str, reason: str) -> None:
        if in_path in self._files:
            self._files[in_path].set_skipped(reason)

    def _on_file_cancelled(self, in_path: str) -> None:
        if in_path in self._files:
            self._files[in_path].set_cancelled()

    def _on_batch_progress(self, index: int, total: int) -> None:
        self._count_lbl.setText(t("converter_file_x_of_y", x=index, y=total))

    def _on_all_done(self, completed: int, failed: int, skipped: int, cancelled: int) -> None:
        self._convert_btn.setText(t("converter_convert_all_btn"))
        from PySide6.QtGui import QColor
        self._convert_btn.setIcon(FluentIcon.PLAY_SOLID.icon(color=QColor("#ffffff")))
        self._count_lbl.setText(t(
            "converter_summary",
            done=completed, failed=failed, skipped=skipped, cancelled=cancelled,
        ))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _add_file(self, path: str) -> None:
        if path in self._files:
            return
        row = _FileRow(path, parent=self._list_widget)
        row.remove_requested.connect(self._remove_file)
        self._files[path] = row
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._list_layout.addWidget(row)
        self._drop_hint.setVisible(False)

    def _remove_file(self, path: str) -> None:
        row = self._files.pop(path, None)
        if row:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        if not self._files:
            self._list_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._drop_hint.setVisible(True)

    def _make_group(
        self, label: QLabel, value_lbl: QLabel
    ) -> tuple[QFrame, "_AnchoredComboBox"]:
        group = _PillFrame(parent=self)
        group.setObjectName("optionPill")
        group.setFixedHeight(34)
        group.setCursor(Qt.CursorShape.PointingHandCursor)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        value_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        combo = _AnchoredComboBox(group, parent=self)
        combo.setFixedWidth(_ARROW_ONLY_WIDTH)
        group._target = combo

        lay = QHBoxLayout(group)
        lay.setContentsMargins(4, 0, 10, 0)
        lay.setSpacing(4)
        lay.addWidget(label)
        lay.addWidget(value_lbl)
        lay.addWidget(combo)
        return group, combo
