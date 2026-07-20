"""Regression checks for BananaFlow's release compliance docs.

These tests intentionally stay text-based: they guard the files the
installer and release process actually rely on without changing runtime
downloader behavior.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _requirement_name(raw: str) -> str:
    raw = raw.split("#", 1)[0].split(";", 1)[0].strip().strip('"').strip("'")
    raw = re.split(r"\s*(?:\[|==|>=|<=|~=|!=|>|<)", raw, maxsplit=1)[0]
    return raw.strip().lower().replace("_", "-")


def _requirements_txt_names() -> set[str]:
    names: set[str] = set()
    for line in _read("requirements.txt").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = _requirement_name(stripped)
        if name:
            names.add(name)
    return names


def _toml_array_entries(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(rf"^\s*{re.escape(key)}\s*=", line):
            continue

        assert "[" in line, f"{key!r} must be a TOML array"
        body_lines = [line.split("[", 1)[1]]
        if "]" not in body_lines[0]:
            for following in lines[index + 1 :]:
                body_lines.append(following)
                if "]" in following:
                    break

        body = "\n".join(body_lines).split("]", 1)[0]
        body_without_comments = "\n".join(
            body_line.split("#", 1)[0] for body_line in body.splitlines()
        )
        return re.findall(r'["\']([^"\']+)["\']', body_without_comments)

    raise AssertionError(f"could not find {key!r} array in pyproject.toml")


def _pyproject_dependency_names() -> set[str]:
    text = _read("pyproject.toml")
    entries: list[str] = []
    for key in ("requires", "dependencies", "dev", "po-token"):
        entries.extend(_toml_array_entries(text, key))
    return {
        name
        for name in (_requirement_name(entry) for entry in entries)
        if name
        and not name.startswith("license ::")
        and not name.startswith("programming language ::")
        and not name.startswith("operating system ::")
    }


def test_license_file_exists_and_declares_gpl3_or_later():
    license_text = _read("LICENSE")

    # LICENSE deliberately contains ONLY the unmodified canonical GPLv3
    # text -- no SPDX tag, no project name/copyright preamble. GitHub's
    # Licensee needs a near-100% match to the canonical template to detect
    # a license at all (issue #24: even a single added SPDX-License-
    # Identifier line was apparently enough "extra metadata" to keep this
    # repo showing NOASSERTION). The GPL-3.0-or-later declaration lives in
    # pyproject.toml instead (see test_pyproject_declares_gpl3_or_later_not_mit)
    # and NOTICE/THIRD_PARTY_NOTICES.md for the project-specific notices.
    assert "GNU GENERAL PUBLIC LICENSE" in license_text
    assert "GPL-3.0-or-later" not in license_text, (
        "keep LICENSE as pure canonical GPLv3 text with nothing added -- "
        "the SPDX declaration belongs in pyproject.toml, not here"
    )


def test_pyproject_declares_gpl3_or_later_not_mit():
    pyproject = _read("pyproject.toml")
    # PEP 639 SPDX string form (needs setuptools >= 77). The legacy
    # `license = { text = ... }` table is rejected by setuptools >= 78
    # and broke every wheel build — it must not come back.
    license_line = re.search(r'(?m)^license\s*=\s*"([^"]+)"\s*$', pyproject)

    assert license_line, (
        "pyproject.toml must declare the project license as a PEP 639 "
        'SPDX string, e.g. license = "GPL-3.0-or-later"'
    )
    assert license_line.group(1) == "GPL-3.0-or-later"
    assert re.search(r"(?m)^license\s*=\s*\{", pyproject) is None, (
        "legacy license table syntax breaks wheel builds on setuptools >= 78"
    )


def test_installer_uses_combined_license_bundle_and_installs_notice_docs():
    # The paths are rooted at the {#RepoRoot} define rather than written as
    # "..\\LICENSES.md": ISCC does not normalize parent traversal and the dead
    # "\\packaging\\.." segment cost 13 characters of MAX_PATH, which broke the
    # compile from any long checkout (finding F-12). What this test guards is
    # unchanged — that the combined bundle is the shown license and that every
    # notice doc is installed. tests/test_installer_paths.py owns the
    # normalization itself.
    installer = _read("packaging/bananaflow.iss")

    assert "LicenseFile={#RepoRoot}LICENSES.md" in installer
    for filename in (
        "LICENSE",
        "LICENSES.md",
        "NOTICE",
        "SOURCE_OFFER.md",
        "CONTRIBUTING.md",
        "THIRD_PARTY_NOTICES.md",
        "README.md",
    ):
        assert re.search(
            rf'(?m)^Source:\s*"\{{#RepoRoot\}}{re.escape(filename)}"', installer
        ), f"{filename} is no longer installed by the installer"


def test_portable_build_copies_license_source_notice_docs_before_zipping():
    build_script = _read("scripts/build_windows.ps1")

    assert "Copying license/source notice docs into portable folder" in build_script
    assert "Copy-Item" in build_script
    assert "$DistDir $name" in build_script
    for filename in (
        "LICENSE",
        "LICENSES.md",
        "NOTICE",
        "SOURCE_OFFER.md",
        "CONTRIBUTING.md",
        "THIRD_PARTY_NOTICES.md",
        "README.md",
    ):
        assert f"'{filename}'" in build_script


#: The license/source-notice set every conveyed build must carry. Kept in
#: one place so the Windows and macOS assertions can never drift apart.
_RELEASE_NOTICE_DOCS = (
    "LICENSE",
    "LICENSES.md",
    "NOTICE",
    "SOURCE_OFFER.md",
    "CONTRIBUTING.md",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
)

#: The subset placed at the DMG root, visible on mount without installing.
_DMG_ROOT_DOCS = (
    "LICENSE",
    "LICENSES.md",
    "NOTICE",
    "SOURCE_OFFER.md",
    "THIRD_PARTY_NOTICES.md",
)


def test_macos_app_bundle_carries_license_source_notice_docs():
    """BananaFlow is GPL-3.0-or-later and bundles LGPL FFmpeg, Chromium, Deno
    and the bgutil provider, so a conveyed binary must carry the license
    texts and the written source offer.

    scripts/build_windows.ps1 has always enforced this for the portable
    folder and installer. The macOS path did not: packaging/bananaflow.spec
    never listed these docs in ``datas``, and neither the build script nor
    the release workflow copied them, so every shipped .app/DMG went out
    with no LICENSE, NOTICE or SOURCE_OFFER at all.
    """
    for source in ("scripts/build_macos.sh", ".github/workflows/release-macos.yml"):
        text = _read(source)
        assert "license/source notice docs" in text.lower(), (
            f"{source} must stage the license/notice bundle into the .app"
        )
        assert "Contents/Resources" in text
        for filename in _RELEASE_NOTICE_DOCS:
            assert filename in text, f"{source} no longer stages {filename}"


def test_macos_license_docs_are_staged_before_codesigning():
    """Adding files to the bundle after codesign invalidates the signature,
    so the copy must come first in both macOS build paths."""
    for source, sign_marker in (
        ("scripts/build_macos.sh", "codesign --force"),
        (".github/workflows/release-macos.yml", "Ad-hoc codesign the bundle"),
    ):
        text = _read(source)
        docs_at = text.lower().find("license/source notice docs")
        sign_at = text.find(sign_marker)
        assert docs_at != -1 and sign_at != -1, f"could not locate both steps in {source}"
        assert docs_at < sign_at, (
            f"{source} stages the license docs after codesigning, which "
            f"invalidates the signature — stage them before signing"
        )


def test_macos_license_staging_fails_closed_on_a_missing_doc():
    for source, failure in (
        ("scripts/build_macos.sh", "exit 1"),
        (".github/workflows/release-macos.yml", "exit 1"),
    ):
        text = _read(source)
        assert "required release notice doc is missing" in text.lower(), (
            f"{source} must name the missing doc rather than silently skipping it"
        )
        assert failure in text, (
            f"{source} must fail the build on a missing notice doc, not warn"
        )


def test_macos_dmg_root_carries_the_license_bundle():
    """The .app is dragged out of the DMG and the DMG discarded, so the
    in-bundle copy is what survives — but the terms should also be readable
    on mount, before the user installs anything."""
    for source in ("scripts/build_macos.sh", ".github/workflows/release-macos.yml"):
        text = _read(source)
        staging_block = text.split("Applications", 1)[-1].split("hdiutil create", 1)[0]
        for filename in _DMG_ROOT_DOCS:
            assert filename in staging_block, (
                f"{source} does not copy {filename} to the DMG staging root"
            )


def test_packaging_bundles_full_po_provider_backend_inputs():
    build_script = _read("scripts/build_windows.ps1")
    spec = _read("packaging/bananaflow.spec")

    assert 'pip install "bgutil-ytdlp-pot-provider==1.3.1"' in build_script
    assert "Staging PO Token Provider plugin and Deno script backend" in build_script
    assert "PO Token Provider staging failed" in build_script
    assert "_stage_tree(HERE / 'pot-provider-backend', 'pot-provider-backend')" in spec


def test_runtime_components_does_not_claim_bgutil_is_mit():
    runtime_components = _read("core/runtime_components.py")

    assert "bgutil-ytdlp-pot-provider is GPL v3" in runtime_components
    assert not re.search(
        r"bgutil-ytdlp-pot-provider[^\n.]{0,160}(MIT-licensed|MIT licensed|\bMIT\b)",
        runtime_components,
        re.I,
    )


def test_declared_dependencies_are_covered_by_third_party_notices():
    notices = _read("THIRD_PARTY_NOTICES.md").lower().replace("_", "-")
    declared = _requirements_txt_names() | _pyproject_dependency_names()

    missing = sorted(name for name in declared if name not in notices)
    assert not missing, (
        "Declared dependencies must be listed in THIRD_PARTY_NOTICES.md: "
        + ", ".join(missing)
    )


def test_staged_binary_folders_are_gitignored_except_readmes():
    gitignore = _read(".gitignore")
    for pattern in (
        "packaging/yt-dlp-plugins/*",
        "!packaging/yt-dlp-plugins/README.md",
        "packaging/runtime/*",
        "!packaging/runtime/README.md",
        "packaging/pot-provider-backend/*",
        "!packaging/pot-provider-backend/README.md",
        "packaging/pot-provider-cache/",
        "packaging/pot-provider-server/*",
        "!packaging/pot-provider-server/README.md",
        "packaging/ffmpeg/",
    ):
        assert pattern in gitignore

    visible = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "packaging/yt-dlp-plugins",
            "packaging/runtime",
            "packaging/pot-provider-backend",
            "packaging/pot-provider-cache",
            "packaging/pot-provider-server",
            "packaging/ffmpeg",
        ],
        cwd=REPO_ROOT,
        text=True,
    ).splitlines()
    assert set(visible) == {
        "packaging/runtime/README.md",
        "packaging/pot-provider-backend/README.md",
        "packaging/yt-dlp-plugins/README.md",
    }
