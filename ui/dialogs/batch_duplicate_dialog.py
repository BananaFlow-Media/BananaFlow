"""
ui/dialogs/batch_duplicate_dialog.py  –  Batched duplicate-file confirmation
==============================================================================
The "ask" duplicate policy used to show one confirm() dialog per duplicate
file found while building a batch — a 40-track playlist with 40 existing
files meant 40 consecutive pop-ups. This shows exactly one dialog for the
whole batch instead: a list of every file that already exists, with two
outcomes for the whole set.

Zero download-logic here — this module only asks the question. The caller
(ui.controllers.download_controller.DownloadController.start_batch) decides
what "skip" and "replace" mean for job accounting.
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QScrollArea, QVBoxLayout, QWidget

from ui.dialogs.styled_dialog import (
    StyledDialog,
    add_header,
    make_button,
    make_footer,
    make_root_layout,
)
from ui.direction import force_ltr_label
from ui.i18n import t


def _batch_duplicates_subtitle(n: int) -> str:
    """"1 file already exists" vs "{n} files already exist" — English and
    Hebrew both change the noun AND the verb between singular and plural, so
    a single template with just a trailing {plural}="s" suffix can't produce
    either sentence correctly (e.g. "1 file already exist"). Two dedicated
    keys instead of one parametrised template."""
    if n == 1:
        return t("batch_duplicates_subtitle_one")
    return t("batch_duplicates_subtitle_many", n=n)


def ask_batch_duplicate_action(parent, items: Sequence[tuple[str, str]]) -> bool:
    """
    Show one consolidated dialog listing every duplicate found in the batch.

    Parameters
    ----------
    items : list of (title, existing_path) pairs — one per duplicate found.

    Returns
    -------
    True  — skip all: keep every existing file, mark it completed. Also the
            result when the dialog is dismissed any other way (closed,
            Escape) — the safe, non-destructive default, matching the old
            per-file dialog's "decline == skip" behavior.
    False — replace all: re-download and overwrite every one.
    """
    if not items:
        return True
    dlg = _BatchDuplicateDialog(items, parent)
    dlg.exec()
    return dlg.skip_all


class _BatchDuplicateDialog(StyledDialog):
    def __init__(self, items: Sequence[tuple[str, str]], parent=None) -> None:
        super().__init__(parent, minimum_size=(460, 320), resize_to=(560, 460))
        self.setWindowTitle(t("batch_duplicates_title"))
        # Safe default: closing the dialog without an explicit button click
        # (Escape, the window's X) must not silently overwrite files.
        self.skip_all = True

        n = len(items)
        root = make_root_layout(self)
        add_header(
            root,
            t("batch_duplicates_title"),
            _batch_duplicates_subtitle(n),
        )
        root.addWidget(self._make_list(items), stretch=1)

        skip_btn = make_button(t("batch_duplicates_skip_all_btn"), "cancel")
        skip_btn.clicked.connect(self._on_skip_all)
        replace_btn = make_button(t("batch_duplicates_replace_all_btn"), "danger")
        replace_btn.clicked.connect(self._on_replace_all)
        root.addWidget(make_footer(skip_btn, replace_btn))

    def _make_list(self, items: Sequence[tuple[str, str]]) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setObjectName("batchDuplicateScroll")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        for title, path in items:
            title_lbl = QLabel(title)
            title_lbl.setObjectName("batchDuplicateItemTitle")
            title_lbl.setWordWrap(True)
            layout.addWidget(title_lbl)

            path_lbl = QLabel(path)
            path_lbl.setObjectName("batchDuplicateItemPath")
            path_lbl.setWordWrap(True)
            force_ltr_label(path_lbl)  # paths are technical, always LTR
            layout.addWidget(path_lbl)

        layout.addStretch()
        scroll.setWidget(content)
        return scroll

    def _on_skip_all(self) -> None:
        self.skip_all = True
        self.accept()

    def _on_replace_all(self) -> None:
        self.skip_all = False
        self.accept()
