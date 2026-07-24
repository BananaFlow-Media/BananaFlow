"""
tests/test_batch_duplicate_dialog.py  –  Batched duplicate-file dialog
=========================================================================
The "ask" duplicate policy used to show one confirm() popup per duplicate
file found in a batch. This covers its replacement: ONE dialog for the
whole batch, listing every duplicate, with two outcomes (skip all /
replace all) rather than a per-file decision.

Headless (QT_QPA_PLATFORM=offscreen); skipped when PySide6 is missing.
Dialog widgets are exercised directly (never via .exec(), which blocks
until a button is clicked) — mirrors tests/test_update_prompt_dialog.py.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWidgets import QDialog
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


_ITEMS = [
    ("Track One", "/music/Track One.mp3"),
    ("Track Two", "/music/Track Two.mp3"),
    ("Track Three", "/music/Track Three.mp3"),
]


class TestAskBatchDuplicateAction:
    def test_empty_items_skips_without_showing_a_dialog(self, app):
        from ui.dialogs.batch_duplicate_dialog import ask_batch_duplicate_action

        assert ask_batch_duplicate_action(None, []) is True


@pytest.fixture
def restore_language(app):
    """Save/restore the active i18n language so this file's language
    switches never leak into other test files sharing the process."""
    from ui.i18n import current_language, set_language

    original = current_language()
    yield
    set_language(original)


# ── Subtitle wording: singular vs plural (pinned exact strings) ─────────────
# "1 file already exist..." is wrong English, and Hebrew changes both the
# noun (קובץ/קבצים) and the verb (קיים/קיימים) between singular and plural —
# a single "{n} file{plural}" template can't express either correctly, so
# _batch_duplicates_subtitle picks between two dedicated keys. Pinned here so
# the exact wording can't silently regress.

class TestBatchDuplicatesSubtitleWording:
    def test_singular_english(self, restore_language):
        from ui.i18n import set_language
        from ui.dialogs.batch_duplicate_dialog import _batch_duplicates_subtitle

        set_language("en")
        assert _batch_duplicates_subtitle(1) == (
            "1 file already exists in the destination folder."
        )

    def test_plural_english(self, restore_language):
        from ui.i18n import set_language
        from ui.dialogs.batch_duplicate_dialog import _batch_duplicates_subtitle

        set_language("en")
        assert _batch_duplicates_subtitle(40) == (
            "40 files already exist in the destination folder."
        )

    def test_singular_hebrew(self, restore_language):
        from ui.i18n import set_language
        from ui.dialogs.batch_duplicate_dialog import _batch_duplicates_subtitle

        set_language("he")
        assert _batch_duplicates_subtitle(1) == "קובץ אחד כבר קיים בתיקיית היעד."

    def test_plural_hebrew(self, restore_language):
        from ui.i18n import set_language
        from ui.dialogs.batch_duplicate_dialog import _batch_duplicates_subtitle

        set_language("he")
        assert _batch_duplicates_subtitle(40) == "40 קבצים כבר קיימים בתיקיית היעד."

    def test_dialog_widget_uses_singular_for_one_item(self, app, restore_language):
        """End-to-end: the actual dialog header, not just the helper
        function, must render the singular wording for a single duplicate."""
        from ui.i18n import set_language
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog
        from PySide6.QtWidgets import QLabel

        set_language("en")
        dlg = _BatchDuplicateDialog(_ITEMS[:1])
        try:
            subtitles = {
                w.text() for w in dlg.findChildren(QLabel)
                if w.objectName() == "dialogSubtitle"
            }
            assert "1 file already exists in the destination folder." in subtitles
        finally:
            dlg.deleteLater()

    def test_dialog_widget_uses_plural_for_many_items(self, app, restore_language):
        from ui.i18n import set_language
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog
        from PySide6.QtWidgets import QLabel

        set_language("en")
        dlg = _BatchDuplicateDialog(_ITEMS)
        try:
            subtitles = {
                w.text() for w in dlg.findChildren(QLabel)
                if w.objectName() == "dialogSubtitle"
            }
            assert "3 files already exist in the destination folder." in subtitles
        finally:
            dlg.deleteLater()


class TestBatchDuplicateDialogWidget:
    def test_defaults_to_skip_all_before_any_choice(self, app):
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog

        dlg = _BatchDuplicateDialog(_ITEMS)
        try:
            # Safe default: dismissing without an explicit button click
            # (Escape, window close) must never silently overwrite files.
            assert dlg.skip_all is True
        finally:
            dlg.deleteLater()

    def test_skip_all_button_sets_flag_and_accepts(self, app):
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog

        dlg = _BatchDuplicateDialog(_ITEMS)
        try:
            dlg._on_skip_all()
            assert dlg.skip_all is True
            assert dlg.result() == QDialog.DialogCode.Accepted
        finally:
            dlg.deleteLater()

    def test_replace_all_button_sets_flag_and_accepts(self, app):
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog

        dlg = _BatchDuplicateDialog(_ITEMS)
        try:
            dlg._on_replace_all()
            assert dlg.skip_all is False
            assert dlg.result() == QDialog.DialogCode.Accepted
        finally:
            dlg.deleteLater()

    def test_reject_without_a_choice_stays_at_the_safe_default(self, app):
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog

        dlg = _BatchDuplicateDialog(_ITEMS)
        try:
            dlg.reject()  # simulates Escape / window-close
            assert dlg.skip_all is True
        finally:
            dlg.deleteLater()

    def test_lists_every_item_title_and_path(self, app):
        """One dialog for the WHOLE batch: every duplicate must appear in
        it, not just the first one."""
        from ui.dialogs.batch_duplicate_dialog import _BatchDuplicateDialog
        from PySide6.QtWidgets import QLabel

        dlg = _BatchDuplicateDialog(_ITEMS)
        try:
            labels = {
                w.text() for w in dlg.findChildren(QLabel)
                if w.objectName() in ("batchDuplicateItemTitle", "batchDuplicateItemPath")
            }
            for title, path in _ITEMS:
                assert title in labels
                assert path in labels
        finally:
            dlg.deleteLater()
