"""
tests/test_youtube_doctor.py  –  Reliability-hardening phase 3
========================================================================
YouTube Doctor: local diagnostics only. These tests also double as a
guard against accidentally leaking cookie *values* into any message.
"""

from __future__ import annotations

import sys
from pathlib import PurePosixPath

import pytest

from core.youtube_doctor import (
    CookieDiagnostics,
    DoctorCheck,
    DoctorStatus,
    MIN_YT_DLP_VERSION,
    ProviderBackendDiagnostics,
    ProviderDetection,
    YoutubeDoctorReport,
    check_cookies,
    check_js_runtimes,
    check_po_token_provider,
    check_reliability_mode,
    check_yt_dlp_ejs,
    check_yt_dlp_version,
    detect_po_token_provider,
    run_youtube_doctor,
)


# ──────────────────────────────────────────────────────────────────────────────
# Bundled PO Token Provider reporting (full-stack readiness)
# ──────────────────────────────────────────────────────────────────────────────

class TestBundledProviderReporting:

    def test_bundled_provider_plugin_only_is_not_ready(self):
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [],
            _bundled_modules=lambda: ["getpot_bgutil"],
        )
        assert check.status == DoctorStatus.WARN
        assert len(detections) == 1
        assert detections[0].method == "bundled"
        assert detections[0].bundled is True
        assert "plugin-only" in check.message.lower()
        assert "not po ready" in check.message.lower()

    def test_bundled_provider_ready_requires_backend_health(self):
        backend = ProviderBackendDiagnostics(
            mode="script-deno",
            runtime_name="deno",
            backend_present=True,
            backend_healthy=True,
            version="1.3.1",
        )
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [],
            _bundled_modules=lambda: ["getpot_bgutil", "getpot_bgutil_script"],
            _backend_check=lambda: backend,
        )
        assert check.status == DoctorStatus.PASS
        assert len(detections) == 2
        assert "po token provider is ready" in check.message.lower()
        assert "script version 1.3.1" in check.message.lower()
        assert "official provider mechanism" in check.message.lower()
        assert "does not generate, store, or inject po tokens" in check.message.lower()

    def test_ready_requires_script_provider_module(self):
        backend = ProviderBackendDiagnostics(
            mode="script-deno",
            runtime_name="deno",
            backend_present=True,
            backend_healthy=True,
            version="1.3.1",
        )
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [],
            _bundled_modules=lambda: ["getpot_bgutil"],
            _backend_check=lambda: backend,
        )
        assert check.status == DoctorStatus.WARN
        assert len(detections) == 1
        assert "getpot_bgutil_script" in check.message
        assert "not po ready" in check.message.lower()

    def test_bundled_provider_backend_unhealthy_is_not_ready(self):
        backend = ProviderBackendDiagnostics(
            mode="script-deno",
            runtime_name="deno",
            backend_present=True,
            backend_healthy=False,
            reason="version check failed",
        )
        check, _detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [],
            _bundled_modules=lambda: ["getpot_bgutil"],
            _backend_check=lambda: backend,
        )
        assert check.status == DoctorStatus.WARN
        assert "failed its deno script health check" in check.message.lower()

    def test_bundled_provider_not_double_counted_with_namespace(self):
        # Same module found bundled *and* on the namespace path — reported once.
        detections = detect_po_token_provider(
            _find_spec=lambda name: _FakeSpec(["/fake/path"]) if name == "yt_dlp_plugins.extractor" else None,
            _iter_modules=lambda locations: [(None, "getpot_bgutil", False)],
            _distributions=lambda: [],
            _bundled_modules=lambda: ["getpot_bgutil"],
        )
        assert [d.method for d in detections] == ["bundled"]

    def test_no_bundled_provider_falls_through_to_warn(self):
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN
        assert detections == []


# ──────────────────────────────────────────────────────────────────────────────
# 1. yt-dlp version
# ──────────────────────────────────────────────────────────────────────────────

class TestYtDlpVersionCheck:

    def test_supported_version_passes(self):
        check = check_yt_dlp_version(installed_version=MIN_YT_DLP_VERSION)
        assert check.status == DoctorStatus.PASS

    def test_newer_version_passes(self):
        check = check_yt_dlp_version(installed_version="2026.7.4")
        assert check.status == DoctorStatus.PASS

    def test_too_old_version_warns(self):
        check = check_yt_dlp_version(installed_version="2026.3.13")
        assert check.status == DoctorStatus.WARN
        assert "2026.3.13" in check.message
        assert check.detail  # actionable recommendation present


