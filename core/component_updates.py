"""
core/component_updates.py  –  Runtime component update checker
================================================================
BananaFlow's ability to download depends on runtime components that go stale
much faster than the app itself — above all yt-dlp (and its matched
yt-dlp-ejs JS-solver package). When those fall behind, YouTube changes
break downloads even though nothing in BananaFlow changed. This module
answers one question: "are the critical runtime components up to date?"

Responsibilities
----------------
* Look up the installed version of each monitored component
  (importlib.metadata, with a yt_dlp module fallback for frozen builds).
* Query PyPI's public JSON API for the latest published version.
* Compare using numeric tuple ordering (handles yt-dlp's zero-padded
  CalVer like "2026.07.04" == "2026.7.4" as well as yt-dlp-ejs SemVer).
* Never raise: any network/parsing failure is expressed per component as
  ``check_ok=False`` so a flaky network can't crash a background check.

What this module does NOT do
----------------------------
* It never installs anything. Building the pip command
  (:func:`pip_upgrade_command`) is as far as it goes — running it is the
  UI's job, and only after explicit user approval.
* In a packaged (PyInstaller-frozen) build the components are baked into
  the EXE and cannot be upgraded in place; :func:`can_update_in_place`
  reports that so the UI can explain that a full app update is required.

Design
------
* Zero GUI imports — stdlib + httpx only, mirroring core/update_checker.
* The HTTP fetcher and installed-version lookup are injectable so tests
  never touch the network or depend on the local environment.
"""

from __future__ import annotations

import importlib.metadata
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

from version import FULL_VERSION as _APP_VERSION


# ──────────────────────────────────────────────────────────────────────────────
# Monitored components
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComponentSpec:
    """One runtime component the app monitors for staleness.

    ``pip_requirement`` is what an in-place upgrade actually installs.
    yt-dlp-ejs is deliberately upgraded *through* ``yt-dlp[default]``:
    the [default] extra pins the exact matching ejs release, so
    upgrading it independently could produce a mismatched pair.
    """
    key:             str    # stable id used in update-state ids
    display_name:    str
    pypi_name:       str
    pip_requirement: str


