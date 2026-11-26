#!/usr/bin/env python3
"""Assemble reviewed update assets for a draft in the official repository."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.app_updates import AppUpdateSupport, _asset_name
from core.tool_updates import MAX_TOOL_BYTES, TOOLS, _executable_names, _healthcheck, _platform_key
from version import FULL_VERSION, MAINTENANCE_REVISION


def _write_checksums(output: Path, files: list[Path]) -> None:
    rows = []
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        rows.append(f"{digest.hexdigest()}  {path.name}")
    (output / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="ascii")


def _maintenance(source: Path, output: Path) -> list[Path]:
    if not 1 <= MAINTENANCE_REVISION <= 999999:
        raise ValueError("Build a numbered maintenance revision before preparing assets.")
    artifacts = []
    for kind in ("windows-installed", "windows-portable", "macos-app"):
        support = AppUpdateSupport(True, kind=kind)
        artifact = source / _asset_name(FULL_VERSION, support)
        if artifact.is_file():
            artifacts.append((artifact, output / _asset_name(FULL_VERSION, support, MAINTENANCE_REVISION)))
    if not artifacts:
        raise ValueError("No matching native CI artifacts were found.")
    for artifact, destination in artifacts:
        with artifact.open("rb") as data, destination.open("xb") as copy:
            shutil.copyfileobj(data, copy, length=1024 * 1024)
    return [destination for _, destination in artifacts]


def _runtime(tool: str, revision: int, source: Path, output: Path, license_file: Path) -> list[Path]:
    target = _platform_key()
    if not target or not 1 <= revision <= 999999:
        raise ValueError("Build on a supported native platform and provide a positive tool revision.")
    names = _executable_names(tool)
    inputs = [(source / name, name) for name in names] + [(license_file, "LICENSE")]
    entries = [path for path, _ in inputs]
    if not all(path.is_file() and not path.is_symlink() and path.stat().st_size > 0 for path in entries):
        raise ValueError("The native tools and their license must be regular, nonempty files.")
    if sum(path.stat().st_size for path in entries) > MAX_TOOL_BYTES:
        raise ValueError("The runtime assets exceed the update size limit.")
    _healthcheck(source, names)
    path = output / f"bananaflow-{tool}-{target}-r{revision}.zip"
    with zipfile.ZipFile(path, mode="x", compression=zipfile.ZIP_DEFLATED) as archive:
        for file, name in inputs:
            archive.write(file, name)
    if path.stat().st_size > MAX_TOOL_BYTES:
        raise ValueError("The compressed runtime assets exceed the update size limit.")
    return [path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="kind", required=True)
    for name in ("maintenance", "runtime"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--source", type=Path, required=True)
        subparser.add_argument("--output", type=Path, required=True)
        if name == "runtime":
            subparser.add_argument("--tool", choices=TOOLS, required=True)
            subparser.add_argument("--revision", type=int, required=True)
            subparser.add_argument("--license", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("The output directory must be new.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent, prefix="bananaflow-assets-") as temporary:
        prepared = Path(temporary) / "assets"
        prepared.mkdir()
        if args.kind == "maintenance":
            files = _maintenance(args.source, prepared)
            tag = f"maintenance-v{FULL_VERSION}"
        else:
            files = _runtime(args.tool, args.revision, args.source.resolve(), prepared, args.license)
            tag = f"runtime-channel-v{FULL_VERSION}-r{MAINTENANCE_REVISION}"
        _write_checksums(prepared, files)
        prepared.rename(args.output)
    print(f"Draft tag: {tag}")


if __name__ == "__main__":
    main()
