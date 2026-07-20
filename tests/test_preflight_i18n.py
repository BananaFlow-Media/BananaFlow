"""
tests/test_preflight_i18n.py  –  Startup preflight warning localization
==========================================================================
run_preflight()'s user-facing MessageBox used to be built from raw
English strings baked directly into the warnings list — Hebrew users
would see English text at every startup warning. Each warning is now a
PreflightWarning(key, params); this file proves:

  * run_preflight() attaches the correct key/params for every failure.
  * PreflightWarning.render() (English, used by the CLI/log fallback)
    reproduces the original wording exactly — no behavior regression.
  * ui.i18n.render_preflight_warnings() renders the same warnings in
    Hebrew when the active language is Hebrew.
  * Every PREFLIGHT_TEXTS_EN key has a real Hebrew translation (also
    covered generically by test_i18n_coverage.py; asserted again here
    with concrete run_preflight() output for end-to-end confidence).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from error_handler import PREFLIGHT_TEXTS_EN, PreflightWarning, run_preflight

REPO_ROOT = Path(__file__).resolve().parent.parent


def _keys(warnings: list[PreflightWarning]) -> list[str]:
    return [w.key for w in warnings]


class TestRunPreflightWarningKeys:

    def test_all_ok_produces_no_warnings(self, tmp_path, monkeypatch):
        import error_handler
        monkeypatch.setattr(error_handler, "check_ffmpeg", lambda: True)
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: True)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: True)

        result = run_preflight(output_dir=str(tmp_path))
        assert result.warnings == []
        assert result.all_ok()

    def test_ffmpeg_missing_warning(self, monkeypatch):
        import error_handler
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: None)
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: True)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: True)

        result = run_preflight()
        assert "preflight_ffmpeg_missing" in _keys(result.warnings)
        assert not result.ffmpeg_ok
        assert not result.all_ok()

    def test_no_internet_warning(self, monkeypatch):
        import error_handler
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: False)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: True)

        result = run_preflight()
        assert "preflight_no_internet" in _keys(result.warnings)
        assert not result.network_ok

    def test_output_dir_not_writable_warning_carries_detail(self, monkeypatch):
        import error_handler
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: True)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: True)
        monkeypatch.setattr(
            error_handler, "check_output_dir_writable",
            lambda path: (False, "permission denied"),
        )

        result = run_preflight(output_dir="/some/protected/path")
        warning = next(w for w in result.warnings if w.key == "preflight_output_dir_not_writable")
        assert warning.params["detail"] == "permission denied"
        assert not result.output_dir_ok

    def test_cookies_invalid_warning_carries_detail(self, monkeypatch, tmp_path):
        import error_handler
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: True)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: True)
        monkeypatch.setattr(
            error_handler, "check_cookies_file_valid",
            lambda path: (False, "malformed Netscape header"),
        )

        cookies_path = tmp_path / "cookies.txt"
        cookies_path.write_text("not a real cookies file")
        result = run_preflight(cookies_file=str(cookies_path))
        warning = next(w for w in result.warnings if w.key == "preflight_cookies_invalid")
        assert warning.params["detail"] == "malformed Netscape header"
        assert not result.cookies_ok

    def test_no_cookies_file_configured_is_not_a_warning(self, monkeypatch):
        import error_handler
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: True)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: True)

        result = run_preflight(cookies_file="")
        assert "preflight_cookies_invalid" not in _keys(result.warnings)
        assert result.cookies_ok

    def test_playwright_missing_is_informational_warning(self, monkeypatch):
        import error_handler
        monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr("utils.paths.get_bundled_ffmpeg_dir", lambda: None)
        monkeypatch.setattr(error_handler, "probe_connectivity", lambda timeout=3.0: True)
        monkeypatch.setattr(error_handler, "check_playwright", lambda: False)

        result = run_preflight()
        assert "preflight_playwright_missing" in _keys(result.warnings)
        # Playwright is optional — must not block all_ok().
        assert result.all_ok()


class TestPreflightWarningRender:
    """PreflightWarning.render() (English) must reproduce the original
    wording exactly, so the CLI and the log fallback in main.py are
    unaffected by the key-based refactor."""

    def test_render_ffmpeg_missing(self):
        w = PreflightWarning("preflight_ffmpeg_missing")
        assert "FFmpeg was not found on your PATH" in w.render()
        assert "winget install Gyan.FFmpeg" in w.render()

    def test_render_with_detail_param(self):
        w = PreflightWarning("preflight_output_dir_not_writable", {"detail": "disk full"})
        rendered = w.render()
        assert "disk full" in rendered
        assert "not writable" in rendered

    def test_warning_text_joins_like_before(self):
        from error_handler import PreflightResult
        result = PreflightResult(
            ffmpeg_ok=False, network_ok=False, output_dir_ok=True,
            cookies_ok=True, playwright_ok=True,
            warnings=[
                PreflightWarning("preflight_ffmpeg_missing"),
                PreflightWarning("preflight_no_internet"),
            ],
            details=[],
        )
        text = result.warning_text()
        assert "\n\n" in text
        assert "FFmpeg was not found" in text
        assert "No internet connection" in text


class TestHebrewRendering:

    def test_render_preflight_warnings_in_hebrew(self):
        from ui.i18n import render_preflight_warnings, set_language

        set_language("he")
        try:
            text = render_preflight_warnings([
                PreflightWarning("preflight_ffmpeg_missing"),
                PreflightWarning("preflight_output_dir_not_writable", {"detail": "no permission"}),
            ])
        finally:
            set_language("en")

        assert any("֐" <= ch <= "׿" for ch in text), "no Hebrew characters rendered"
        assert "no permission" in text  # detail param preserved untranslated
        # Shell commands must survive translation verbatim.
        assert "winget install Gyan.FFmpeg" in text

    def test_render_preflight_warnings_in_english_matches_render(self):
        from ui.i18n import render_preflight_warnings

        warnings = [PreflightWarning("preflight_no_internet")]
        assert render_preflight_warnings(warnings) == warnings[0].render()

    def test_all_preflight_keys_have_hebrew(self):
        from ui.i18n import TRANSLATIONS
        for key in PREFLIGHT_TEXTS_EN:
            assert key in TRANSLATIONS["he"], key
            assert TRANSLATIONS["he"][key] != TRANSLATIONS["en"][key], (
                f"{key}: Hebrew identical to English"
            )

    def test_all_preflight_keys_injected_into_english_table_verbatim(self):
        from ui.i18n import TRANSLATIONS
        for key, template in PREFLIGHT_TEXTS_EN.items():
            assert TRANSLATIONS["en"][key] == template, key


class TestMainWiresTranslatedPreflightToTheMessageBox:
    """Static regression guard: main.py must build the startup MessageBox
    body from render_preflight_warnings(preflight.warnings) (translated),
    not preflight.warning_text() (hard-coded English) — the exact bug
    this refactor fixes. A source-text check is used because main()
    isn't practical to invoke directly (it owns the QApplication and the
    process's real startup sequence)."""

    def test_messagebox_uses_translated_renderer(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        # The MessageBox call site's body argument.
        assert "render_preflight_warnings(preflight.warnings)" in source
        assert "from ui.i18n import render_preflight_warnings" in source

    def test_english_only_fallback_still_used_only_for_the_log_line(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        # preflight.warning_text() (English) must only remain as the
        # logger.warning() fallback when the MessageBox itself can't be
        # shown — never as what the user actually sees.
        assert source.count("preflight.warning_text()") == 1
        idx = source.index("preflight.warning_text()")
        assert "logger.warning(" in source[max(0, idx - 200):idx]
