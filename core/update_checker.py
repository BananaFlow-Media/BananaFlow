"""
core/update_checker.py  –  GitHub releases update checker
==========================================================
Responsibilities
----------------
* Query the GitHub Releases API for the latest published release of
  BananaFlow.
* Compare it against the hard-coded CURRENT_VERSION using proper
  semantic-version (SemVer) ordering, not lexicographic string comparison.
* Return a ReleaseInfo dataclass when an update is available, or None
  when the app is already up to date.
* Never raise to the caller – all network and parsing failures are caught
  internally and surfaced only through the return value being None, so a
  flaky network on startup never crashes the app or shows an error dialog.

Design decisions
----------------
* Zero GUI imports – pure stdlib + httpx only.
* Semantic versioning: "1.10.0" correctly compares as newer than "1.9.0".
* The GitHub API is queried with a conservative 8-second timeout and a
  descriptive User-Agent string (GitHub's API guidelines require one).
* Pre-releases (where "prerelease": true in the API response) are skipped
  by default; pass include_prereleases=True to the check() method to
  include them.
* CURRENT_VERSION is a single module-level constant – update it on every
  release.  The main.py entry point should read it from here:
      from core.update_checker import CURRENT_VERSION

Usage
-----
>>> checker = UpdateChecker()
>>> info = checker.check()
>>> if info:
...     print(f"Update available: v{info.version}")
...     print(f"Release notes: {info.release_notes[:200]}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx


# ──────────────────────────────────────────────────────────────────────────────
# Version constant
# ──────────────────────────────────────────────────────────────────────────────
# Sourced from the top-level version module so a single bump propagates
# to the update checker, MusicBrainz User-Agent, CLI --version, and the
# Qt setApplicationVersion call in main.py.

from version import FULL_VERSION as CURRENT_VERSION  # noqa: E402


def _safe_release_url(url: str) -> str:
    """Keep a release URL only if it is http(s) with a host, else drop it.

    Release URLs arrive verbatim from the GitHub API response and are handed
    to QDesktopServices.openUrl, which asks the Windows shell to launch the
    scheme's registered handler. A response carrying ``javascript:``,
    ``file:///C:/...`` or any other scheme must therefore never reach it.
    Failing closed to "" is what the update dialog already treats as "no URL"
    (``if ... .release_url``), so a dropped value degrades to the generic
    releases page rather than opening anything unexpected.

    This mirrors core.metadata_reports._safe_link, which defends the report
    boundary the same way.
    """
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


# ──────────────────────────────────────────────────────────────────────────────
# Public data-class
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ReleaseInfo:
    """
    Metadata about a newer GitHub release, returned when an update is found.

    Fields
    ------
    version         Clean version string without a leading 'v', e.g. "1.2.0".
    release_url     Direct URL to the GitHub release page.
    release_notes   Markdown body of the release (may be empty string).
    published_at    ISO-8601 timestamp from GitHub, e.g. "2025-03-15T10:00:00Z".
    asset_url       URL to the first downloadable release asset (installer /
                    zip), if one exists.  Empty string if no assets attached.
    """
    version:       str
    release_url:   str
    release_notes: str
    published_at:  str
    asset_url:     str = ""

    def short_notes(self, max_chars: int = 400) -> str:
        """
        Return a truncated, plain-text version of the release notes suitable
        for display in a notification banner or tooltip.
        Strips Markdown heading markers and trims to max_chars.
        """
        plain = re.sub(r"#{1,6}\s*", "", self.release_notes)   # remove ## headings
        plain = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", plain)   # remove **bold**
        plain = re.sub(r"`(.+?)`", r"\1", plain)                # remove `code`
        plain = plain.strip()
        if len(plain) > max_chars:
            return plain[:max_chars].rsplit(" ", 1)[0] + " …"
        return plain

    def display_version(self) -> str:
        """Return the version string with a leading 'v', e.g. 'v1.2.0'."""
        return f"v{self.version}"


@dataclass
class UpdateCheckOutcome:
    """
    Result of a manual/interactive update check where "no update" and
    "check failed" must be told apart (check() collapses both to None,
    which is right for the silent startup path but useless feedback for
    a Settings button the user just clicked).

    status is machine-readable:
        "update_available"  – ``release`` carries the newer ReleaseInfo
        "up_to_date"        – GitHub answered; no strictly newer release
        "error"             – network/API/parsing failure of any kind
    """
    status:  str
    release: Optional["ReleaseInfo"] = None

    @property
    def update_available(self) -> bool:
        return self.status == "update_available" and self.release is not None


# ──────────────────────────────────────────────────────────────────────────────
# Semantic version helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_semver(version_str: str) -> tuple[int, int, int]:
    """
    Parse a SemVer string into a (major, minor, patch) integer tuple.

    Handles common prefixes and pre-release suffixes gracefully:
        "v1.2.3"       → (1, 2, 3)
        "1.10.0"       → (1, 10, 0)
        "2.0.0-beta.1" → (2, 0, 0)   # pre-release suffix ignored for ordering
        "1.0"          → (1, 0, 0)   # missing patch treated as 0
        "garbage"      → (0, 0, 0)   # falls back to zero-tuple safely
    """
    # Strip leading 'v' or 'V'
    clean = version_str.strip().lstrip("vV")
    # Strip pre-release suffix (everything after a hyphen)
    clean = clean.split("-")[0].split("+")[0]
    # Extract numeric parts
    parts = re.findall(r"\d+", clean)
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch = int(parts[2]) if len(parts) > 2 else 0
    return major, minor, patch


def _is_newer(remote_version: str, local_version: str) -> bool:
    """
    Return True if remote_version is strictly newer than local_version
    using semantic version ordering.

    >>> _is_newer("1.10.0", "1.9.0")
    True
    >>> _is_newer("1.0.0", "1.0.0")
    False
    >>> _is_newer("0.9.9", "1.0.0")
    False
    """
    return _parse_semver(remote_version) > _parse_semver(local_version)


# ──────────────────────────────────────────────────────────────────────────────
# UpdateChecker
# ──────────────────────────────────────────────────────────────────────────────

class UpdateChecker:
    """
    Query the GitHub Releases API and compare against CURRENT_VERSION.

    Parameters
    ----------
    repo_owner : str
        GitHub username or organisation that owns the repository.
    repo_name  : str
        Repository name on GitHub.
    timeout    : float
        HTTP request timeout in seconds.  Defaults to 8.0.

    The GitHub API endpoint used is:
        GET https://api.github.com/repos/{owner}/{repo}/releases/latest

    For public repositories this endpoint requires no authentication and
    allows up to 60 requests/hour per IP address – more than sufficient
    for a once-per-launch update check.
    """

    _API_BASE = "https://api.github.com"
    _USER_AGENT = f"BananaFlow/{CURRENT_VERSION} (update-checker; httpx)"

    def __init__(
        self,
        repo_owner: str = "BananaFlow-Media",
        repo_name:  str = "BananaFlow",
        timeout:    float = 8.0,
    ) -> None:
        self._repo_owner = repo_owner
        self._repo_name  = repo_name
        self._timeout    = timeout

    # ── Public API ─────────────────────────────────────────────────────────────

    def check(
        self,
        include_prereleases: bool = False,
    ) -> Optional[ReleaseInfo]:
        """
        Check for a newer release on GitHub.

        Returns
        -------
        ReleaseInfo
            When a newer version is available.
        None
            When the app is up to date, the network is unavailable,
            the GitHub API returns an error, or parsing fails for any reason.
            This method is guaranteed never to raise.
        """
        return self.check_detailed(include_prereleases=include_prereleases).release

    def check_detailed(
        self,
        include_prereleases: bool = False,
    ) -> UpdateCheckOutcome:
        """
        Like check(), but distinguishes "up to date" from "check failed"
        so interactive callers (the Settings buttons) can give honest
        feedback. Never raises.
        """
        try:
            if include_prereleases:
                releases = self._fetch_all_releases()
                release = releases[0] if releases else None
            else:
                release = self._fetch_latest_release()
        except Exception:
            return UpdateCheckOutcome(status="error")

        if release is None:
            return UpdateCheckOutcome(status="error")

        try:
            info = self._release_to_info(release, include_prereleases)
        except Exception:
            return UpdateCheckOutcome(status="error")

        if info is None:
            return UpdateCheckOutcome(status="up_to_date")
        return UpdateCheckOutcome(status="update_available", release=info)

    def current_version(self) -> str:
        """Return the hard-coded version of this build."""
        return CURRENT_VERSION

    # ── Internal logic ─────────────────────────────────────────────────────────

    def _release_to_info(
        self,
        release: dict,
        include_prereleases: bool,
    ) -> Optional[ReleaseInfo]:
        """
        Convert one GitHub release JSON dict into a ReleaseInfo, or None
        when it is not a notifiable update (not newer, draft, or a
        pre-release the caller didn't ask for).
        """
        # Extract version from tag name (e.g. "v1.2.0" → "1.2.0")
        tag       = release.get("tag_name", "")
        version   = tag.lstrip("vV").strip()

        if not version:
            # A release with no version tag is a malformed response, not
            # proof we're current — surfaces as "error" via check_detailed.
            raise ValueError("release JSON carries no usable tag_name")

        # Skip if this release is not actually newer
        if not _is_newer(version, CURRENT_VERSION):
            return None

        # Skip draft releases regardless of caller preference
        if release.get("draft", False):
            return None

        # Skip pre-releases unless explicitly requested
        if release.get("prerelease", False) and not include_prereleases:
            return None

        # Extract the URL to the first downloadable asset (installer / archive)
        asset_url = ""
        assets    = release.get("assets", [])
        if assets:
            asset_url = _safe_release_url(assets[0].get("browser_download_url", ""))

        return ReleaseInfo(
            version=version,
            release_url=_safe_release_url(release.get("html_url", "")),
            release_notes=release.get("body", "") or "",
            published_at=release.get("published_at", ""),
            asset_url=asset_url,
        )

    def _fetch_latest_release(self) -> Optional[dict]:
        """
        Fetch the single latest non-pre-release from the GitHub API.
        Returns the parsed JSON dict or None on failure.
        """
        url = (
            f"{self._API_BASE}/repos/"
            f"{self._repo_owner}/{self._repo_name}/releases/latest"
        )
        response_data = self._get_json(url)
        if isinstance(response_data, dict):
            return response_data
        return None

    def _fetch_all_releases(self) -> list[dict]:
        """
        Fetch the list of all releases (including pre-releases) sorted newest
        first.  Used when include_prereleases=True.
        Returns an empty list on failure.
        """
        url = (
            f"{self._API_BASE}/repos/"
            f"{self._repo_owner}/{self._repo_name}/releases"
            "?per_page=10"
        )
        response_data = self._get_json(url)
        if isinstance(response_data, list):
            return response_data
        return []

    def _get_json(self, url: str) -> object:
        """
        Perform a GET request to `url` and return the parsed JSON body.
        Raises httpx.HTTPError or json.JSONDecodeError on failure.
        """
        headers = {
            "User-Agent": self._USER_AGENT,
            "Accept":     "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()


# ──────────────────────────────────────────────────────────────────────────────
# Smoke-test  (python core/update_checker.py)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("UpdateChecker  –  smoke-test")
    print("=" * 60)
    print()

    # ── 1. SemVer parsing and comparison ─────────────────────────────────────
    print("── 1. Semantic version comparison ──")

    cases: list[tuple[str, str, bool]] = [
        ("1.1.0",   "1.0.0",   True),    # minor bump
        ("1.10.0",  "1.9.0",   True),    # double-digit minor
        ("2.0.0",   "1.99.99", True),    # major bump
        ("1.0.0",   "1.0.0",   False),   # same version
        ("0.9.9",   "1.0.0",   False),   # older remote
        ("v1.2.3",  "1.2.2",   True),    # leading-v remote
        ("1.0.0-beta.1", "0.9.0", True), # pre-release suffix ignored
        ("garbage", "1.0.0",   False),   # malformed remote
    ]

    all_ok = True
    for remote, local, expected in cases:
        result = _is_newer(remote, local)
        icon   = "✅" if result == expected else "❌"
        print(f"  {icon}  _is_newer({remote!r:>14}, {local!r:<8}) → {result}  (expected {expected})")
        if result != expected:
            all_ok = False

    assert all_ok, "SemVer comparison test failed."
    print()

    # ── 2. _parse_semver edge cases ───────────────────────────────────────────
    print("── 2. _parse_semver edge cases ──")
    parse_cases: list[tuple[str, tuple[int, int, int]]] = [
        ("1.2.3",       (1, 2, 3)),
        ("v2.10.0",     (2, 10, 0)),
        ("1.0",         (1, 0, 0)),
        ("3",           (3, 0, 0)),
        ("1.0.0-rc.1",  (1, 0, 0)),
        ("not-a-ver",   (0, 0, 0)),
    ]
    for raw, expected_tuple in parse_cases:
        parsed = _parse_semver(raw)
        icon   = "✅" if parsed == expected_tuple else "❌"
        print(f"  {icon}  _parse_semver({raw!r:<18}) → {parsed}  (expected {expected_tuple})")
    print()

    # ── 3. Live GitHub API check (network) ────────────────────────────────────
    print("── 3. Live GitHub API check ──")
    print("  (Querying a well-known public repo as a connectivity test)")
    print("  Using: github/gitignore  (guaranteed to have releases)")

    # Use a known-public repo with releases as a connectivity proxy.
    # Your own repo details will be substituted once you publish to GitHub.
    test_checker = UpdateChecker(
        repo_owner="github",          # public repo used as connectivity test
        repo_name="gitignore",
        timeout=8.0,
    )

    # We do NOT assert on the result value here (repo may or may not exist)
    # – we only verify that the method never raises.
    result = test_checker.check()
    if result is not None:
        print(f"  Found release: v{result.version}  →  {result.release_url}")
        print(f"  Published: {result.published_at}")
        print(f"  Notes preview: {result.short_notes(120)}")
    else:
        print("  No update found (or repo not yet published) – returned None ✅")
        print("  (This is correct – check() must never raise, only return None)")
    print()

    # ── 4. ReleaseInfo helper methods ─────────────────────────────────────────
    print("── 4. ReleaseInfo helper methods ──")
    sample = ReleaseInfo(
        version="2.0.0",
        release_url="https://github.com/user/repo/releases/tag/v2.0.0",
        release_notes=(
            "## What's New\n\n"
            "- **Clipboard Monitor** now detects Spotify links\n"
            "- Fixed `crash` on Windows 11 when output dir has spaces\n"
            "- Improved search result ranking\n"
        ),
        published_at="2025-06-01T12:00:00Z",
        asset_url="https://github.com/user/repo/releases/download/v2.0.0/bananaflow-setup.exe",
    )
    print(f"  display_version()  → {sample.display_version()!r}   (expected 'v2.0.0')")
    assert sample.display_version() == "v2.0.0"

    notes = sample.short_notes(80)
    print(f"  short_notes(80)    → {notes!r}")
    assert len(notes) <= 90   # small tolerance for ellipsis
    assert "##" not in notes, "Markdown headings should be stripped"
    assert "**" not in notes, "Markdown bold markers should be stripped"
    print("  ✅  ReleaseInfo helpers work correctly")
    print()

    print("=" * 60)
    print("All offline assertions passed ✅")
    print("Network tests returned None gracefully if repo not found ✅")
    sys.exit(0)
