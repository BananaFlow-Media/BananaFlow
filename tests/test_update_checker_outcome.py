"""
tests/test_update_checker_outcome.py  –  check_detailed() outcomes
=====================================================================
check() collapses "up to date" and "check failed" into None (right for
the silent startup path). The Settings buttons need the difference, so
check_detailed() must classify every case correctly. All network access
is stubbed via UpdateChecker._get_json.
"""

from __future__ import annotations

import httpx
import pytest

from core.update_checker import CURRENT_VERSION, UpdateChecker


def _release_json(
    tag: str = "v99.0.0",
    *,
    draft: bool = False,
    prerelease: bool = False,
    assets: list | None = None,
) -> dict:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/x/y/releases/tag/{tag}",
        "body": "## Notes\nStuff changed.",
        "published_at": "2026-07-01T00:00:00Z",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets or [],
    }


def _checker_returning(payload) -> UpdateChecker:
    checker = UpdateChecker()
    checker._get_json = lambda url: payload      # type: ignore[method-assign]
    return checker


def _checker_raising(exc: Exception) -> UpdateChecker:
    def boom(url):
        raise exc
    checker = UpdateChecker()
    checker._get_json = boom                     # type: ignore[method-assign]
    return checker


class TestCheckDetailed:

    def test_newer_release_is_update_available(self):
        outcome = _checker_returning(_release_json("v99.0.0")).check_detailed()
        assert outcome.status == "update_available"
        assert outcome.update_available
        assert outcome.release is not None
        assert outcome.release.version == "99.0.0"

    def test_same_version_is_up_to_date(self):
        outcome = _checker_returning(_release_json(f"v{CURRENT_VERSION}")).check_detailed()
        assert outcome.status == "up_to_date"
        assert not outcome.update_available
        assert outcome.release is None

    def test_older_release_is_up_to_date(self):
        outcome = _checker_returning(_release_json("v0.0.1")).check_detailed()
        assert outcome.status == "up_to_date"

    def test_draft_release_is_not_an_update(self):
        outcome = _checker_returning(_release_json("v99.0.0", draft=True)).check_detailed()
        assert outcome.status == "up_to_date"

    def test_prerelease_skipped_by_default(self):
        outcome = _checker_returning(
            _release_json("v99.0.0", prerelease=True)
        ).check_detailed()
        assert outcome.status == "up_to_date"

    def test_network_failure_is_error(self):
        outcome = _checker_raising(httpx.ConnectError("offline")).check_detailed()
        assert outcome.status == "error"
        assert not outcome.update_available

    def test_non_dict_response_is_error(self):
        outcome = _checker_returning(["unexpected", "list"]).check_detailed()
        assert outcome.status == "error"

    def test_missing_tag_is_error_not_up_to_date(self):
        outcome = _checker_returning(_release_json("")).check_detailed()
        assert outcome.status == "error"

    def test_check_delegates_and_never_raises(self):
        assert _checker_raising(RuntimeError("boom")).check() is None
        release = _checker_returning(_release_json("v99.0.0")).check()
        assert release is not None and release.version == "99.0.0"


class TestWorkerFlags:
    """UpdateWorker must keep the canonical repo defaults (guarded in
    test_p0_gates too) and honour its check_app / check_components flags."""

    def test_worker_signature_defaults(self):
        pytest.importorskip("PySide6")
        import inspect
        from ui.workers.update_worker import UpdateWorker

        sig = inspect.signature(UpdateWorker.__init__)
        assert sig.parameters["repo_owner"].default == "BananaFlow-Media"
        assert sig.parameters["repo_name"].default == "BananaFlow"
        assert sig.parameters["check_app"].default is True
        assert sig.parameters["check_components"].default is False


class TestReleaseUrlIsNotBlindlyTrusted:
    """A release URL is untrusted input on its way to the Windows shell.

    ui/dialogs/update_prompt_dialog.py hands ReleaseInfo.release_url straight
    to QDesktopServices.openUrl when the user clicks "Open Download Page", so
    a non-http(s) scheme in the API response would ask the shell to launch its
    registered handler. The value must be rejected at ingestion, the same way
    core.metadata_reports._safe_link already guards the report boundary.
    """

    @pytest.mark.parametrize("hostile", [
        "javascript:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ms-msdt:/id PCWDiagnostic",
        "not a url at all",
        "https://",            # scheme is fine, but there is no host
    ])
    def test_hostile_release_url_is_dropped(self, hostile):
        payload = _release_json("v99.0.0")
        payload["html_url"] = hostile

        outcome = _checker_returning(payload).check_detailed()

        # The update itself is still reported -- only the URL is refused, so
        # the user still learns a release exists.
        assert outcome.status == "update_available"
        assert outcome.release is not None
        assert outcome.release.release_url == "", (
            f"hostile html_url survived ingestion: {outcome.release.release_url!r}")

    def test_legitimate_release_url_survives(self):
        outcome = _checker_returning(_release_json("v99.0.0")).check_detailed()
        assert outcome.release is not None
        assert outcome.release.release_url == (
            "https://github.com/x/y/releases/tag/v99.0.0")

    def test_hostile_asset_url_is_dropped(self):
        payload = _release_json("v99.0.0", assets=[
            {"browser_download_url": "file:///C:/Windows/System32/calc.exe"}])

        outcome = _checker_returning(payload).check_detailed()

        assert outcome.release is not None
        assert outcome.release.asset_url == ""

    def test_legitimate_asset_url_survives(self):
        payload = _release_json("v99.0.0", assets=[
            {"browser_download_url": "https://github.com/x/y/releases/download/v99.0.0/s.exe"}])

        outcome = _checker_returning(payload).check_detailed()

        assert outcome.release is not None
        assert outcome.release.asset_url == (
            "https://github.com/x/y/releases/download/v99.0.0/s.exe")
