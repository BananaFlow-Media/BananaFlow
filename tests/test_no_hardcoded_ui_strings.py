"""
tests/test_no_hardcoded_ui_strings.py  –  Hardcoded-English regression pins
============================================================================
tests/test_i18n_coverage.py guarantees that every ``t("key")`` literal has
EN + HE translations — but it cannot see strings that never go through
``t()`` at all. A set of user-facing strings used to bypass i18n entirely
and stayed English in Hebrew mode (the per-download "Downloaded" toast,
the "Duplicate Detected" dialog, the clipboard toast, offline/online
status lines, the folder-picker caption, the Converter button reset, and
the search section headers).

This file pins each fixed site so the exact regressions cannot return,
and asserts the replacement keys exist so the fixes stay wired.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


# (file, forbidden literal, what it must use instead)
_FORBIDDEN = [
    ("ui/app_window.py", 'title="Downloaded"', 't("download_toast_title")'),
    ("ui/app_window.py", '"Track saved."', 't("download_toast_fallback")'),
    ("ui/app_window.py", 'title="Clipboard"', 't("clipboard_toast_title")'),
    ("ui/app_window.py", 'f"Detected: ', 't("clipboard_toast_detected", ...)'),
    ("ui/app_window.py", '"📡  No internet connection."', 't("status_offline")'),
    ("ui/app_window.py", '"✅  Internet connection restored."', 't("status_online")'),
    ("ui/app_window.py", 'headline = "Download failed"', 't("err_generic_title")'),
    ("ui/controllers/download_controller.py", '"Duplicate Detected"', 't("duplicate_detected_title")'),
    ("ui/controllers/download_controller.py", 'already exists:', 't("duplicate_detected_msg", ...)'),
    ("ui/panels/options_bar.py", '"Choose download folder"', 't("choose_download_folder")'),
    ("ui/panels/converter_panel.py", 'setText("Convert All")', 't("converter_convert_all_btn")'),
    ("ui/panels/search_panel.py", '"Tracks"', "search_filter_* keys via t()"),
    ("ui/panels/search_panel.py", '"Albums"', "search_filter_* keys via t()"),
]


@pytest.mark.parametrize("relative,literal,replacement", _FORBIDDEN,
                         ids=[f"{f}:{lit[:24]}" for f, lit, _ in _FORBIDDEN])
def test_fixed_hardcoded_string_does_not_return(relative, literal, replacement):
    source = _read(relative)
    assert literal not in source, (
        f"{relative} regressed to the hardcoded literal {literal!r} — "
        f"route it through the translation system ({replacement})"
    )


# Keys the fixes above rely on. Existence in BOTH tables is asserted here
# for wiring; quality (real Hebrew, placeholder safety, parity) is covered
# by tests/test_i18n_coverage.py.
_REQUIRED_KEYS = [
    "status_offline", "status_online",
    "status_batch_done", "status_batch_cancelled",
    "download_toast_title", "download_toast_fallback",
    "clipboard_toast_title", "clipboard_toast_detected",
    "duplicate_detected_title", "duplicate_detected_msg",
    "choose_download_folder",
]


def test_replacement_keys_exist_in_both_languages():
    from ui.i18n import TRANSLATIONS
    for key in _REQUIRED_KEYS:
        assert key in TRANSLATIONS["en"], f"missing EN key: {key}"
        assert key in TRANSLATIONS["he"], f"missing HE key: {key}"


def test_duplicate_dialog_message_carries_title_and_path():
    """The confirm dialog must show which file collides and where."""
    from ui.i18n import TRANSLATIONS
    for lang in ("en", "he"):
        template = TRANSLATIONS[lang]["duplicate_detected_msg"]
        assert "{title}" in template and "{path}" in template, (
            f"{lang}: duplicate_detected_msg must keep the {{title}} and "
            f"{{path}} placeholders the controller supplies"
        )


def test_batch_final_status_is_rendered_by_outcome_mapping():
    """The orchestrator's final status line is canonical English (it also
    serves the CLI); the GUI must re-render a translated summary keyed off
    the batch OUTCOME so Hebrew users never end a batch on English text.
    The mapping lives in AppWindow._on_all_downloads_finished."""
    source = _read("ui/app_window.py")
    assert 't("status_completed_summary"' in source
    assert 't("status_stopped_summary"' in source
    assert 't("status_completed_with_errors"' in source


def test_batch_outcome_distinguishes_pause_cancel_fatal():
    """The controller must map termination intent to explicit outcomes, not a
    single _was_cancelled boolean."""
    source = _read("ui/controllers/download_controller.py")
    assert "_was_cancelled" not in source, (
        "download_controller regressed to the collapsed _was_cancelled flag — "
        "use the BatchOutcome termination-intent model instead"
    )
    for token in ("PAUSED_BY_USER", "CANCELLED_BY_USER", "STOPPED_BY_FATAL_ERROR"):
        assert token in source, f"missing outcome mapping: {token}"


def test_status_strings_carry_no_emoji():
    """Downloads-page status/ETA strings must be emoji-free (the footer draws a
    themed status icon instead). Pins the Part-7 cleanup."""
    from ui.i18n import TRANSLATIONS
    emoji_keys = [
        "status_starting", "status_downloading_progress", "status_paused",
        "status_cancelling", "status_completed_summary",
        "status_completed_with_errors", "status_stopped_summary",
        "status_stopped_error", "status_offline", "status_online",
        "status_batch_done", "status_batch_cancelled",
        "fetching", "scraping", "fetch_done",
        "eta_calculating", "eta_about_left",
    ]
    forbidden = "🔍🕷✅⚠❌🔴🚫📡⬇▶⏸"
    for lang in ("en", "he"):
        for key in emoji_keys:
            text = TRANSLATIONS[lang][key]
            bad = [ch for ch in text if ch in forbidden]
            assert not bad, f"{lang}:{key} still contains emoji {bad}: {text!r}"


# ── Preexisting-breakdown summary wording (pinned) ───────────────────────────
# The first draft of this message said "{n} download{plural} completed
# (...)" — mislabelling the WHOLE batch as "downloads" even though some of
# it was a duplicate-skip that never downloaded anything. The fix anchors on
# completed/total instead. Pinned here so the wording can't silently regress.

def test_completed_with_preexisting_wording_pinned():
    from ui.i18n import TRANSLATIONS
    assert TRANSLATIONS["en"]["status_completed_with_preexisting"] == (
        "{completed} of {total} completed — {downloaded} downloaded, "
        "{preexisting} already existed."
    )
    assert TRANSLATIONS["he"]["status_completed_with_preexisting"] == (
        "{completed} מתוך {total} הושלמו — {downloaded} הורדו, "
        "{preexisting} כבר היו קיימים."
    )


def test_completed_with_preexisting_renders_expected_example():
    """The bug-report example (40 already-existing + 19 downloaded = 59/59)
    as a concrete, permanent regression pin."""
    from ui.i18n import TRANSLATIONS
    template = TRANSLATIONS["en"]["status_completed_with_preexisting"]
    rendered = template.format(completed=59, total=59, downloaded=19, preexisting=40)
    assert rendered == "59 of 59 completed — 19 downloaded, 40 already existed."


def test_completed_with_cancelled_summary_accounts_for_the_whole_batch():
    """Cancelling single tracks never cancels the batch, so such a run still
    ends as COMPLETED. Summarising only the successes ("9 downloads
    completed.") silently drops the tracks the user stopped and ends the
    batch short of its own total."""
    import re
    from ui.i18n import TRANSLATIONS

    for lang in ("en", "he"):
        assert "status_completed_with_cancelled" in TRANSLATIONS[lang]

    source = _read("ui/app_window.py")
    match = re.search(
        r't\(\s*"status_completed_with_cancelled",(.*?)\)', source, re.S,
    )
    assert match, "status_completed_with_cancelled call site not found"
    call_args = match.group(1)
    for name in ("completed=", "total=", "cancelled="):
        assert name in call_args, f"call site must supply {name}"


def test_completed_with_preexisting_call_site_uses_completed_and_total():
    """The call site must supply the real batch totals (completed=/total=),
    not a bare n= that conflates "completed" with "downloaded"."""
    import re
    source = _read("ui/app_window.py")
    match = re.search(
        r't\(\s*"status_completed_with_preexisting",(.*?)\)', source, re.S,
    )
    assert match, "status_completed_with_preexisting call site not found"
    call_args = match.group(1)
    assert "completed=" in call_args
    assert "total=" in call_args
    assert re.search(r'(?<![A-Za-z_])n=', call_args) is None, (
        "regressed to a bare n= — must use completed=/total= (see "
        "test_completed_with_preexisting_wording_pinned)"
    )
