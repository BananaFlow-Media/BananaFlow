"""Static guards on the Inno Setup source paths (finding F-12).

The installer used to reference its inputs as ``..\\dist\\bananaflow\\*``. ISCC does
not normalize that: it composes ``<repo>\\packaging\\..\\dist\\...`` literally
and hands the result to the Win32 file APIs, spending 13 characters of the 260
character ``MAX_PATH`` budget on a segment that means nothing. With the PO Token
Provider backend's ``node_modules`` nesting underneath, the audit measured 262
characters composed versus 249 normalized — so the installer could not compile
from any checkout with a path longer than roughly 70 characters, and said so
with "The system cannot find the path specified" while naming a node_modules
file.

Compiling the installer needs ISCC and a full PyInstaller build, so these are
static guards over the script text. The real compiles — from the ordinary path
and from a deliberately long one — are release evidence recorded in
``docs/release/RELEASING.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ISS_PATH = REPO_ROOT / "packaging" / "bananaflow.iss"


def _iss_text() -> str:
    return ISS_PATH.read_text(encoding="utf-8")


def _directives(text: str, name: str) -> list[str]:
    """Return the values of every ``Name: "value"`` style directive."""
    return re.findall(rf'(?mi)^\s*{name}\s*:\s*"([^"]+)"', text)


def _settings(text: str, name: str) -> list[str]:
    """Return the values of every ``Name=value`` [Setup] entry."""
    return re.findall(rf"(?mi)^\s*{name}\s*=\s*(.+?)\s*$", text)


def test_source_root_is_defined_from_the_script_location():
    """The roots must resolve once, at compile time, not per-Source string."""
    text = _iss_text()

    assert re.search(
        r'(?m)^#define\s+RepoRoot\s+ExtractFilePath\(RemoveBackslash\(SourcePath\)\)',
        text,
    ), "RepoRoot must be derived from SourcePath so it is absolute and normalized"
    assert re.search(r'(?m)^#define\s+DistDir\s+RepoRoot\s*\+\s*"dist\\bananaflow"', text), (
        "DistDir must be composed from RepoRoot"
    )


def test_no_source_entry_uses_parent_traversal():
    """The actual F-12 regression: no avoidable ``..`` in any packaged input."""
    sources = _directives(_iss_text(), "Source")

    assert sources, "the installer must package at least one source"
    for value in sources:
        assert ".." not in value, (
            f"Source {value!r} still contains parent traversal; ISCC does not "
            f"normalize it and it costs MAX_PATH budget for nothing"
        )


def test_every_source_entry_is_rooted_at_a_normalized_define():
    sources = _directives(_iss_text(), "Source")

    for value in sources:
        assert value.startswith("{#DistDir}") or value.startswith("{#RepoRoot}"), (
            f"Source {value!r} is not rooted at a normalized define"
        )


def test_output_and_license_paths_are_normalized():
    text = _iss_text()

    for setting in ("OutputDir", "LicenseFile", "SetupIconFile"):
        values = _settings(text, setting)
        assert values, f"{setting} must be set"
        for value in values:
            assert ".." not in value, f"{setting}={value} still traverses upward"


def test_the_composed_dist_root_is_shorter_than_the_old_form():
    """Prove the fix actually buys MAX_PATH budget rather than just reading better."""
    old = REPO_ROOT / "packaging" / ".." / "dist" / "bananaflow"
    new = REPO_ROOT / "dist" / "bananaflow"

    saved = len(str(old)) - len(str(new))

    assert saved == len("\\packaging\\..") == 13
    assert str(new) == str(new.resolve()), "the normalized form has no traversal left"


def test_version_is_still_read_from_the_built_exe():
    """The version propagation must survive the path change."""
    text = _iss_text()

    match = re.search(r'(?m)^#define\s+AppVersion\s+GetStringFileInfo\((.+?),', text)
    assert match, "AppVersion must still be read from the built EXE"
    assert "DistDir" in match.group(1)
    assert ".." not in match.group(1)


# ──────────────────────────────────────────────────────────────────────────────
# Nothing may be dropped by the path correction
# ──────────────────────────────────────────────────────────────────────────────

#: Every payload the installer shipped before F-12 was corrected. A path fix
#: that silently stopped installing a file would be a far worse defect than the
#: one being fixed, and would not fail any compile.
_REQUIRED_SOURCES = {
    "{#DistDir}\\*",
    "{#RepoRoot}scripts\\install_playwright.ps1",
    "{#RepoRoot}LICENSE",
    "{#RepoRoot}LICENSES.md",
    "{#RepoRoot}NOTICE",
    "{#RepoRoot}SOURCE_OFFER.md",
    "{#RepoRoot}CONTRIBUTING.md",
    "{#RepoRoot}THIRD_PARTY_NOTICES.md",
    "{#RepoRoot}README.md",
}


def test_no_installed_file_was_lost_in_the_path_correction():
    sources = set(_directives(_iss_text(), "Source"))

    assert _REQUIRED_SOURCES <= sources, (
        f"the installer stopped packaging: {_REQUIRED_SOURCES - sources}"
    )


@pytest.mark.parametrize("relative", sorted(
    value.replace("{#RepoRoot}", "").replace("\\", "/")
    for value in _REQUIRED_SOURCES
    if value.startswith("{#RepoRoot}")
))
def test_every_referenced_repo_file_exists(relative: str):
    """A normalized path that points at nothing fails the compile, not a test."""
    assert (REPO_ROOT / relative).is_file(), f"{relative} is referenced but missing"


def test_destinations_and_uninstall_policy_are_unchanged():
    """The path fix must not move where anything lands or what survives uninstall."""
    text = _iss_text()

    assert 'DefaultDirName={autopf}\\{#AppName}' in text
    assert re.search(r'(?m)^\s*Source:\s*"\{#DistDir\}\\\*";\s*DestDir:\s*"\{app\}"', text)
    assert 'PrivilegesRequired=lowest' in text
    # User data lives outside {app} and must not be touched by uninstall.
    uninstall = re.findall(r'(?mi)^\s*Type:\s*filesandordirs;\s*Name:\s*"([^"]+)"', text)
    assert uninstall == ["{app}\\scripts"], (
        f"uninstall policy changed: {uninstall}"
    )
    assert ".bananaflow" not in "".join(uninstall), "user data must survive uninstall"
