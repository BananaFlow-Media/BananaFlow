"""
ui/dialogs/duplicate_files_dialog.py  –  Duplicate-file conflict resolution dialog
====================================================================================
Presents every duplicate group as a two-column card grid (Windows Explorer style).

Layout per group
----------------
  right col (col 0, index 0, 2, 4…)   |   left col (col 1, index 1, 3, 5…)
  ──────────────────────────────────────────────────────────────────────────
  [ ☑  file_A.mp3 ]                   |   [ ☑  file_B.mp3 ]
    Albums\\Artist\\file_A.mp3             Albums\\ArtistCopy\\file_B.mp3
    4.2 MB  |  12/05/2025 14:30            4.2 MB  |  10/03/2025 09:15
  ──────────────────────────────────────────────────────────────────────────
  [ ☑  file_C.mp3 ]                   |   (empty if group has odd count)
    ...

Bulk toolbar (top)
------------------
  ✅ שניהם         → check ALL files across all groups
  ➡ עמודה ימנית   → keep only even-index files (right column) per group
  ⬅ עמודה שמאלית  → keep only odd-index  files (left  column) per group

Paths displayed
---------------
  Relative to the root_folder passed in (mirrors what the Tag Editor loaded).
  Fallback to absolute path if the file is outside the root.

Usage
-----
    dlg = DuplicateFilesDialog(groups, elapsed, strategy, root_folder, parent=self)
    if dlg.exec() == QDialog.Accepted:
        paths = dlg.files_to_delete   # list[Path]
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.direction import force_ltr_label, isolate_number
from ui.dialogs.styled_dialog import CheckMarkBox, StyledDialog, confirm, set_button_role, show_info
from ui.i18n import t
from ui.theme_manager import get_colors


def _dim_accent(hex_color: str, factor: float = 0.85) -> str:
    """Return a darkened/dimmed variant of a hex color for hover states."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r = max(0, int(int(h[0:2], 16) * factor))
    g = max(0, int(int(h[2:4], 16) * factor))
    b = max(0, int(int(h[4:6], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return isolate_number(f"{n_bytes:.1f} {unit}")
        n_bytes /= 1024  # type: ignore[assignment]
    return isolate_number(f"{n_bytes:.1f} TB")


def _fmt_date(ts: float) -> str:
    from utils.time_format import display_timestamp
    return isolate_number(display_timestamp(ts))


def _rel_path(path: Path, root: Path | None) -> str:
    if root is None:
        return str(path)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ── Dialog ─────────────────────────────────────────────────────────────────────

class DuplicateFilesDialog(StyledDialog):
    """
    Two-column card-grid conflict dialog for duplicate audio files.

    Parameters
    ----------
    groups      : {key: [Path, …], …}  — output from DuplicateDetectorWorker
    elapsed     : scan duration in seconds
    strategy    : "md5" or "size"
    root_folder : the Tag Editor's currently loaded root (for relative paths)
    """

    def __init__(
        self,
        groups:      dict,
        elapsed:     float,
        strategy:    str = "md5",
        root_folder: Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent, minimum_size=(880, 560), resize_to=(1040, 720))
        self._result = groups if hasattr(groups, "groups") else None
        self._groups = ({group.id: [Path(path) for path in group.paths] for group in groups.groups}
                        if self._result is not None else groups)
        self._elapsed  = elapsed
        self._strategy = strategy
        self._root     = root_folder
        self._files_to_delete: list[Path] = []

        # _group_cbs[group_idx] = [QCheckBox for file_0, file_1, …]
        self._group_cbs: list[list[QCheckBox]] = []

        self.setWindowTitle(t("duplicates_manage_title"))
        # Layout direction is inherited from the app (RTL for Hebrew, LTR for
        # English). Path and filename labels inside cards are explicitly
        # forced LTR below via force_ltr_label so they always read correctly.
        self.setModal(True)
        self._build()

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def files_to_delete(self) -> list[Path]:
        """Paths whose checkboxes were UNCHECKED when the user confirmed."""
        return self._files_to_delete

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        c = get_colors()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        layout.addWidget(self._make_header(c))
        if self._result is not None and self._result.partial:
            warning = QLabel(t("duplicates_partial_warning", n=len(self._result.warnings)))
            warning.setObjectName("duplicatePartialWarning")
            warning.setWordWrap(True)
            layout.addWidget(warning)
            for path, _stage, code in self._result.warnings:
                detail = QLabel(t("duplicates_partial_detail", path=Path(path).name,
                                 reason=t(f"{code}")))
                detail.setObjectName("duplicateWarningDetail")
                force_ltr_label(detail)
                layout.addWidget(detail)
        layout.addWidget(self._make_hint(c))
        layout.addLayout(self._make_bulk_toolbar())
        layout.addWidget(self._make_scroll_area(), stretch=1)
        layout.addLayout(self._make_footer())

    # ── Section builders ───────────────────────────────────────────────────────

    def _make_header(self, c) -> QLabel:
        n_groups = len(self._groups)
        n_files  = sum(len(v) for v in self._groups.values())
        strat = (
            t("duplicates_strategy_size")
            if self._strategy == "size"
            else t("duplicates_strategy_md5")
        )
        lbl = QLabel(t(
            "duplicates_header",
            n_files=n_files,
            n_groups=n_groups,
            strat=strat,
            elapsed=self._elapsed,
        ))
        lbl.setObjectName("dialogHeader")
        return lbl

    def _make_hint(self, c) -> QLabel:
        lbl = QLabel(t("duplicates_hint"))
        lbl.setObjectName("dialogHint")
        return lbl

    def _make_bulk_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self._both_btn = QPushButton(t("duplicates_keep_all_btn"))
        set_button_role(self._both_btn, "secondary")
        self._both_btn.setFixedHeight(30)
        self._both_btn.setToolTip(t("duplicates_keep_all_tooltip"))
        self._both_btn.clicked.connect(self._on_keep_both)
        row.addWidget(self._both_btn)

        row.addStretch()
        return row

    def _make_scroll_area(self) -> QScrollArea:
        c = get_colors()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(20)

        hdr_font = QFont()
        hdr_font.setBold(True)
        hdr_font.setPointSize(10)

        for group_idx, (_key, paths) in enumerate(self._groups.items(), start=1):
            # ── Group header ─────────────────────────────────────────────────
            grp_lbl = QLabel(t("duplicates_group_label", n=group_idx, count=len(paths)))
            grp_lbl.setFont(hdr_font)
            grp_lbl.setObjectName("duplicateGroupLabel")
            content_layout.addWidget(grp_lbl)

            if self._result is not None:
                group = self._result.groups[group_idx - 1]
                confidence_key = ("duplicates_confidence_possible" if group.confidence.value == "possible"
                                  else "duplicates_confidence_same_audio" if group.evidence.value == "audio_payload"
                                  else "duplicates_confidence_same_file")
                evidence = QLabel(t(confidence_key))
                evidence.setObjectName("duplicateEvidenceLabel")
                evidence.setWordWrap(True)
                content_layout.addWidget(evidence)

            # ── 2-column card grid ───────────────────────────────────────────
            grid_widget = QWidget()
            grid = QGridLayout(grid_widget)
            grid.setSpacing(10)
            grid.setContentsMargins(0, 4, 0, 0)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)

            group_cbs: list[QCheckBox] = []
            for file_idx, path in enumerate(paths):
                row_num = file_idx // 2
                col_num = file_idx % 2
                card, cb = self._make_file_card(path, c)
                grid.addWidget(card, row_num, col_num)
                group_cbs.append(cb)

            self._group_cbs.append(group_cbs)
            content_layout.addWidget(grid_widget)

        content_layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def _make_file_card(self, path: Path, c) -> tuple[QFrame, QCheckBox]:
        card = QFrame()
        card.setObjectName("fileCard")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(12, 10, 12, 10)
        vbox.setSpacing(5)

        # Row 1: checkbox + filename (bold)
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        cb = CheckMarkBox()
        cb.setChecked(True)  # Checked = KEEP
        cb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        top_row.addWidget(cb)

        name_font = QFont()
        name_font.setBold(True)
        name_lbl = QLabel(path.name)
        name_lbl.setFont(name_font)
        name_lbl.setObjectName("fileCardName")
        name_lbl.setWordWrap(True)
        force_ltr_label(name_lbl)  # filenames are technical, always LTR
        top_row.addWidget(name_lbl, stretch=1)
        vbox.addLayout(top_row)

        # Row 2: relative path (shown like Windows Explorer, relative to root)
        rel = _rel_path(path, self._root)
        path_lbl = QLabel(rel)
        path_lbl.setObjectName("fileCardPath")
        path_lbl.setWordWrap(True)
        path_lbl.setToolTip(str(path))  # full absolute path in tooltip
        force_ltr_label(path_lbl)  # paths are technical, always LTR
        vbox.addWidget(path_lbl)

        # Row 3: size + modification date
        try:
            st       = path.stat()
            meta_str = f"{_fmt_size(st.st_size)}   |   {_fmt_date(st.st_mtime)}"
        except OSError:
            meta_str = "—"
        meta_lbl = QLabel(meta_str)
        meta_lbl.setObjectName("fileCardMeta")
        vbox.addWidget(meta_lbl)

        return card, cb

    def _make_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()

        self._cancel_btn = QPushButton(t("cancel_btn"))
        set_button_role(self._cancel_btn, "cancel")
        self._cancel_btn.setFixedHeight(36)
        self._cancel_btn.setMinimumWidth(90)
        self._cancel_btn.setObjectName("cancelBtn")
        self._cancel_btn.clicked.connect(self.reject)
        row.addWidget(self._cancel_btn)

        self._apply_btn = QPushButton(t("duplicates_apply_btn"))
        set_button_role(self._apply_btn, "primary", default=True)
        self._apply_btn.setFixedHeight(36)
        self._apply_btn.setMinimumWidth(170)
        self._apply_btn.clicked.connect(self._on_apply)
        row.addWidget(self._apply_btn)

        return row

    # ── Bulk column operations ─────────────────────────────────────────────────

    def _on_keep_both(self) -> None:
        """Check all checkboxes across every group."""
        for group_cbs in self._group_cbs:
            for cb in group_cbs:
                cb.setChecked(True)

    # ── Apply & confirm ────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        to_delete = self._collect_unchecked()
        if not to_delete:
            show_info(
                self,
                t("duplicates_nothing_title"),
                t("duplicates_nothing_msg"),
            )
            return

        possible = (self._result is not None
                    and any(group.confidence.value == "possible" for group in self._result.groups))
        if confirm(
            self,
            t("duplicates_confirm_title"),
            t("duplicates_possible_confirm_msg" if possible else "duplicates_confirm_msg", n=len(to_delete)),
            accept_text=t("duplicates_confirm_yes"),
            cancel_text=t("duplicates_confirm_no"),
            danger=True,
        ):
            self._files_to_delete = to_delete
            self.accept()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _collect_unchecked(self) -> list[Path]:
        result: list[Path] = []
        for group_idx, (_key, paths) in enumerate(self._groups.items()):
            cbs = self._group_cbs[group_idx] if group_idx < len(self._group_cbs) else []
            for file_idx, path in enumerate(paths):
                if file_idx < len(cbs) and not cbs[file_idx].isChecked():
                    result.append(path)
        return result
