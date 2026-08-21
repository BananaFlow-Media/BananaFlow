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
* Query PyPI's public JSON API for the latest published version. For yt-dlp,
  inspect non-yanked release entries as well so nightly builds are visible.
* Compare using numeric tuple ordering (handles yt-dlp's zero-padded
  CalVer like "2026.07.04" == "2026.7.4" as well as yt-dlp-ejs SemVer).
* Never raise: any network/parsing failure is expressed per component as
  ``check_ok=False`` so a flaky network can't crash a background check.

Installation is deliberately separate: source environments use the explicit
pip command below, while packaged builds use the verified app-data overlay in
``core.component_overlay``. Both paths run only after user approval.

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

    Works for yt-dlp CalVer ("2026.07.04" → (2026, 7, 4)), yt-dlp nightly
    CalVer ("2026.8.4.234419.dev0"), and plain SemVer. Hyphen/local suffixes
    are ignored so existing semantic-pre-release comparisons stay unchanged.
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
    if not r or not l:
        return False
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
    """Query PyPI for the latest appropriate version of each component."""

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

        status.check_ok = bool(status.installed_version and status.latest_version)
        if status.check_ok:
            status.update_available = is_newer_version(
                status.latest_version, status.installed_version,
            )
        return status

    @staticmethod
    def _latest_non_yanked_release(data: dict) -> str:
        """Newest parseable release that has at least one non-yanked file."""
        candidates: list[str] = []
        for version, files in (data.get("releases") or {}).items():
            if not parse_version_tuple(str(version)) or not files:
                continue
            if all(bool(file_info.get("yanked")) for file_info in files):
                continue
            candidates.append(str(version))
        return max(candidates, key=parse_version_tuple, default="")

    def _fetch_latest_pypi(self, pypi_name: str) -> str:
        url = f"https://pypi.org/pypi/{pypi_name}/json"
        headers = {"User-Agent": self._USER_AGENT, "Accept": "application/json"}
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

        if pypi_name == "yt-dlp":
            # info.version follows PyPI's normal stable view. yt-dlp also
            # publishes an upstream-recommended nightly channel, so inspect the
            # release map and choose the newest non-yanked build deliberately.
            newest = self._latest_non_yanked_release(data)
            if newest:
                return newest

        version = data.get("info", {}).get("version", "")
        return str(version) if version else ""


# ──────────────────────────────────────────────────────────────────────────────
# In-place upgrade support (source mode only)
# ──────────────────────────────────────────────────────────────────────────────

def can_update_in_place() -> bool:
    """Whether pip can update the current source environment in place.

    Frozen builds return False because their approved path is the separate
    versioned app-data overlay, not mutation of the installed environment.
    """
    from utils.paths import is_frozen
    return not is_frozen()


def pip_upgrade_command() -> list[str]:
    """The exact command an approved in-place component update runs.

    ``--pre`` is required for pip to consider yt-dlp's nightly builds.
    Upgrading ``yt-dlp[default]`` refreshes yt-dlp *and* pulls the matching
    pinned yt-dlp-ejs, keeping the pair consistent.
    """
    return [
        sys.executable, "-m", "pip", "install", "--upgrade", "--pre",
        "yt-dlp[default]",
    ]
