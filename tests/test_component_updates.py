"""
tests/test_component_updates.py  –  Runtime component update checker
========================================================================
Everything runs offline: the PyPI fetcher and installed-version lookup
are injected, so no test touches the network or depends on what happens
to be installed in the environment running the suite.
"""

from __future__ import annotations

import sys

from core.component_updates import (
    MONITORED_COMPONENTS,
    ComponentStatus,
    ComponentUpdateChecker,
    can_update_in_place,
    installed_component_version,
    is_newer_version,
    parse_version_tuple,
    pip_upgrade_command,
)


# ──────────────────────────────────────────────────────────────────────────────
# Version parsing / comparison
# ──────────────────────────────────────────────────────────────────────────────

class TestVersionComparison:

    def test_parse_calver_and_semver(self):
        assert parse_version_tuple("2026.07.04") == (2026, 7, 4)
        assert parse_version_tuple("2026.7.4") == (2026, 7, 4)
        assert parse_version_tuple("0.8.0") == (0, 8, 0)
        assert parse_version_tuple("v1.2.3") == (1, 2, 3)
        assert parse_version_tuple("1.0.0-beta.1") == (1, 0, 0)
        assert parse_version_tuple("garbage") == ()
        assert parse_version_tuple("") == ()

    def test_zero_padded_calver_equals_plain(self):
        # yt-dlp publishes "2026.07.04" while comparisons may see "2026.7.4"
        assert not is_newer_version("2026.07.04", "2026.7.4")
        assert not is_newer_version("2026.7.4", "2026.07.04")

    def test_strictly_newer(self):
        assert is_newer_version("2026.7.5", "2026.07.04")
        assert is_newer_version("2027.1.1", "2026.12.31")
        assert is_newer_version("0.9.0", "0.8.0")
        assert not is_newer_version("0.8.0", "0.8.0")
        assert not is_newer_version("0.7.9", "0.8.0")

    def test_different_lengths_zero_padded(self):
        assert not is_newer_version("1.2", "1.2.0")
        assert is_newer_version("1.2.1", "1.2")

    def test_garbage_never_reports_newer(self):
        assert not is_newer_version("garbage", "2026.6.9")
        assert not is_newer_version("", "")


# ──────────────────────────────────────────────────────────────────────────────
# Checker (fully injected — no network)
# ──────────────────────────────────────────────────────────────────────────────

def _checker(latest: dict[str, str], installed: dict[str, str]) -> ComponentUpdateChecker:
    return ComponentUpdateChecker(
        _fetch_latest=lambda name: latest[name],
        _installed=lambda name: installed.get(name, ""),
    )


class TestComponentUpdateChecker:

    def test_monitored_components_are_the_critical_ones(self):
        keys = [spec.key for spec in MONITORED_COMPONENTS]
        assert keys == ["yt-dlp", "yt-dlp-ejs"]

    def test_up_to_date_environment(self):
        report = _checker(
            latest={"yt-dlp": "2026.7.4", "yt-dlp-ejs": "0.8.0"},
            installed={"yt-dlp": "2026.07.04", "yt-dlp-ejs": "0.8.0"},
        ).check()
        assert report.all_checks_ok
        assert not report.has_updates
        assert report.updates == []

    def test_outdated_component_detected(self):
        report = _checker(
            latest={"yt-dlp": "2026.8.1", "yt-dlp-ejs": "0.8.0"},
            installed={"yt-dlp": "2026.07.04", "yt-dlp-ejs": "0.8.0"},
        ).check()
        assert report.has_updates
        assert [c.key for c in report.updates] == ["yt-dlp"]
        yt = report.updates[0]
        assert yt.installed_version == "2026.07.04"
        assert yt.latest_version == "2026.8.1"
        assert yt.check_ok

    def test_network_failure_is_absorbed_not_raised(self):
        def boom(_name: str) -> str:
            raise ConnectionError("no network")

        report = ComponentUpdateChecker(
            _fetch_latest=boom,
            _installed=lambda name: "2026.07.04",
        ).check()
        assert not report.has_updates
        assert not report.all_checks_ok
        assert all(not c.check_ok for c in report.components)
        # Installed versions were still resolved — the failure was remote.
        assert all(c.installed_version for c in report.components)

    def test_missing_installed_package_marks_check_not_ok(self):
        report = _checker(
            latest={"yt-dlp": "2026.8.1", "yt-dlp-ejs": "0.9.0"},
            installed={},   # nothing importable / no metadata
        ).check()
        assert not report.has_updates          # no comparison possible
        assert all(not c.check_ok for c in report.components)
        assert all(c.latest_version for c in report.components)

    def test_partial_failure_still_reports_the_working_component(self):
        def latest(name: str) -> str:
            if name == "yt-dlp":
                return "2026.9.1"
            raise TimeoutError("pypi slow")

        report = ComponentUpdateChecker(
            _fetch_latest=latest,
            _installed=lambda name: {"yt-dlp": "2026.6.9", "yt-dlp-ejs": "0.8.0"}[name],
        ).check()
        assert [c.key for c in report.updates] == ["yt-dlp"]
        assert not report.all_checks_ok
        assert report.any_check_ok

    def test_never_downgrades(self):
        # Installed is ahead of PyPI (e.g. nightly / dev install)
        report = _checker(
            latest={"yt-dlp": "2026.6.9", "yt-dlp-ejs": "0.8.0"},
            installed={"yt-dlp": "2026.07.04", "yt-dlp-ejs": "0.9.0"},
        ).check()
        assert not report.has_updates

    def test_pypi_url(self):
        status = ComponentStatus(key="yt-dlp", display_name="yt-dlp")
        assert status.pypi_url == "https://pypi.org/project/yt-dlp/"


# ──────────────────────────────────────────────────────────────────────────────
# In-place upgrade support
# ──────────────────────────────────────────────────────────────────────────────

class TestInPlaceUpgrade:

    def test_pip_command_upgrades_the_default_extra(self):
        cmd = pip_upgrade_command()
        assert cmd[0] == sys.executable
        assert cmd[1:4] == ["-m", "pip", "install"]
        assert "--upgrade" in cmd
        # Must go through the [default] extra so yt-dlp-ejs stays the
        # exact pinned match — never a bare/unpaired upgrade.
        assert cmd[-1] == "yt-dlp[default]"

    def test_all_specs_upgrade_through_the_same_requirement(self):
        assert {spec.pip_requirement for spec in MONITORED_COMPONENTS} == {"yt-dlp[default]"}

    def test_can_update_in_place_in_source_mode(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert can_update_in_place() is True

    def test_cannot_update_in_place_when_frozen(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert can_update_in_place() is False

    def test_installed_version_lookup_never_raises_for_unknown(self):
        assert installed_component_version("definitely-not-a-real-pkg-xyz") == ""
