"""
tests/test_hardcoded_string_audit.py  –  Hardcoded English UI string guard
========================================================================
Issue #42: CONTRIBUTING.md requires every user-facing string to go
through ui.i18n.t() (see "RTL, Accessibility and Translations"), and
tests/test_i18n_coverage.py already guards every t("key") literal, but
nothing previously caught a string literal passed *directly* to a
Qt text-setting call, bypassing t() entirely. This walks ui/ with the
AST (not a regex) looking for calls to common text-setting methods
(setText, setToolTip, addItem, QMessageBox.information, ...) whose
visible-text argument is a plain string literal that looks like English
prose, and fails if any are found outside the reviewed allowlist below.

The allowlist is keyed by (relative file path, line number) — deliberately
precise, not by string value — so a new violation that happens to reuse
an allowlisted word (e.g. a fresh "English" elsewhere) still fails and
must be reviewed, not silently waved through.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Qt/QFluentWidgets methods whose first positional argument is
# user-visible text.
_TEXT_METHODS = {
    "setText", "setToolTip", "setPlaceholderText", "setWindowTitle",
    "setStatusTip", "setWhatsThis", "addItem", "setTitle",
    "setPlainText", "setHtml", "addTab", "information", "warning",
    "critical", "question", "setAccessibleName", "setAccessibleDescription",
    "showMessage",
}

# (file, line, text) reviewed and accepted as intentionally untranslated:
# playlist/report format acronyms and a language picker's endonyms, the
# same category CONTRIBUTING.md / test_i18n_coverage.py's
# _INTENTIONALLY_ENGLISH_IN_HEBREW already treats as not needing
# translation (product names, platform names, technical terms).
_ALLOWED = {
    ("ui/panels/metadata_editor/io_dialog.py", 400, "HTML"),
    ("ui/panels/metadata_editor/io_dialog.py", 400, "CSV"),
    ("ui/panels/metadata_editor/io_dialog.py", 402, "English"),
    ("ui/panels/metadata_editor/io_dialog.py", 468, "M3U8"),
    ("ui/panels/metadata_editor/io_dialog.py", 468, "M3U"),
}


def _looks_like_prose(value: str) -> bool:
    return bool(value) and value[0].isascii() and value[0].isalpha()


def _scan_ui_for_hardcoded_text() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "ui").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name not in _TEXT_METHODS:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if _looks_like_prose(arg.value):
                    hits.append((rel, node.lineno, arg.value))
    return hits


def test_no_new_hardcoded_english_strings_in_ui_widgets():
    hits = _scan_ui_for_hardcoded_text()
    unreviewed = [h for h in hits if h not in _ALLOWED]
    assert not unreviewed, (
        "found user-facing string literal(s) bypassing ui.i18n.t() — wrap "
        "them in t() and add a translation key, or if genuinely "
        "untranslatable (a technical acronym/proper noun), add the exact "
        "(file, line, text) to _ALLOWED here with a one-line reason: "
        f"{unreviewed}"
    )


def test_allowlist_entries_still_exist_verbatim():
    """Catches a stale allowlist entry (moved/changed/removed line) just
    as loudly as a new violation — an allowlist nothing can ever fail
    against isn't actually guarding anything."""
    hits = set(_scan_ui_for_hardcoded_text())
    stale = _ALLOWED - hits
    assert not stale, f"allowlist entries no longer found verbatim in ui/: {stale}"
