"""
tests/test_i18n_coverage.py  –  Full-app Hebrew/English coverage guard
========================================================================
Automates the i18n audit so it can never silently regress:

  1. Every t("key") literal anywhere in the app exists in BOTH the
     English and Hebrew tables (a missing key renders as the raw key
     name in the UI).
  2. The en/he tables stay in exact parity — a key added to one side
     must be added to the other.
  3. Core-produced diagnostic/error texts (core.youtube_doctor.
     DOCTOR_TEXTS_EN, error_handler.ERROR_TEXTS_EN) are injected into
     the "en" table verbatim AND have a real Hebrew translation — not
     just an English fallback.
  4. Hebrew translations must actually be Hebrew (or a deliberate
     technical term), never an accidental copy of the English text.
  5. Placeholders ({name}-style) used by the English template must all
     be safe in the Hebrew string: Hebrew may drop a placeholder (e.g.
     English plural suffixes don't apply) but must never reference one
     the caller doesn't supply.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from ui.i18n import TRANSLATIONS

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keys whose Hebrew deliberately equals the English text (product names,
# platform names, and untranslatable technical terms).
_INTENTIONALLY_ENGLISH_IN_HEBREW = {
    "platform_youtube", "platform_ytmusic", "platform_spotify",
    "spotify_group",
    "update_prompt_component_line",   # "{name}: {cur} → {new}" — no words
    "err_doctor_prefix",              # "YouTube Doctor: " — product name
    "tray_tooltip",                   # "BananaFlow" — product name
}


def _iter_project_py_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel_path = path.relative_to(REPO_ROOT)
        if any(part.startswith(".") for part in rel_path.parts):
            continue
        rel = rel_path.as_posix()
        if rel.startswith(("venv/", "build/", "dist/", "tests/", "tools/")):
            continue
        if "__pycache__" in rel or "site-packages" in rel:
            continue
        yield path


def _used_keys() -> dict[str, str]:
    """Every t('key') literal in app code -> one file that uses it."""
    used: dict[str, str] = {}
    pattern = re.compile(r"""\bt\(\s*["']([A-Za-z0-9_]+)["']""")
    for path in _iter_project_py_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            used.setdefault(match.group(1), path.relative_to(REPO_ROOT).as_posix())
    return used


def _placeholders(template: str) -> set[str]:
    return {
        field_name.split(".")[0].split("[")[0]
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


_HEBREW_RE = re.compile(r"[֐-׿]")


class TestKeyCoverage:

    def test_every_used_key_exists_in_english(self):
        missing = {
            key: where for key, where in _used_keys().items()
            if key not in TRANSLATIONS["en"]
        }
        assert not missing, f"t() keys missing from the English table: {missing}"

    def test_every_used_key_exists_in_hebrew(self):
        missing = {
            key: where for key, where in _used_keys().items()
            if key not in TRANSLATIONS["he"]
        }
        assert not missing, f"t() keys missing from the Hebrew table: {missing}"

    def test_en_he_tables_are_in_exact_parity(self):
        en, he = set(TRANSLATIONS["en"]), set(TRANSLATIONS["he"])
        assert en - he == set(), f"keys with no Hebrew translation: {sorted(en - he)}"
        assert he - en == set(), f"Hebrew-only keys (typo?): {sorted(he - en)}"


class TestCoreTextInjection:
    """core renders canonical English from its own template dicts; the
    "en" table must carry those exact templates so the UI's English
    rendering can never drift from what core produced."""

    def test_doctor_templates_injected_verbatim(self):
        from core.youtube_doctor import DOCTOR_TEXTS_EN
        for key, template in DOCTOR_TEXTS_EN.items():
            assert TRANSLATIONS["en"].get(key) == template, key

    def test_error_templates_injected_verbatim(self):
        from error_handler import ERROR_TEXTS_EN
        for key, template in ERROR_TEXTS_EN.items():
            assert TRANSLATIONS["en"].get(key) == template, key

    def test_preflight_templates_injected_verbatim(self):
        """Startup preflight warnings (FFmpeg/network/output-dir/cookies/
        Playwright) shown in the GUI MessageBox — must never regress to
        English-only the way the pre-refactor code was."""
        from error_handler import PREFLIGHT_TEXTS_EN
        for key, template in PREFLIGHT_TEXTS_EN.items():
            assert TRANSLATIONS["en"].get(key) == template, key

    def test_every_error_pattern_key_has_title_and_detail(self):
        import error_handler
        for _pattern, key, _severity, _code in error_handler._YTDLP_PATTERNS:
            assert f"{key}_title" in error_handler.ERROR_TEXTS_EN, key
            assert f"{key}_detail" in error_handler.ERROR_TEXTS_EN, key

    def test_classified_error_renders_in_hebrew_via_its_key(self):
        """The full chain: classify_error attaches a stable key; the UI
        renders t('{key}_title'/'{key}_detail') in the active language."""
        from error_handler import classify_error
        from ui.i18n import set_language, t

        err = classify_error(Exception("HTTP Error 403: Forbidden"))
        assert err.message_key == "err_403"
        set_language("he")
        try:
            title = t(f"{err.message_key}_title", **err.message_params)
            detail = t(f"{err.message_key}_detail", **err.message_params)
            assert _HEBREW_RE.search(title) and _HEBREW_RE.search(detail)
            assert "403" in title
        finally:
            set_language("en")

    def test_core_texts_have_real_hebrew(self):
        from core.youtube_doctor import DOCTOR_TEXTS_EN
        from error_handler import ERROR_TEXTS_EN, PREFLIGHT_TEXTS_EN
        problems = []
        for key in list(DOCTOR_TEXTS_EN) + list(ERROR_TEXTS_EN) + list(PREFLIGHT_TEXTS_EN):
            he = TRANSLATIONS["he"].get(key)
            if he is None:
                problems.append(f"{key}: missing")
            elif key not in _INTENTIONALLY_ENGLISH_IN_HEBREW and not _HEBREW_RE.search(he):
                problems.append(f"{key}: no Hebrew characters")
        assert not problems, problems


class TestHebrewQuality:

    def test_hebrew_is_not_a_copy_of_english(self):
        """A Hebrew entry identical to the English one is almost always
        an accidental paste — except for the allowlisted technical terms."""
        suspicious = []
        for key, en_text in TRANSLATIONS["en"].items():
            he_text = TRANSLATIONS["he"].get(key, "")
            if key in _INTENTIONALLY_ENGLISH_IN_HEBREW:
                continue
            # Only flag entries long enough to contain actual prose.
            if he_text == en_text and len(en_text) > 12 and _HEBREW_RE.search(en_text) is None:
                suspicious.append(key)
        assert not suspicious, f"Hebrew identical to English (untranslated?): {suspicious}"

    def test_hebrew_placeholders_are_a_subset_of_english(self):
        """Hebrew may drop placeholders (e.g. English '{plural}' suffixes)
        but must never *add* one — the caller only supplies the English
        template's parameters, and str.format raises on unknown fields."""
        problems = []
        for key, en_text in TRANSLATIONS["en"].items():
            he_text = TRANSLATIONS["he"].get(key, "")
            extra = _placeholders(he_text) - _placeholders(en_text)
            if extra:
                problems.append(f"{key}: Hebrew references unknown placeholders {sorted(extra)}")
        assert not problems, problems


class TestUpdateStoryConsistency:
    """The whole app must tell one story: updating BananaFlow is the primary
    path and component updates are advanced/source-mode support."""

    def test_component_check_is_labeled_advanced_in_both_languages(self):
        assert "(Advanced)" in TRANSLATIONS["en"]["check_component_updates_title"]
        assert "(מתקדם)" in TRANSLATIONS["he"]["check_component_updates_title"]

    def test_app_check_is_labeled_recommended_in_both_languages(self):
        assert "Recommended" in TRANSLATIONS["en"]["check_app_updates_desc"]
        assert "מומלץ" in TRANSLATIONS["he"]["check_app_updates_desc"]

    def test_frozen_note_never_tells_users_to_pip_install(self):
        for lang in ("en", "he"):
            assert "pip" not in TRANSLATIONS[lang]["update_prompt_frozen_note"].lower()
