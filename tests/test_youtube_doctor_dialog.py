"""
tests/test_youtube_doctor_dialog.py  –  Reliability-hardening phase 4
========================================================================
build_report_text() is a pure, Qt-free function, so the dialog's content
is fully unit-testable without a QApplication. A separate, lightweight
Qt smoke test (tests/test_youtube_doctor_gui.py) covers actual widget
construction.
"""

from __future__ import annotations

from core.youtube_doctor import (
    CookieDiagnostics,
    DoctorCheck,
    DoctorStatus,
    YoutubeDoctorReport,
    run_youtube_doctor,
)
from ui.dialogs.youtube_doctor_dialog import (
    _CATEGORY_LABEL_KEYS,
    _YES_MAYBE_NO_KEYS,
    build_report_text,
)
from ui.i18n import TRANSLATIONS, t


_SECRET = "SECRET_COOKIE_VALUE_MUST_NEVER_APPEAR"


def _check(category, status, message="msg", detail=""):
    return DoctorCheck(category=category, status=status, message=message, detail=detail)


class TestBuildReportText:

    def test_all_categories_appear(self):
        report = run_youtube_doctor()
        text = build_report_text(report)
        for category, key in _CATEGORY_LABEL_KEYS.items():
            assert t(key) in text

    def test_pass_warn_fail_icons_present(self):
        report = YoutubeDoctorReport(
            checks=[
                _check("yt_dlp_version", DoctorStatus.PASS, "all good"),
                _check("po_token_provider", DoctorStatus.WARN, "missing provider"),
                _check("js_runtime", DoctorStatus.FAIL, "no runtime"),
            ],
        )
        text = build_report_text(report)
        assert "✅" in text and "all good" in text
        assert "⚠" in text and "missing provider" in text
        assert "❌" in text and "no runtime" in text

    def test_recommended_actions_included(self):
        report = YoutubeDoctorReport(
            checks=[
                _check(
                    "po_token_provider",
                    DoctorStatus.WARN,
                    "missing",
                    detail="Update or reinstall BananaFlow so the bundled provider files are present.",
                ),
            ],
        )
        text = build_report_text(report)
        assert t("youtube_doctor_actions_title") in text
        assert "Update or reinstall BananaFlow" in text

    def test_no_actions_section_when_nothing_to_recommend(self):
        report = YoutubeDoctorReport(checks=[_check("yt_dlp_version", DoctorStatus.PASS)])
        text = build_report_text(report)
        assert t("youtube_doctor_actions_title") not in text

    def test_renders_provider_readiness_details_when_ready(self):
        """Doctor's rendered dialog text must show the full provider stack
        readiness and the official provider path in use."""
        report = YoutubeDoctorReport(checks=[
            _check(
                "po_token_provider", DoctorStatus.PASS,
                message=(
                    "PO Token Provider is ready: bgutil plugin is available, "
                    "bundled Deno is selected, the Deno script backend is "
                    "present, and the backend health check passed (script "
                    "version 1.3.1). yt-dlp will use the official provider "
                    "mechanism with BananaFlow's bundled server_home; BananaFlow "
                    "does not generate, store, or inject PO Tokens."
                ),
            ),
        ])
        text = build_report_text(report)
        assert "PO Token Provider is ready" in text
        assert "bundled Deno" in text
        assert "official provider mechanism" in text
        assert "bundled server_home" in text
        assert "does not generate, store, or inject PO Tokens" in text

    def test_renders_no_provider_guidance_when_not_detected(self):
        report = YoutubeDoctorReport(checks=[
            _check(
                "po_token_provider", DoctorStatus.WARN,
                message=(
                    "No PO Token Provider plugin detected. Some YouTube "
                    "videos may fail with a PO Token error until a provider "
                    "is available for yt-dlp."
                ),
                detail=(
                    "Update or reinstall BananaFlow so the bundled PO Token Provider "
                    "files are present. For source installs, install the po-token "
                    "extra and run the provider staging helper."
                ),
            ),
        ])
        text = build_report_text(report)
        assert "No PO Token Provider plugin detected" in text
        assert "Update or reinstall BananaFlow" in text
        assert "source installs" in text

    def test_ready_cookies_po_summary_lines_present(self):
        report = run_youtube_doctor()
        text = build_report_text(report)
        assert t("youtube_doctor_ready_label") in text
        assert t("youtube_doctor_cookies_label") in text
        assert t("youtube_doctor_po_label") in text

    def test_cookie_value_never_appears(self, tmp_path):
        cookies_path = tmp_path / "cookies.txt"
        cookies_path.write_text(f".youtube.com\tTRUE\t/\tFALSE\t0\tLOGIN_INFO\t{_SECRET}\n")
        report = run_youtube_doctor(cookies_file=str(cookies_path))
        text = build_report_text(report)
        assert _SECRET not in text

    def test_full_path_never_appears(self, tmp_path):
        cookies_path = tmp_path / "cookies.txt"
        cookies_path.write_text("# empty\n")
        report = run_youtube_doctor(cookies_file=str(cookies_path))
        text = build_report_text(report)
        assert str(tmp_path) not in text


class TestI18nKeysComplete:
    """Every key build_report_text() references must exist in both
    languages — a missing key falls back silently to English (or the
    raw key), which would be an easy regression to miss otherwise."""

    def test_category_keys_exist_in_both_languages(self):
        for key in _CATEGORY_LABEL_KEYS.values():
            assert key in TRANSLATIONS["en"], key
            assert key in TRANSLATIONS["he"], key

    def test_yes_maybe_no_keys_exist_in_both_languages(self):
        for key in _YES_MAYBE_NO_KEYS.values():
            assert key in TRANSLATIONS["en"], key
            assert key in TRANSLATIONS["he"], key

    def test_dialog_and_card_keys_exist_in_both_languages(self):
        for key in (
            "youtube_doctor_group", "youtube_doctor_card_title", "youtube_doctor_card_desc",
            "youtube_doctor_run_btn", "youtube_doctor_dialog_title", "youtube_doctor_dialog_subtitle",
            "youtube_doctor_ready_label", "youtube_doctor_cookies_label", "youtube_doctor_po_label",
            "youtube_doctor_actions_title",
        ):
            assert key in TRANSLATIONS["en"], key
            assert key in TRANSLATIONS["he"], key