MONITORED_COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        key="yt-dlp",
        display_name="yt-dlp",
        pypi_name="yt-dlp",
        pip_requirement="yt-dlp[default]",
    ),
    ComponentSpec(
        key="yt-dlp-ejs",
        display_name="yt-dlp-ejs",
        pypi_name="yt-dlp-ejs",
        pip_requirement="yt-dlp[default]",
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ComponentStatus:
    """Check result for one component."""
    key:               str
    display_name:      str
    installed_version: str = ""     # "" when the package couldn't be found
    latest_version:    str = ""     # "" when the PyPI lookup failed
    update_available:  bool = False
    check_ok:          bool = False  # False = lookup failed (network etc.)

    @property
    def pypi_url(self) -> str:
        return f"https://pypi.org/project/{self.key}/"


@dataclass
class ComponentUpdateReport:
    components: list[ComponentStatus] = field(default_factory=list)

    @property
    def updates(self) -> list[ComponentStatus]:
        return [c for c in self.components if c.update_available]

    @property
    def has_updates(self) -> bool:
        return bool(self.updates)

    @property
    def all_checks_ok(self) -> bool:
        return bool(self.components) and all(c.check_ok for c in self.components)

    @property
    def any_check_ok(self) -> bool:
        return any(c.check_ok for c in self.components)


# ──────────────────────────────────────────────────────────────────────────────
# Version comparison
# ──────────────────────────────────────────────────────────────────────────────

def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Extract every integer run from a version string.

    Works for yt-dlp CalVer ("2026.07.04" → (2026, 7, 4)) and plain
    SemVer ("0.8.0" → (0, 8, 0)). Pre-release/local suffixes after '-'
    or '+' are ignored. Garbage yields an empty tuple.
    """
    clean = (version_str or "").strip().lstrip("vV")
    clean = clean.split("-")[0].split("+")[0]
    return tuple(int(p) for p in re.findall(r"\d+", clean))


def is_newer_version(remote: str, local: str) -> bool:
    """True when ``remote`` is strictly newer than ``local``.

    Tuples of different lengths are compared zero-padded so
    "1.2" == "1.2.0". Two unparseable strings compare as equal (False).
    """
    r, l = parse_version_tuple(remote), parse_version_tuple(local)
    width = max(len(r), len(l))
    return r + (0,) * (width - len(r)) > l + (0,) * (width - len(l))


# ──────────────────────────────────────────────────────────────────────────────
# Installed-version lookup
# ──────────────────────────────────────────────────────────────────────────────

def installed_component_version(pypi_name: str) -> str:
    """Best-effort installed version of a monitored package.

    importlib.metadata works both in a source venv and (when PyInstaller
    bundles the dist-info) in a frozen build; yt-dlp additionally ships
    its version as a module attribute, which survives freezing even
    without metadata.
    """
    try:
        return importlib.metadata.version(pypi_name)
    except Exception:
        pass
    if pypi_name == "yt-dlp":
        try:
            import yt_dlp
            return yt_dlp.version.__version__
        except Exception:
            return ""
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# ComponentUpdateChecker
# ──────────────────────────────────────────────────────────────────────────────

class ComponentUpdateChecker:
    """
    Query PyPI for the latest version of each monitored component.

    PyPI's JSON API (GET https://pypi.org/pypi/<name>/json) is public,
    unauthenticated, and returns ``info.version`` as the latest
    non-yanked release — one request per component per check.
    """

    _USER_AGENT = f"BananaFlow/{_APP_VERSION} (component-update-checker; httpx)"

    def __init__(
        self,
        timeout: float = 8.0,
        *,
        _fetch_latest: Optional[Callable[[str], str]] = None,
        _installed: Callable[[str], str] = installed_component_version,
    ) -> None:
        self._timeout = timeout
        self._fetch_latest = _fetch_latest if _fetch_latest is not None else self._fetch_latest_pypi
        self._installed = _installed

    # ── Public API ─────────────────────────────────────────────────────────────

    def check(self) -> ComponentUpdateReport:
        """Check every monitored component. Never raises."""
        report = ComponentUpdateReport()
        for spec in MONITORED_COMPONENTS:
            report.components.append(self._check_one(spec))
        return report

    # ── Internals ──────────────────────────────────────────────────────────────

    def _check_one(self, spec: ComponentSpec) -> ComponentStatus:
        status = ComponentStatus(key=spec.key, display_name=spec.display_name)
        try:
            status.installed_version = self._installed(spec.pypi_name) or ""
        except Exception:
            status.installed_version = ""
        try:
            status.latest_version = self._fetch_latest(spec.pypi_name) or ""
        except Exception:
            status.latest_version = ""

        # A meaningful comparison needs both sides.
        status.check_ok = bool(status.installed_version and status.latest_version)
        if status.check_ok:
            status.update_available = is_newer_version(
                status.latest_version, status.installed_version,
            )
        return status

    def _fetch_latest_pypi(self, pypi_name: str) -> str:
        url = f"https://pypi.org/pypi/{pypi_name}/json"
        headers = {"User-Agent": self._USER_AGENT, "Accept": "application/json"}
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        version = data.get("info", {}).get("version", "")
        return str(version) if version else ""


# ──────────────────────────────────────────────────────────────────────────────
# In-place upgrade support (source mode only)
# ──────────────────────────────────────────────────────────────────────────────

def can_update_in_place() -> bool:
    """True when components can be pip-upgraded into the running
    environment (source checkout / venv). False for a frozen EXE, where
    dependencies are baked into the bundle and only a full app update
    can refresh them."""
    from utils.paths import is_frozen
    return not is_frozen()


def pip_upgrade_command() -> list[str]:
    """The exact command an approved in-place component update runs.

    Upgrading ``yt-dlp[default]`` refreshes yt-dlp *and* pulls the
    matching pinned yt-dlp-ejs, keeping the pair consistent.
    """
    return [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"]
