"""
tests/test_dependency_versions.py  –  Dependency-version drift guard
========================================================================
requirements.txt, pyproject.toml, and core.youtube_doctor.MIN_YT_DLP_VERSION
must all agree on the minimum yt-dlp version. Phase 3.1 fixed a real
drift where pyproject.toml had been left at the pre-phase-1 2026.3.13
while requirements.txt had already moved to 2026.6.9 — this test exists
so that regresses silently again.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_yt_dlp_min_version(text: str) -> str:
    match = re.search(r'yt-dlp\[default\]>=(\S+?)(?:[\s"#,]|$)', text)
    assert match, "could not find a yt-dlp[default]>=X.Y.Z pin"
    return match.group(1)


def test_requirements_and_pyproject_agree_on_yt_dlp_minimum():
    requirements_text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    requirements_version = _extract_yt_dlp_min_version(requirements_text)
    pyproject_version = _extract_yt_dlp_min_version(pyproject_text)

    assert requirements_version == pyproject_version, (
        f"requirements.txt pins yt-dlp[default]>={requirements_version} but "
        f"pyproject.toml pins yt-dlp[default]>={pyproject_version} — keep them in sync."
    )


def test_youtube_doctor_minimum_agrees_with_requirements():
    from core.youtube_doctor import MIN_YT_DLP_VERSION

    requirements_text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    requirements_version = _extract_yt_dlp_min_version(requirements_text)

    assert MIN_YT_DLP_VERSION == requirements_version, (
        f"core.youtube_doctor.MIN_YT_DLP_VERSION={MIN_YT_DLP_VERSION!r} no longer "
        f"matches requirements.txt's yt-dlp[default]>={requirements_version} pin."
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

    # Only real (non-comment) requirement lines matter — the package name
    # legitimately appears in an explanatory comment about why it's absent.
    requirement_lines = [
        line for line in requirements_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("bgutil-ytdlp-pot-provider" in line for line in requirement_lines), (
        "bgutil-ytdlp-pot-provider must not be an active line in "
        "requirements.txt; the Windows packaging script stages the pinned "
        "provider stack explicitly."
    )

    # Only the po-token optional-dependencies group may reference it.
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
    """pyproject.toml's po-token extra and
    packaging/stage_pot_provider.py both hardcode the bgutil-ytdlp-pot-
    provider version independently (the packaging script downloads its
    own pinned source archive rather than reading it from an installed
    package). Nothing else cross-checked them (issue #30)."""
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
        f"packaging/stage_pot_provider.py's PROVIDER_VERSION={stage_script_version!r} "
        f"— keep them in sync (the staged plugin/backend must match what a "
        f"source/venv install of the po-token extra provides)."
    )
