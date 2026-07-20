"""
tests/test_hardcoded_string_scanner.py  –  Generic hardcoded-UI-text scanner
=============================================================================
tests/test_no_hardcoded_ui_strings.py pins specific, already-fixed
regressions (a denylist of exact literals from earlier phases). This
file is the complement Phase 4 asked for: a *generic* scanner over the
files the phase calls out (dialogs, panels, controllers, Tag Editor,
Converter, metadata flows) that catches the *next* hardcoded string
before it ships, not just re-checks the ones already found by hand.
It found one real, previously-unknown bug on first run (queue_panel.py
hardcoding the word "done" — see ALLOWLIST below for how a legitimate
exception would be recorded instead).

Scope and false-positive avoidance
-----------------------------------
* Only scans "sink" calls that are unambiguously user-facing text:
  .setText/.setWindowTitle/.setToolTip/.setPlaceholderText/.setStatusTip/
  .setWhatsThis, QLabel-family constructors, and the InfoBar/show_info/
  show_warning/show_error/MessageBox helpers.
* f-string interpolation expressions ({var}, {var.attr}) are stripped
  before checking for letters, so f"{pct}%" or f"{done}/{total}" alone
  (no literal words) do not trigger — only *actual literal words* inside
  the string do.
* Deliberately NOT scanned: combo-box .addItem() calls (format/codec/
  quality/browser-name values in this app are legitimately technical or
  proper nouns — Chrome, Firefox, mp3, 320k — scanning them would need a
  large allowlist for zero real signal) and any file/pattern already
  covered by test_no_hardcoded_ui_strings.py's specific-regression pins.
* A literal must contain a real word (2+ letters) to count — single
  punctuation/symbol strings (icons, "%", "…") are never flagged.

Deliberate exceptions go in ALLOWLIST below, one entry per (file, line)
pair, each with a one-line reason — never a blanket file exclusion.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Dialogs, panels, controllers, Tag Editor, Converter, metadata flows —
# the exact areas named in the Phase 4 brief. Deliberately not "all of
# ui/": the panels/dialogs/controllers not listed here are either tiny,
# already covered by test_no_hardcoded_ui_strings.py's specific pins, or
# dominated by legitimate technical combo values that would need a
# disproportionate allowlist for no real gain.
TARGET_FILES = [
    # metadata_editor_panel.py was split into ui/panels/metadata_editor/ —
    # scan the actual source files, not the compatibility re-export shim.
    "ui/panels/metadata_editor/panel.py",
    "ui/panels/metadata_editor/dialogs.py",
    "ui/panels/metadata_editor/explorer_view.py",
    "ui/panels/metadata_editor/tree.py",
    "ui/panels/metadata_editor/widgets.py",
    "ui/panels/metadata_editor/shared.py",
    "ui/controllers/metadata_controller.py",
    "ui/workers/metadata_worker.py",
    "ui/panels/converter_panel.py",
    "ui/dialogs/styled_dialog.py",
    "ui/panels/settings_panel.py",
    "ui/app_window.py",
    "ui/controllers/download_controller.py",
    "ui/controllers/fetch_controller.py",
    "ui/controllers/search_controller.py",
    "ui/panels/queue_panel.py",
    "ui/panels/history_panel.py",
    "ui/panels/search_panel.py",
    "ui/panels/options_bar.py",
    "ui/panels/url_bar.py",
    "ui/panels/status_bar.py",
    "ui/dialogs/youtube_doctor_dialog.py",
    "ui/dialogs/update_prompt_dialog.py",
    "ui/dialogs/cookie_auth_dialog.py",
]

SINK_RE = re.compile(
    r'\.(setText|setWindowTitle|setToolTip|setPlaceholderText|setStatusTip|setWhatsThis)'
    r'\s*\(\s*(f?)"([^"]*)"'
)
INFOBAR_RE = re.compile(
    r'(?:InfoBar\.\w+|show_info|show_warning|show_error|MessageBox)'
    r'\s*\(\s*(?:[\w.]+\s*=\s*)?(f?)"([^"]*)"'
)
LABEL_RE = re.compile(
    r'\b(?:QLabel|BodyLabel|CaptionLabel|SubtitleLabel|TitleLabel)\s*\(\s*(f?)"([^"]*)"'
)
BRACE_RE = re.compile(r"\{[^{}]*\}")

# (relative_file, line_number, reason) — a hit at exactly this file+line
# is expected and allowed. Nothing is currently allowlisted; add entries
# here (never a blanket file skip) if a future real exception is found.
ALLOWLIST: set[tuple[str, int]] = set()


def _literal_text(s: str) -> str:
    """Strip f-string {expr} interpolations, leaving only literal text."""
    return BRACE_RE.sub("", s)


def _looks_like_user_text(s: str) -> bool:
    stripped = _literal_text(s)
    return bool(re.search(r"[A-Za-z]{2,}", stripped)) or bool(
        re.search(r"[֐-׿]{2,}", stripped)
    )


def _scan_file(rel_path: str) -> list[tuple[int, str]]:
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    hits: list[tuple[int, str]] = []
    for pattern, group_index in ((SINK_RE, 3), (INFOBAR_RE, 2), (LABEL_RE, 2)):
        for m in pattern.finditer(text):
            literal = m.group(group_index)
            if _looks_like_user_text(literal):
                line_no = text[: m.start()].count("\n") + 1
                if (rel_path, line_no) not in ALLOWLIST:
                    hits.append((line_no, literal))
    return hits


def test_no_new_hardcoded_user_facing_strings():
    all_hits = {}
    for rel_path in TARGET_FILES:
        hits = _scan_file(rel_path)
        if hits:
            all_hits[rel_path] = hits

    if all_hits:
        lines = []
        for rel_path, hits in all_hits.items():
            for line_no, literal in hits:
                lines.append(f"  {rel_path}:{line_no}: {literal!r}")
        pytest_message = (
            "Found hardcoded user-facing text that bypasses t():\n"
            + "\n".join(lines)
            + "\n\nRoute it through ui.i18n.t() with an EN+HE key, or add "
            "(file, line) to ALLOWLIST in this test with a one-line reason "
            "if it's genuinely a technical constant."
        )
        raise AssertionError(pytest_message)


def test_target_files_all_exist():
    """Guard the scanner's own file list against silent typos/renames —
    a misspelled path here would just mean that file is never scanned."""
    missing = [f for f in TARGET_FILES if not (REPO_ROOT / f).exists()]
    assert not missing, f"TARGET_FILES lists nonexistent paths: {missing}"


def test_queue_stats_done_uses_translation():
    """Regression pin for the one real bug this scanner found on its
    first run: the queue panel's '{done}/{total} done' status hardcoded
    the English word "done" even in Hebrew mode."""
    source = (REPO_ROOT / "ui/panels/queue_panel.py").read_text(encoding="utf-8")
    assert 'f"· {done}/{total} done"' not in source
    assert 't("queue_stats_done"' in source

    from ui.i18n import TRANSLATIONS
    assert "queue_stats_done" in TRANSLATIONS["en"]
    assert "queue_stats_done" in TRANSLATIONS["he"]
    assert TRANSLATIONS["en"]["queue_stats_done"] != TRANSLATIONS["he"]["queue_stats_done"]