# ──────────────────────────────────────────────────────────────────────────────
# 2. yt-dlp-ejs detection
# ──────────────────────────────────────────────────────────────────────────────

class TestYtDlpEjsCheck:

    def test_importable_passes(self):
        check = check_yt_dlp_ejs(_find_spec=lambda name: object())
        assert check.status == DoctorStatus.PASS

    def test_missing_warns(self):
        check = check_yt_dlp_ejs(_find_spec=lambda name: None)
        assert check.status == DoctorStatus.WARN


# ──────────────────────────────────────────────────────────────────────────────
# 3. JS runtime checks
# ──────────────────────────────────────────────────────────────────────────────

class TestJsRuntimeCheck:

    def _which_only(self, monkeypatch, available: dict[str, str]):
        from utils import yt_dlp_opts
        monkeypatch.setattr(yt_dlp_opts.shutil, "which", lambda name: available.get(name))

    def test_deno_available_selected(self, monkeypatch):
        self._which_only(monkeypatch, {"deno": "/usr/bin/deno"})
        check, statuses = check_js_runtimes()
        assert check.status == DoctorStatus.PASS
        assert "deno" in check.message
        deno = next(s for s in statuses if s.name == "deno")
        assert deno.found and deno.supported

    def test_node_22_plus_supported(self, monkeypatch):
        from utils import yt_dlp_opts
        self._which_only(monkeypatch, {"node": "/usr/bin/node"})
        monkeypatch.setattr(yt_dlp_opts, "_get_node_version_output", lambda p: "v22.11.0")
        check, statuses = check_js_runtimes()
        assert check.status == DoctorStatus.PASS
        node = next(s for s in statuses if s.name == "node")
        assert node.found and node.supported

    def test_node_below_22_unsupported(self, monkeypatch):
        from utils import yt_dlp_opts
        self._which_only(monkeypatch, {"node": "/usr/bin/node"})
        monkeypatch.setattr(yt_dlp_opts, "_get_node_version_output", lambda p: "v18.0.0")
        check, statuses = check_js_runtimes()
        # Node is the only thing present and it's unsupported -> no usable runtime.
        assert check.status == DoctorStatus.FAIL
        node = next(s for s in statuses if s.name == "node")
        assert node.found is True
        assert node.supported is False

    def test_quickjs_fallback(self, monkeypatch):
        self._which_only(monkeypatch, {"qjs": "/usr/bin/qjs"})
        check, statuses = check_js_runtimes()
        assert check.status == DoctorStatus.PASS
        assert "quickjs" in check.message

    def test_no_runtime_fails(self, monkeypatch):
        self._which_only(monkeypatch, {})
        check, statuses = check_js_runtimes()
        assert check.status == DoctorStatus.FAIL
        assert all(not s.found for s in statuses)

    def test_bun_is_never_selected(self, monkeypatch):
        self._which_only(monkeypatch, {"bun": "/usr/bin/bun"})
        check, statuses = check_js_runtimes()
        assert check.status == DoctorStatus.FAIL
        assert all(s.name != "bun" for s in statuses)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Cookies diagnostics
# ──────────────────────────────────────────────────────────────────────────────

_SECRET = "SECRET_VALUE_MARKER_DO_NOT_LEAK"


