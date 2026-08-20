"""
tests/test_dependency_versions.py  –  Dependency-version drift guard
========================================================================
pyproject.toml defines BananaFlow's oldest supported/safe yt-dlp source
version. requirements.txt is the reproducible application/release install and
may deliberately pin a newer reviewed nightly. YouTube Doctor must never call
a known-vulnerable version healthy and must remain compatible with the source
floor.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_yt_dlp_requirement(text: str) -> tuple[str, str]:
    match = re.search(
        r'yt-dlp\[default\](==|>=)(\S+?)(?:[\s"#,]|$)',
        text,
    )
    assert match, "could not find a yt-dlp[default] ==/>= requirement"
    return match.group(1), match.group(2)


def _calver_date(version: str) -> tuple[int, int, int]:
    """Compare the date portion of yt-dlp CalVer, including nightly strings."""
    parts = re.findall(r"\d+", version or "")
    assert len(parts) >= 3, f"invalid yt-dlp CalVer: {version!r}"
    return int(parts[0]), int(parts[1]), int(parts[2])


def test_pyproject_is_safe_floor_and_release_pin_is_not_older():
    requirements_text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    requirements_spec = _extract_yt_dlp_requirement(requirements_text)
    pyproject_spec = _extract_yt_dlp_requirement(pyproject_text)

    assert pyproject_spec[0] == ">=", (
        "pyproject.toml must express the safe source compatibility floor"
    )
    assert requirements_spec[0] == "==", (
        "requirements.txt must pin the reviewed yt-dlp used by release builds"
    )
    assert _calver_date(requirements_spec[1]) >= _calver_date(pyproject_spec[1]), (
        f"release pin {requirements_spec[1]} is older than source floor {pyproject_spec[1]}"
    )


def test_source_floor_is_at_least_security_patched_2026_7_4():
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    _operator, floor_version = _extract_yt_dlp_requirement(pyproject_text)

    assert _calver_date(floor_version) >= (2026, 7, 4), (
        "BananaFlow must not support a yt-dlp baseline older than 2026.7.4, "
        "the first patched release for GHSA-6v4j-43gg-vj32."
    )


def test_youtube_doctor_minimum_is_secure_and_not_newer_than_source_floor():
    from core.youtube_doctor import MIN_YT_DLP_VERSION

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    _operator, floor_version = _extract_yt_dlp_requirement(pyproject_text)

    assert _calver_date(MIN_YT_DLP_VERSION) >= (2026, 7, 4)
    assert _calver_date(MIN_YT_DLP_VERSION) <= _calver_date(floor_version), (
        f"YouTube Doctor requires {MIN_YT_DLP_VERSION}, which is newer than "
        f"the source compatibility floor {floor_version}."
    )


def test_pot_provider_is_not_a_source_tree_hard_dependency():
    """bgutil-ytdlp-pot-provider is GPL v3 (verified against its PyPI
    classifier). BananaFlow itself is GPL-3.0-or-later, so this is not a
    license-compatibility concern for the GPL release. The source tree
    keeps it in pyproject.toml's ``po-token`` extra because the public
    Windows package stages the pinned provider plugin and its upstream
    Deno script backend explicitly during packaging. See
    THIRD_PARTY_NOTICES.md for the license notice and source notes."""
    requirements_text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    requirement_lines = [
        line for line in requirements_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("bgutil-ytdlp-pot-provider" in line for line in requirement_lines), (
        "bgutil-ytdlp-pot-provider must not be an active line in "
        "requirements.txt; the Windows packaging script stages the pinned "
        "provider stack explicitly."
    )

    dependencies_block = re.search(
        r"^dependencies\s*=\s*\[(.*?)^\]", pyproject_text, re.S | re.M,
    )
    assert dependencies_block, "could not find pyproject.toml's [project] dependencies list"
    assert "bgutil-ytdlp-pot-provider" not in dependencies_block.group(1), (
        "bgutil-ytdlp-pot-provider must not be in pyproject.toml's "
        "unconditional dependencies list; keep source/venv opt-in separate "
        "from the packaged Windows provider stack."
    )

    po_token_block = re.search(
        r'^po-token\s*=\s*\[(.*?)^\]', pyproject_text, re.S | re.M,
    )
    assert po_token_block, "pyproject.toml must define a [project.optional-dependencies] po-token extra"
    assert "bgutil-ytdlp-pot-provider" in po_token_block.group(1)


def test_pot_provider_version_pin_matches_the_staging_script():
    """pyproject.toml's po-token extra and packaging/stage_pot_provider.py
    must pin the same provider version."""
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'bgutil-ytdlp-pot-provider==(\S+?)"', pyproject_text)
    assert match, "could not find pyproject.toml's bgutil-ytdlp-pot-provider== pin"
    pyproject_version = match.group(1)

    stage_script_text = (REPO_ROOT / "packaging" / "stage_pot_provider.py").read_text(encoding="utf-8")
    match = re.search(r'PROVIDER_VERSION\s*=\s*"([^"]+)"', stage_script_text)
    assert match, "could not find packaging/stage_pot_provider.py's PROVIDER_VERSION"
    stage_script_version = match.group(1)

    assert pyproject_version == stage_script_version, (
        f"pyproject.toml pins bgutil-ytdlp-pot-provider=={pyproject_version} but "
        f"packaging/stage_pot_provider.py's PROVIDER_VERSION={stage_script_version!r} — "
        "keep them in sync."
    )
