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


def _staged_ffmpeg(tmp_path, target):
    import json
    stage = generate_sbom.stage_ffmpeg
    extension = ".exe" if target == "windows-x64" else ""
    evidence = {"target": target, "binaries": {}}
    if target == "windows-x64":
        pin = stage.load_windows_pin()
        evidence["input"] = pin
        version = pin["version"]
        configuration = "--enable-version3 --disable-gpl"
    else:
        pins = stage.macos.load_pins()
        evidence["sources"] = pins["sources"]
        evidence["source_manifest_sha256"] = stage.macos.sha256(stage.macos.PINS)
        evidence["build_recipe_sha256"] = stage.macos.sha256(Path(stage.macos.__file__))
        version = next(s["version"] for s in pins["sources"] if s["name"] == "ffmpeg")
        configuration = "--disable-gpl --disable-nonfree"
    for name in ("ffmpeg", "ffprobe"):
        path = tmp_path / f"{name}{extension}"
        path.write_bytes(name.encode())
        evidence["binaries"][name] = {
            "sha256": stage.macos.sha256(path),
            "version": f"{name} version {version}\nconfiguration: {configuration}",
            "license": "GNU Lesser General Public License",
        }
    (tmp_path / "ffmpeg-build.json").write_text(json.dumps(evidence), encoding="utf-8")
    return evidence


def test_windows_inventory_uses_manifest_and_actual_binary_hashes(tmp_path):
    _staged_ffmpeg(tmp_path, "windows-x64")
    components = generate_sbom._native_bom_components("windows-x64", tmp_path)
    names = {item["name"] for item in components}
    assert not any("macOS" in name or "macos-arm64" in name for name in names)
    binaries = [c for c in components if c["name"] in {"ffmpeg (windows-x64, LGPL)", "ffprobe (windows-x64, LGPL)"}]
    assert len(binaries) == 2
    for binary in binaries:
        tool = binary["name"].split()[0]
        assert binary["hashes"][0]["content"] == generate_sbom.stage_ffmpeg.macos.sha256(tmp_path / f"{tool}.exe")
        assert binary["licenses"] == [{"license": {"name": "LGPL-3.0-or-later"}}]
        assert "before packaging/signing" in binary["properties"][0]["value"]
    archive = next(c for c in components if c["name"] == "FFmpeg Windows upstream archive")
    pin = generate_sbom.stage_ffmpeg.load_windows_pin()
    assert archive["version"] == pin["version"]
    assert archive["hashes"][0]["content"] == pin["sha256"]
    assert archive["externalReferences"][0]["url"] == pin["url"]


def test_explicit_no_ffmpeg_does_not_inventory_stale_staged_binaries(tmp_path, monkeypatch):
    _staged_ffmpeg(tmp_path, "macos-arm64")
    monkeypatch.setenv("BANANAFLOW_BUNDLE_FFMPEG", "0")
    components = generate_sbom._native_bom_components("macos-arm64", tmp_path)
    assert not any("ffmpeg" in c["name"].lower() or "ffprobe" in c["name"].lower() for c in components)
    spec = (REPO_ROOT / "packaging/bananaflow.spec").read_text(encoding="utf-8")
    assert 'os.environ.get("BANANAFLOW_BUNDLE_FFMPEG") != "0"' in spec


def test_macos_inventory_includes_exact_source_dependencies_not_intel_assets(tmp_path):
    _staged_ffmpeg(tmp_path, "macos-arm64")
    components = generate_sbom._native_bom_components("macos-arm64", tmp_path)
    names = {c["name"] for c in components}
    for source in generate_sbom.stage_ffmpeg.macos.load_pins()["sources"]:
        component = next(c for c in components if c["name"] == source["name"] + " (macOS source)")
        assert component["version"] == source["version"]
        assert component["hashes"][0]["content"] == source["sha256"]
        assert component["externalReferences"][0]["url"] == source["url"]
    assert "Deno" not in names
    assert "bgutil-ytdlp-pot-provider" not in names
    assert "ffmpeg (macos-arm64, LGPL)" in names
    assert "ffprobe (macos-arm64, LGPL)" in names
    assert "evermeet" not in str(components)
    assert "linking-exception" not in str(components)


def test_missing_staging_does_not_invent_native_ffmpeg(tmp_path):
    for target in ("macos-arm64", "windows-x64", "linux"):
        components = generate_sbom._native_bom_components(target, tmp_path)
        assert not any("ffmpeg" in c["name"].lower() or "ffprobe" in c["name"].lower() for c in components)


def test_staged_binary_without_evidence_is_refused(tmp_path):
    (tmp_path / "ffmpeg").write_bytes(b"unverified")
    with pytest.raises(OSError):
        generate_sbom._native_bom_components("macos-arm64", tmp_path)


def test_staged_binary_changed_after_verification_is_refused(tmp_path):
    _staged_ffmpeg(tmp_path, "macos-arm64")
    (tmp_path / "ffprobe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed after"):
        generate_sbom._native_bom_components("macos-arm64", tmp_path)


def test_stale_staging_source_pin_is_refused(tmp_path):
    import json
    evidence = _staged_ffmpeg(tmp_path, "windows-x64")
    evidence["input"]["sha256"] = "0" * 64
    (tmp_path / "ffmpeg-build.json").write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError, match="Windows release pin"):
        generate_sbom._native_bom_components("windows-x64", tmp_path)


def test_human_inventory_uses_the_supplied_platform_components(tmp_path):
    _staged_ffmpeg(tmp_path, "macos-arm64")
    native = generate_sbom._native_bom_components("macos-arm64", tmp_path)
    path = tmp_path / "inventory.md"
    generate_sbom.write_human_readable([], native, path)
    text = path.read_text()
    assert "ffmpeg (macos-arm64, LGPL)" in text
    assert "verified source archive" in text
    assert "before packaging/signing" in text
    assert "Windows" not in text
    assert "Deno" not in text


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
    assert "Native / staged binaries" in inventory
    if sys.platform not in {"win32", "darwin"}:
        assert "FFmpeg (Windows" not in inventory
    assert "sbom.cyclonedx.json" in inventory


def test_windows_build_script_generates_the_sbom():
    text = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "scripts\\generate_sbom.py" in text
    assert "SBOM generation failed" in text
