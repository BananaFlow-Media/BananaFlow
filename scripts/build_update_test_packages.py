"""Build two real updater-capable packages, never a tag or public release.

The old package uses a synthetic 0.0.0 version. The target uses a synthetic
next patch or an increased revision of the same version for maintenance.
It proves the first updater's old-process -> new-process path;
it must not be confused with migration from a previously published binary.
The separate published-baseline smoke covers that compatibility boundary.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VERSION = "0.0.0"


def fixture_source(original: bytes, version: str, revision: int = 0) -> bytes:
    numbers = tuple(int(part) for part in version.split("."))
    if len(numbers) != 3:
        raise ValueError("A fixture needs a three-part numeric version")
    runtime_dir = f"_internal-maintenance-{version}-r{revision}" if revision else "_internal"
    return original + (
        '\n# Synthetic packaged-updater CI fixture; never publish.\n'
        f'__version__ = "{version}"\n'
        f'VERSION_INFO = {numbers!r}\n'
        'PRERELEASE = None\n'
        'FULL_VERSION = __version__\n'
        'PEP440_VERSION = __version__\n'
        f'MAINTENANCE_REVISION = {revision}\n'
        f'WINDOWS_RUNTIME_DIR = {runtime_dir!r}\n'
    ).encode("utf-8")


def command(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def build() -> None:
    if sys.platform == "win32":
        command("pwsh", "-NoProfile", "-File", "scripts/build_windows.ps1",
                "-RequireBundledFfmpeg", "-SkipTests")
        iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        command(iscc, "packaging/bananaflow.iss")
    elif sys.platform == "darwin":
        command("bash", "scripts/build_macos.sh", "--skip-tests")
    else:
        raise RuntimeError("Run this validation on Windows or Apple Silicon macOS")


def main(argv: tuple[str, ...] | list[str] = ()) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maintenance", action="store_true")
    args = parser.parse_args(argv)
    source = ROOT / "version.py"
    original = source.read_bytes()
    versions = {}
    exec(compile(original, str(source), "exec"), versions)
    source_version = versions["FULL_VERSION"]
    major, minor, patch = (int(part) for part in source_version.split("-")[0].split("."))
    target_version = FIXTURE_VERSION if args.maintenance else f"{major}.{minor}.{patch + 1}"
    target_revision = 1 if args.maintenance else 0
    if source_version == FIXTURE_VERSION:
        raise RuntimeError("The target version must differ from the synthetic baseline")
    fixture_dir = ROOT / "update-fixtures"
    if fixture_dir.exists():
        raise RuntimeError("Refusing to overwrite an existing update-fixtures directory")
    fixture_dir.mkdir()
    try:
        # The fixture version exists only during the first local CI build. All
        # four forms remain consistent; no application override is introduced.
        source.write_bytes(fixture_source(original, FIXTURE_VERSION))
        # Python's timestamp bytecode cache may otherwise hide a quick rewrite.
        shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)
        build()
        shutil.move(str(ROOT / "dist"), str(fixture_dir / "old"))
    finally:
        source.write_bytes(original)
        shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)
    try:
        # Current public releases can have the same version as this PR's source.
        # A CI-only next-patch target exercises strict upgrade/version checks
        # without changing the repository's product version or publishing it.
        source.write_bytes(fixture_source(original, target_version, target_revision))
        shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)
        build()
        if target_revision:
            prefix = f"BananaFlow-v{target_version}-"
            for artifact in (ROOT / "dist").glob(prefix + "*"):
                if artifact.suffix.lower() in {".zip", ".exe", ".dmg"}:
                    artifact.rename(artifact.with_name(
                        artifact.name.replace(prefix, f"{prefix}maintenance-r{target_revision}-", 1),
                    ))
    finally:
        source.write_bytes(original)
        shutil.rmtree(ROOT / "__pycache__", ignore_errors=True)
    (fixture_dir / "versions.json").write_text(json.dumps({
        "old": FIXTURE_VERSION,
        "new": target_version,
        "new_maintenance_revision": target_revision,
        "source_version": source_version,
        "target_version_is_synthetic": True,
        "baseline_type": "synthetic-updater-capable-package",
        "source_commit": os.environ.get("GITHUB_SHA", ""),
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
