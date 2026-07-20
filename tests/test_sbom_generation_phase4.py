"""Phase 4 (reproducible builds / supply chain) — SBOM generation.

scripts/generate_sbom.py must produce a valid CycloneDX document covering
both the installed Python environment and the pinned native/staged
binaries that importlib.metadata cannot see, plus a human-readable
counterpart. These tests exercise the generator directly (no build
required) and confirm the release build script actually calls it.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
generate_sbom = importlib.import_module("generate_sbom")


def test_native_components_all_have_required_fields():
    for item in generate_sbom._NATIVE_COMPONENTS:
        assert item["name"]
        assert item["version"]
        assert item["license"]
        assert item["source"].startswith("http")
        assert item["purl"]


def test_ffmpeg_and_deno_native_components_carry_sha256_hashes():
    """Everything actually downloaded and verified by a workflow must show
    the same checksum here, so the SBOM cannot silently drift from what a
    release build downloads and verifies."""
    named = {item["name"]: item for item in generate_sbom._NATIVE_COMPONENTS}
    for name in (
        "FFmpeg (Windows, LGPL)", "FFmpeg (macOS, LGPL)",
        "ffprobe (macOS, LGPL)", "Deno",
    ):
        assert name in named, f"missing native component: {name}"
        digest = named[name]["hashes"]["SHA-256"]
        assert len(digest) == 64
        int(digest, 16)  # must be valid hex


def test_pinned_hashes_match_the_windows_release_workflow():
    """The SBOM's pinned FFmpeg checksum must match the one the Windows
    release workflow actually verifies before extracting the archive —
    two independently maintained copies of the same value are exactly how
    a pin quietly goes stale."""
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8",
    )
    named = {item["name"]: item for item in generate_sbom._NATIVE_COMPONENTS}
    ffmpeg_win = named["FFmpeg (Windows, LGPL)"]
    assert ffmpeg_win["hashes"]["SHA-256"] in workflow_text
    # The version string alone (e.g. a stale build tag) would otherwise
    # pass every other assertion here while silently pointing at the
    # wrong FFmpeg build.
    assert ffmpeg_win["version"] in workflow_text


def test_pinned_hashes_match_the_macos_release_workflow():
    workflow_text = (REPO_ROOT / ".github" / "workflows" / "release-macos.yml").read_text(
        encoding="utf-8",
    )
    named = {item["name"]: item for item in generate_sbom._NATIVE_COMPONENTS}
    ffmpeg_mac = named["FFmpeg (macOS, LGPL)"]
    ffprobe_mac = named["ffprobe (macOS, LGPL)"]
    assert ffmpeg_mac["hashes"]["SHA-256"] in workflow_text
    assert ffprobe_mac["hashes"]["SHA-256"] in workflow_text
    assert ffmpeg_mac["version"] in workflow_text
    assert ffprobe_mac["version"] in workflow_text


def test_deno_hash_matches_the_recorded_third_party_notices_value():
    """Unlike the FFmpeg entries (hash of the downloaded .zip), Deno's SBOM
    hash is of the extracted deno.exe — cross-checking it against the
    independently maintained THIRD_PARTY_NOTICES.md evidence is the only
    thing that would have caught it silently going stale or being copied
    from the wrong source (the release .zip's own hash)."""
    notices_text = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    named = {item["name"]: item for item in generate_sbom._NATIVE_COMPONENTS}
    deno = named["Deno"]
    assert deno["hashes"]["SHA-256"] in notices_text
    assert deno["version"] in notices_text
    assert deno.get("hash_note"), (
        "Deno's hash scope (extracted binary, not the release archive) "
        "must stay documented so it is not mistaken for a .zip hash"
    )


def test_python_components_reflect_the_installed_environment():
    components = generate_sbom._installed_python_packages()
    names = {c["name"].lower() for c in components}
    assert len(components) > 20, "the real environment has many more than 20 packages"
    assert "pytest" in names
    for comp in components:
        assert comp["version"] and comp["version"] != "unknown"
        assert comp["purl"].startswith("pkg:pypi/")


def test_build_cyclonedx_produces_a_well_formed_document():
    native = generate_sbom._native_bom_components()
    python_pkgs = generate_sbom._installed_python_packages()[:3]  # keep the test fast
    bom = generate_sbom.build_cyclonedx(native + python_pkgs)

    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.5"
    assert bom["serialNumber"].startswith("urn:uuid:")
    assert bom["metadata"]["component"]["name"] == "bananaflow"
    assert bom["metadata"]["component"]["version"]
    assert len(bom["components"]) == len(native) + len(python_pkgs)


def test_cli_writes_json_and_markdown_outputs(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate_sbom.py"),
         "--out-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr

    json_path = tmp_path / "sbom.cyclonedx.json"
    md_path = tmp_path / "SBOM_INVENTORY.md"
    assert json_path.is_file()
    assert md_path.is_file()

    import json
    bom = json.loads(json_path.read_text(encoding="utf-8"))
    assert bom["bomFormat"] == "CycloneDX"
    assert len(bom["components"]) > len(generate_sbom._NATIVE_COMPONENTS)

    inventory = md_path.read_text(encoding="utf-8")
    assert "FFmpeg (Windows, LGPL)" in inventory
    assert "sbom.cyclonedx.json" in inventory


def test_windows_build_script_generates_the_sbom():
    text = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "scripts\\generate_sbom.py" in text
    assert "SBOM generation failed" in text
