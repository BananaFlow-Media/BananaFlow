"""
scripts/generate_sbom.py  –  CycloneDX SBOM + human-readable inventory
========================================================================
Generates a release SBOM covering:
  * every installed Python package in the active environment (via
    importlib.metadata — stdlib only, no extra build/security tool
    dependency to keep working), and
  * the pinned native/staged binaries that importlib.metadata cannot see
    (FFmpeg/ffprobe, Deno, the bgutil PO Token Provider Python package,
    Playwright's Chromium). FFmpeg uses the selected platform's authoritative
    pin manifests and verified staging evidence; binary and source/archive
    hash scopes are explicit and no opposite-platform FFmpeg is invented. The bgutil provider's own npm dependency tree
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
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from urllib.parse import quote, urlencode

import stage_ffmpeg

REPO_ROOT = Path(__file__).resolve().parent.parent

# Non-FFmpeg components retain their existing inventory policy. FFmpeg itself
# is derived below from the selected target's authoritative manifest and the
# verified staging evidence, never copied from another platform's workflow.
_NATIVE_COMPONENTS = [
    {
        "name": "Deno",
        "license": "MIT",
        "source": "https://github.com/denoland/deno",
        # Version comes from the fetch script and the executable hash is
        # computed from staging, not copied from an old release's notices.
    },
    {
        "name": "bgutil-ytdlp-pot-provider",
        "version": "1.3.2",
        "license": "GPL-3.0-or-later",
        "source": "https://github.com/Brainicism/bgutil-ytdlp-pot-provider",
        "purl": "pkg:pypi/bgutil-ytdlp-pot-provider@1.3.2",
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


def _ffmpeg_items(target: str | None, staging_dir: Path) -> list[dict]:
    if target not in {"windows-x64", "macos-arm64"}:
        return []
    extension = ".exe" if target == "windows-x64" else ""
    paths = {name: staging_dir / f"{name}{extension}" for name in ("ffmpeg", "ffprobe")}
    if not any(path.exists() for path in paths.values()):
        return []
    evidence = json.loads((staging_dir / "ffmpeg-build.json").read_text(encoding="utf-8"))
    if evidence["target"] != target:
        raise ValueError("FFmpeg staging evidence belongs to another platform")
    if target == "windows-x64":
        pin = stage_ffmpeg.load_windows_pin()
        if evidence["input"] != pin:
            raise ValueError("FFmpeg staging evidence does not match the Windows release pin")
        version = pin["version"]
        source_url = "https://github.com/BtbN/FFmpeg-Builds"
        inputs = [{
            "name": "FFmpeg Windows upstream archive", "version": version,
            "source": pin["url"], "hashes": {"SHA-256": pin["sha256"]},
            "hash_note": "verified upstream binary archive, not an executable hash",
            "purl": "pkg:generic/ffmpeg@" + quote(version, safe="") + "?" + urlencode({"download_url": pin["url"]}),
        }]
    else:
        pins = stage_ffmpeg.macos.load_pins()
        if (
            evidence["sources"] != pins["sources"]
            or evidence["source_manifest_sha256"] != stage_ffmpeg.macos.sha256(stage_ffmpeg.macos.PINS)
            or evidence["build_recipe_sha256"] != stage_ffmpeg.macos.sha256(Path(stage_ffmpeg.macos.__file__))
        ):
            raise ValueError("FFmpeg staging evidence does not match the macOS source pins")
        version = next(s["version"] for s in pins["sources"] if s["name"] == "ffmpeg")
        source_url = "https://ffmpeg.org/"
        inputs = [{
            "name": source["name"] + " (macOS source)", "version": source["version"],
            "license": source["license"], "source": source["url"],
            "purl": f"pkg:generic/{source['name']}@{quote(source['version'], safe='')}?" + urlencode({"download_url": source["url"]}),
            "hashes": {"SHA-256": source["sha256"]},
            "hash_note": "verified source archive, not a built executable hash",
        } for source in pins["sources"]]
    items = []
    for name, path in paths.items():
        binary = evidence["binaries"][name]
        actual = stage_ffmpeg.macos.sha256(path)
        if actual != binary["sha256"]:
            raise ValueError(f"{name} changed after verified FFmpeg staging")
        stage_ffmpeg.macos.verify_lgpl(binary["version"], binary["license"], name, version)
        license_id = "LGPL-3.0-or-later" if "--enable-version3" in binary["version"] else "LGPL-2.1-or-later"
        if target == "windows-x64":
            inputs[0]["license"] = license_id
        items.append({
            "type": "application", "name": f"{name} ({target}, LGPL)",
            "version": version, "license": license_id, "source": source_url,
            "purl": f"pkg:generic/{name}@{quote(version, safe='')}?" + urlencode({"target": target}),
            "hashes": {"SHA-256": actual},
            "hash_note": "verified staged executable, before packaging/signing",
        })
    return items + inputs


def _native_items(target: str | None = None, staging_dir: Path | None = None) -> list[dict]:
    if target is None:
        target = {"win32": "windows-x64", "darwin": "macos-arm64"}.get(sys.platform)
    items = ([] if os.environ.get("BANANAFLOW_BUNDLE_FFMPEG") == "0" else
             _ffmpeg_items(target, staging_dir or REPO_ROOT / "packaging" / "ffmpeg"))
    for original in _NATIVE_COMPONENTS:
        item = dict(original)
        if item["name"] in {"Deno", "bgutil-ytdlp-pot-provider"} and target != "windows-x64":
            continue
        if item["name"] == "Deno":
            binary = REPO_ROOT / "packaging" / "runtime" / "deno.exe"
            if not binary.is_file():
                continue
            fetch = (REPO_ROOT / "scripts" / "fetch_deno_runtime.ps1").read_text(encoding="utf-8")
            version_match = re.search(r"\$Version\s*=\s*'([^']+)'", fetch)
            if not version_match:
                raise ValueError("Deno fetch script has no default version pin")
            item["version"] = version_match.group(1)
            item["purl"] = "pkg:github/denoland/deno@v" + item["version"]
            item["hashes"] = {"SHA-256": stage_ffmpeg.macos.sha256(binary)}
            item["hash_note"] = "actual staged deno.exe, not the upstream archive"
        items.append(item)
    return items


def _native_bom_components(target: str | None = None, staging_dir: Path | None = None) -> list[dict]:
    components = []
    for item in _native_items(target, staging_dir):
        comp = {
            "type": item.get("type", "library"),
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
        if item.get("hash_note"):
            comp["properties"] = [{"name": "bananaflow:hash-scope", "value": item["hash_note"]}]
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
    for item in native:
        digest = next((h["content"] for h in item.get("hashes", []) if h["alg"] == "SHA-256"), "—")
        hash_note = next((p["value"] for p in item.get("properties", []) if p["name"] == "bananaflow:hash-scope"), "")
        if digest != "—" and hash_note:
            digest = f"{digest} ({hash_note})"
        license_name = item["licenses"][0]["license"]["name"]
        source = item["externalReferences"][0]["url"]
        lines.append(
            f"| {item['name']} | {item['version']} | {license_name} | "
            f"{source} | {digest} |"
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
    try:
        native_components = _native_bom_components()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"SBOM refuses unverified native staging: {error}", file=sys.stderr)
        return 1
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
