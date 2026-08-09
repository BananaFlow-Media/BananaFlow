"""
scripts/generate_sbom.py  –  CycloneDX SBOM + human-readable inventory
========================================================================
Generates a release SBOM covering:
  * every installed Python package in the active environment (via
    importlib.metadata — stdlib only, no extra build/security tool
    dependency to keep working), and
  * the pinned native/staged binaries that importlib.metadata cannot see
    (FFmpeg/ffprobe, Deno, the bgutil PO Token Provider Python package,
    Playwright's Chromium), sourced from the same pinned version/checksum
    values recorded in THIRD_PARTY_NOTICES.md and the release workflows
    so the SBOM cannot silently drift from what a release actually
    bundles. The bgutil provider's own npm dependency tree
    (packaging/stage_pot_provider.py) is tracked separately by its
    upstream package-lock.json/deno.lock, not itemized here.

Output:
  dist/sbom.cyclonedx.json   – CycloneDX 1.5 JSON, machine-readable
  dist/SBOM_INVENTORY.md     – human-readable table (also embeddable
                               in release evidence)

Usage:
    python scripts/generate_sbom.py [--out-dir dist]

Run this after installing the exact dependency set a release will ship
(same requirements.txt / venv used for the build), so package versions
in the SBOM match the actual artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Native/staged binaries that are not Python packages, so importlib.metadata
# cannot see them. Kept in sync with THIRD_PARTY_NOTICES.md and the pinned
# download steps in .github/workflows/release-windows.yml and
# release-macos.yml — update all three together when a pin changes.
_NATIVE_COMPONENTS = [
    {
        "name": "FFmpeg (Windows, LGPL)",
        "version": "N-125994-gf944afd040",
        "license": "LGPL-2.1-or-later WITH LGPL-3.0-linking-exception",
        "source": "https://github.com/BtbN/FFmpeg-Builds",
        "purl": "pkg:generic/ffmpeg@N-125994-gf944afd040?download_url=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-08-08-13-06/ffmpeg-N-125994-gf944afd040-win64-lgpl.zip",
        "hashes": {"SHA-256": "9df5ca19ba109855856375ad9db8b737bdbc23a3833d1b76d68bcc052c173ac9"},
    },
    {
        "name": "FFmpeg (macOS, LGPL)",
        "version": "8.1.2",
        "license": "LGPL-2.1-or-later WITH LGPL-3.0-linking-exception",
        "source": "https://evermeet.cx/ffmpeg/",
        "purl": "pkg:generic/ffmpeg@8.1.2?download_url=https://evermeet.cx/ffmpeg/ffmpeg-8.1.2.zip",
        "hashes": {"SHA-256": "e91df72a1ee7c26606f90dd2dd4dcccc6a75140ff9ea6fdd50faae828b82ba69"},
    },
    {
        "name": "ffprobe (macOS, LGPL)",
        "version": "8.1.2",
        "license": "LGPL-2.1-or-later WITH LGPL-3.0-linking-exception",
        "source": "https://evermeet.cx/ffmpeg/",
        "purl": "pkg:generic/ffprobe@8.1.2?download_url=https://evermeet.cx/ffmpeg/ffprobe-8.1.2.zip",
        "hashes": {"SHA-256": "399b93f0b9862f69767afa343e90c2f48d7e7958cadbb6deb76a012d0e3b7ce3"},
    },
    {
        "name": "Deno",
        "version": "2.9.1",
        "license": "MIT",
        "source": "https://github.com/denoland/deno",
        "purl": "pkg:github/denoland/deno@v2.9.1",
        # Unlike the FFmpeg entries above (hash of the downloaded .zip),
        # this is the SHA-256 of the extracted deno.exe itself, matching
        # THIRD_PARTY_NOTICES.md's recorded evidence — not the release
        # .zip's own hash, which scripts/fetch_deno_runtime.ps1 verifies
        # separately (against the official .sha256sum asset) before
        # extraction. hash_note carries this distinction into the
        # human-readable inventory; it is not a CycloneDX field.
        "hashes": {"SHA-256": "3819117e301d48a6931f9a1a4fb5e4a10c464163189bcff8fce5d75025d6f2a0"},
        "hash_note": "of extracted deno.exe, not the release .zip",
    },
    {
        "name": "bgutil-ytdlp-pot-provider",
        "version": "1.3.1",
        "license": "GPL-3.0-or-later",
        "source": "https://github.com/Brainicism/bgutil-ytdlp-pot-provider",
        "purl": "pkg:pypi/bgutil-ytdlp-pot-provider@1.3.1",
    },
    {
        "name": "Playwright Chromium (bundled)",
        "version": "see packaging build log for the exact Chromium revision "
                   "installed by `python -m playwright install chromium`",
        "license": "BSD-3-Clause (Chromium) plus component licenses",
        "source": "https://playwright.dev/",
        "purl": "pkg:generic/chromium",
    },
]


def _installed_python_packages() -> list[dict]:
    components = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.name.lower()):
        name = dist.metadata.get("Name") or dist.name
        version = dist.version or "unknown"
        license_expr = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        if not license_expr:
            classifiers = dist.metadata.get_all("Classifier") or []
            licenses = [c.split("::")[-1].strip() for c in classifiers if c.startswith("License")]
            license_expr = "; ".join(licenses) or "UNKNOWN"
        home_page = dist.metadata.get("Home-page") or dist.metadata.get("Project-URL") or ""
        components.append({
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name.lower()}@{version}",
            "licenses": [{"license": {"name": license_expr}}] if license_expr != "UNKNOWN" else [],
            "externalReferences": (
                [{"type": "website", "url": home_page}] if home_page else []
            ),
        })
    return components


def _native_bom_components() -> list[dict]:
    components = []
    for item in _NATIVE_COMPONENTS:
        comp = {
            "type": "library",
            "name": item["name"],
            "version": item["version"],
            "purl": item["purl"],
            "licenses": [{"license": {"name": item["license"]}}],
            "externalReferences": [{"type": "distribution", "url": item["source"]}],
        }
        if "hashes" in item:
            comp["hashes"] = [
                {"alg": alg, "content": digest} for alg, digest in item["hashes"].items()
            ]
        components.append(comp)
    return components


def build_cyclonedx(components: list[dict]) -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {
                "type": "application",
                "name": "bananaflow",
                "version": _read_app_version(),
            },
        },
        "components": components,
    }


def _read_app_version() -> str:
    sys.path.insert(0, str(REPO_ROOT))
    from version import FULL_VERSION  # noqa: PLC0415
    return FULL_VERSION


def write_human_readable(components: list[dict], native: list[dict], out_path: Path) -> None:
    lines = [
        "# SBOM Inventory (human-readable)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Machine-readable counterpart: `sbom.cyclonedx.json` (CycloneDX 1.5).",
        "",
        "## Native / staged binaries",
        "",
        "| Component | Version | License | Source | SHA-256 |",
        "|---|---|---|---|---|",
    ]
    for item in _NATIVE_COMPONENTS:
        digest = next(iter(item.get("hashes", {}).values()), "—")
        if digest != "—" and item.get("hash_note"):
            digest = f"{digest} ({item['hash_note']})"
        lines.append(
            f"| {item['name']} | {item['version']} | {item['license']} | "
            f"{item['source']} | {digest} |"
        )
    lines += [
        "",
        "## Python packages (installed environment)",
        "",
        "| Package | Version | License |",
        "|---|---|---|",
    ]
    for comp in components:
        lic = comp["licenses"][0]["license"]["name"] if comp["licenses"] else "UNKNOWN"
        lines.append(f"| {comp['name']} | {comp['version']} | {lic} |")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="dist", help="output directory (default: dist)")
    args = parser.parse_args()

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    python_components = _installed_python_packages()
    native_components = _native_bom_components()
    bom = build_cyclonedx(native_components + python_components)

    json_path = out_dir / "sbom.cyclonedx.json"
    json_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    md_path = out_dir / "SBOM_INVENTORY.md"
    write_human_readable(python_components, native_components, md_path)

    print(f"Wrote {json_path} ({len(bom['components'])} components)")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