class TestCookiesCheck:

    def test_no_cookies_configured(self):
        check, diag = check_cookies()
        assert check.status == DoctorStatus.PASS
        assert diag.mode == "none"

    def test_missing_file_warns(self, tmp_path):
        check, diag = check_cookies(cookies_file=str(tmp_path / "nope.txt"))
        assert check.status == DoctorStatus.WARN
        assert diag.file_exists is False

    def test_empty_file_warns(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text("# Netscape HTTP Cookie File\n")
        check, diag = check_cookies(cookies_file=str(p))
        assert check.status == DoctorStatus.WARN
        assert diag.file_exists is True
        assert diag.file_non_empty is False

    def test_non_youtube_cookies_warns(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text(f".example.com\tTRUE\t/\tFALSE\t0\tsession\t{_SECRET}\n")
        check, diag = check_cookies(cookies_file=str(p))
        assert check.status == DoctorStatus.WARN
        assert diag.has_youtube_domain_cookies is False

    def test_youtube_cookies_present_without_login_warns(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text(f".youtube.com\tTRUE\t/\tFALSE\t0\tCONSENT\t{_SECRET}\n")
        check, diag = check_cookies(cookies_file=str(p))
        assert check.status == DoctorStatus.WARN
        assert diag.has_youtube_domain_cookies is True
        assert diag.has_likely_login_cookies is False
        assert "appear present" in check.message

    def test_likely_login_cookies_present_passes(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text(f".youtube.com\tTRUE\t/\tFALSE\t0\tLOGIN_INFO\t{_SECRET}\n")
        check, diag = check_cookies(cookies_file=str(p))
        assert check.status == DoctorStatus.PASS
        assert diag.has_likely_login_cookies is True
        assert "appear present" in check.message
        assert "valid" not in check.message.lower()

    def test_login_cookie_on_google_domain_detected(self, tmp_path):
        # SID/HSID-family cookies are typically scoped to .google.com, not
        # .youtube.com, in a real exported cookies.txt.
        p = tmp_path / "cookies.txt"
        p.write_text(
            f".youtube.com\tTRUE\t/\tFALSE\t0\tPREF\t{_SECRET}\n"
            f".google.com\tTRUE\t/\tFALSE\t0\tSID\t{_SECRET}\n"
        )
        check, diag = check_cookies(cookies_file=str(p))
        assert diag.has_likely_login_cookies is True
        assert check.status == DoctorStatus.PASS

    def test_browser_mode(self):
        check, diag = check_cookies(cookies_browser="chrome")
        assert diag.mode == "browser"
        assert diag.browser == "chrome"
        expected = DoctorStatus.WARN if sys.platform == "win32" else DoctorStatus.PASS
        assert check.status == expected

    def test_cookie_value_never_appears_in_message_or_detail(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text(f".youtube.com\tTRUE\t/\tFALSE\t0\tLOGIN_INFO\t{_SECRET}\n")
        check, diag = check_cookies(cookies_file=str(p))
        assert _SECRET not in check.message
        assert _SECRET not in check.detail
        assert _SECRET not in repr(diag)

    def test_full_path_never_appears_in_message(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text("# empty\n")
        check, diag = check_cookies(cookies_file=str(p))
        assert str(tmp_path) not in check.message
        assert diag.file_name == "cookies.txt"  # basename only


# ──────────────────────────────────────────────────────────────────────────────
# 5. PO Token Provider readiness
# ──────────────────────────────────────────────────────────────────────────────

class _FakeSpec:
    def __init__(self, locations):
        self.submodule_search_locations = locations


class _FakeDist:
    def __init__(self, name, files=None):
        self.metadata = {"Name": name}
        self.files = files or []


class TestPoTokenProviderCheck:

    def test_no_provider_warns_and_recommends_installation(self):
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN
        assert detections == []
        assert check.detail  # recommendation present
        assert "install" in check.detail.lower()
        assert "no po token provider plugin detected" in check.message.lower()

    def test_provider_detected_via_namespace_when_not_pip_installed(self):
        """No distribution owns the module (e.g. manually dropped into a
        yt-dlp plugin folder) — still detected, via the weaker signal."""
        check, detections = check_po_token_provider(
            _find_spec=lambda name: _FakeSpec(["/fake/path"]) if name == "yt_dlp_plugins.extractor" else None,
            _iter_modules=lambda locations: [(None, "getpot_bgutil", False)],
            _distributions=lambda: [],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN
        assert len(detections) == 1
        assert detections[0].method == "namespace"
        assert detections[0].heuristic is True
        assert "getpot_bgutil" in detections[0].module_name
        assert detections[0].distribution_name == ""
        assert "does not have the bundled provider backend ready" in check.message.lower()

    def test_provider_detected_via_distribution_record_files(self):
        """Strongest signal: the installed distribution's own RECORD
        metadata lists the getpot_* module — gives both names safely."""
        dist = _FakeDist(
            "bgutil-ytdlp-pot-provider",
            files=[PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil.py")],
        )
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [dist],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN
        assert len(detections) == 1
        assert detections[0].method == "distribution"
        assert detections[0].distribution_name == "bgutil-ytdlp-pot-provider"
        assert detections[0].module_name == "yt_dlp_plugins.extractor.getpot_bgutil"

    def test_provider_detected_via_distribution_name_fallback(self):
        """RECORD doesn't list the module path, but the distribution name
        still matches a known naming hint."""
        dist = _FakeDist("bgutil-ytdlp-pot-provider", files=[])
        check, detections = check_po_token_provider(
            _find_spec=lambda name: None,
            _iter_modules=lambda locations: [],
            _distributions=lambda: [dist],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN
        assert detections[0].method == "distribution"
        assert detections[0].distribution_name == "bgutil-ytdlp-pot-provider"
        assert detections[0].module_name == ""  # not confirmed via RECORD

    def test_unrelated_packages_and_submodules_ignored(self):
        dist = _FakeDist(
            "some-unrelated-package",
            files=[PurePosixPath("some_unrelated_package/__init__.py")],
        )
        check, detections = check_po_token_provider(
            _find_spec=lambda name: _FakeSpec(["/fake/path"]) if name == "yt_dlp_plugins.extractor" else None,
            _iter_modules=lambda locations: [(None, "some_other_extractor", False)],
            _distributions=lambda: [dist],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN
        assert detections == []

    def test_same_provider_not_double_counted(self):
        """Found via both distribution RECORD and namespace scan — must
        not be reported twice."""
        dist = _FakeDist(
            "bgutil-ytdlp-pot-provider",
            files=[PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil.py")],
        )
        check, detections = check_po_token_provider(
            _find_spec=lambda name: _FakeSpec(["/fake/path"]) if name == "yt_dlp_plugins.extractor" else None,
            _iter_modules=lambda locations: [(None, "getpot_bgutil", False)],
            _distributions=lambda: [dist],
            _bundled_modules=lambda: [],
        )
        assert len(detections) == 1
        assert detections[0].method == "distribution"

    def test_detection_never_imports_or_executes_provider_code(self, monkeypatch):
        """Detection must be pure metadata/filesystem inspection — never
        an actual import of the candidate module."""
        import importlib as _importlib

        def _boom(name, *args, **kwargs):
            raise AssertionError(f"detection must not import {name!r}")

        monkeypatch.setattr(_importlib, "import_module", _boom)

        dist = _FakeDist(
            "bgutil-ytdlp-pot-provider",
            files=[PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil.py")],
        )
        check, detections = check_po_token_provider(
            _find_spec=lambda name: _FakeSpec(["/fake/path"]) if name == "yt_dlp_plugins.extractor" else None,
            _iter_modules=lambda locations: [(None, "getpot_bgutil", False)],
            _distributions=lambda: [dist],
            _bundled_modules=lambda: [],
        )
        assert check.status == DoctorStatus.WARN  # completed without importing anything
        assert len(detections) == 1

    def test_provider_detection_is_always_labeled_heuristic(self):
        """Every ProviderDetection must be marked heuristic — there is no
        non-heuristic detection method in this phase."""
        detection = ProviderDetection(method="distribution", distribution_name="bgutil-ytdlp-pot-provider")
        assert detection.heuristic is True
        assert detection.method in ("distribution", "namespace")


# ──────────────────────────────────────────────────────────────────────────────
# 6. YouTube reliability mode status
# ──────────────────────────────────────────────────────────────────────────────

class TestReliabilityModeCheck:

    def test_conservative_passes(self):
        assert check_reliability_mode("conservative").status == DoctorStatus.PASS

    def test_fast_warns(self):
        assert check_reliability_mode("fast").status == DoctorStatus.WARN


# ──────────────────────────────────────────────────────────────────────────────
# 7. Final summary aggregation
# ──────────────────────────────────────────────────────────────────────────────

def _check(category, status, message="msg", detail=""):
    return DoctorCheck(category=category, status=status, message=message, detail=detail)


class TestReportAggregation:

    def test_healthy_public_download_setup(self):
        report = YoutubeDoctorReport(
            checks=[
                _check("yt_dlp_version", DoctorStatus.PASS),
                _check("yt_dlp_ejs", DoctorStatus.PASS),
                _check("js_runtime", DoctorStatus.PASS),
                _check("cookies", DoctorStatus.PASS),
                _check("po_token_provider", DoctorStatus.WARN, detail="Update or reinstall BananaFlow so bundled PO Token Provider files are present."),
                _check("youtube_reliability_mode", DoctorStatus.PASS),
            ],
            cookies=CookieDiagnostics(mode="none"),
            po_token_provider_detections=[],
        )
        assert report.ready_for_public_downloads == "yes"
        assert report.cookies_available_for_gated == "no"
        assert report.po_token_provider_available is False
        assert report.overall_status == DoctorStatus.WARN  # PO provider missing only

    def test_missing_runtime_setup(self):
        report = YoutubeDoctorReport(
            checks=[
                _check("yt_dlp_version", DoctorStatus.PASS),
                _check("yt_dlp_ejs", DoctorStatus.PASS),
                _check("js_runtime", DoctorStatus.FAIL, detail="Install Deno (recommended), Node 22+, or QuickJS."),
                _check("cookies", DoctorStatus.PASS),
                _check("po_token_provider", DoctorStatus.WARN),
                _check("youtube_reliability_mode", DoctorStatus.PASS),
            ],
            cookies=CookieDiagnostics(mode="none"),
        )
        assert report.ready_for_public_downloads == "no"
        assert report.overall_status == DoctorStatus.FAIL
        assert any("Deno" in a for a in report.recommended_actions())

    def test_cookies_present_but_po_provider_missing_setup(self):
        cookies_diag = CookieDiagnostics(
            mode="file", file_name="cookies.txt", file_exists=True, file_readable=True,
            file_non_empty=True, has_youtube_domain_cookies=True, has_likely_login_cookies=True,
        )
        report = YoutubeDoctorReport(
            checks=[
                _check("yt_dlp_version", DoctorStatus.PASS),
                _check("yt_dlp_ejs", DoctorStatus.PASS),
                _check("js_runtime", DoctorStatus.PASS),
                _check("cookies", DoctorStatus.PASS),
                _check("po_token_provider", DoctorStatus.WARN, detail="Update or reinstall BananaFlow so bundled PO Token Provider files are present."),
                _check("youtube_reliability_mode", DoctorStatus.PASS),
            ],
            cookies=cookies_diag,
            po_token_provider_detections=[],
        )
        assert report.cookies_available_for_gated == "yes"
        assert report.po_token_provider_available is False
        assert any("PO Token Provider" in a for a in report.recommended_actions())

    def test_summary_says_provider_ready_not_detected(self):
        report = YoutubeDoctorReport(
            checks=[
                _check("yt_dlp_version", DoctorStatus.PASS),
                _check("yt_dlp_ejs", DoctorStatus.PASS),
                _check("js_runtime", DoctorStatus.PASS),
                _check("cookies", DoctorStatus.PASS),
                _check("po_token_provider", DoctorStatus.PASS),
                _check("youtube_reliability_mode", DoctorStatus.PASS),
            ],
            cookies=CookieDiagnostics(mode="none"),
            po_token_provider_detections=[
                ProviderDetection(method="bundled", bundled=True),
            ],
            po_token_backend=ProviderBackendDiagnostics(
                backend_present=True,
                backend_healthy=True,
            ),
        )

        summary = report.summary_text()

        assert "PO Token Provider ready" in summary
        assert "PO Token Provider ready" in summary
        assert ": yes" in summary
        assert "PO Token Provider detected" not in summary
        assert "PO Token Provider available" not in summary

    def test_summary_says_not_ready_for_plugin_only(self):
        report = YoutubeDoctorReport(
            checks=[
                _check("yt_dlp_version", DoctorStatus.PASS),
                _check("yt_dlp_ejs", DoctorStatus.PASS),
                _check("js_runtime", DoctorStatus.PASS),
                _check("cookies", DoctorStatus.PASS),
                _check("po_token_provider", DoctorStatus.WARN),
                _check("youtube_reliability_mode", DoctorStatus.PASS),
            ],
            cookies=CookieDiagnostics(mode="none"),
            po_token_provider_detections=[
                ProviderDetection(method="bundled", bundled=True),
            ],
        )

        summary = report.summary_text()

        assert "PO Token Provider ready" in summary
        assert "PO Token Provider ready" in summary
        assert ": no" in summary


# ──────────────────────────────────────────────────────────────────────────────
# 8. Top-level run_youtube_doctor() sanity/integration
# ──────────────────────────────────────────────────────────────────────────────

class TestRunYoutubeDoctorIntegration:

    def test_returns_all_categories_and_never_raises(self):
        report = run_youtube_doctor()
        categories = {c.category for c in report.checks}
        assert categories == {
            "yt_dlp_version", "yt_dlp_ejs", "js_runtime",
            "cookies", "po_token_provider", "youtube_reliability_mode",
        }
        assert report.overall_status in (DoctorStatus.PASS, DoctorStatus.WARN, DoctorStatus.FAIL)

    def test_summary_text_mentions_every_category(self):
        report = run_youtube_doctor()
        text = report.summary_text()
        for cat in ("yt_dlp_version", "yt_dlp_ejs", "js_runtime", "cookies",
                    "po_token_provider", "youtube_reliability_mode"):
            assert cat in text

    def test_fast_mode_reflected_in_report(self):
        report = run_youtube_doctor(youtube_reliability_mode="fast")
        mode_check = next(c for c in report.checks if c.category == "youtube_reliability_mode")
        assert mode_check.status == DoctorStatus.WARN
