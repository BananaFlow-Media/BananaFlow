"""Build the reviewed downloader overlay and its channel manifest.

Run inside the same environment prepared from ``requirements.txt`` for a
release build. Only files owned by the installed ``yt-dlp`` and
``yt-dlp-ejs`` distributions are included. The resulting ZIP is pure Python /
JavaScript data and is consumed by ``core.component_overlay``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from version import FULL_VERSION


PACKAGES = ("yt-dlp", "yt-dlp-ejs")


def _distribution_files(name: str) -> tuple[str, list[tuple[Path, PurePosixPath]]]:
    distribution = importlib.metadata.distribution(name)
    version = distribution.version
    files: list[tuple[Path, PurePosixPath]] = []
    for entry in distribution.files or ():
        relative = PurePosixPath(str(entry).replace("\\", "/"))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            continue
        source = Path(distribution.locate_file(entry))
        if source.is_file():
            files.append((source, relative))
    if not files:
        raise RuntimeError(f"Installed distribution {name} has no package files")
    return version, files


def _write_deterministic_zip(destination: Path, files: list[tuple[Path, PurePosixPath]]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, relative in sorted(files, key=lambda item: str(item[1])):
            info = zipfile.ZipInfo(str(relative), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())


def build(output_dir: Path, min_app_version: str, max_app_version: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    versions: dict[str, str] = {}
    owned_files: dict[str, tuple[Path, PurePosixPath]] = {}
    for package in PACKAGES:
        version, files = _distribution_files(package)
        versions[package] = version
        for source, relative in files:
            key = str(relative)
            existing = owned_files.get(key)
            if existing is not None and existing[0].read_bytes() != source.read_bytes():
                raise RuntimeError(f"Distributions disagree about shared file {key}")
            owned_files[key] = (source, relative)

    raw_id = f"yt-dlp-{versions['yt-dlp']}__ejs-{versions['yt-dlp-ejs']}"
    bundle_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw_id)
    bundle_path = output_dir / f"bananaflow-components-{bundle_id}.zip"
    _write_deterministic_zip(bundle_path, list(owned_files.values()))
    bundle_bytes = bundle_path.read_bytes()

    manifest = {
        "schema": 1,
        "channel": "component-channel-v1",
        "bundle_id": bundle_id,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "disabled": False,
        "superseded_by": "",
        "revoked_bundle_ids": [],
        "compatibility": {
            "min_app_version": min_app_version,
            "max_app_version_exclusive": max_app_version,
        },
        "packages": [
            {"name": package, "version": versions[package]} for package in PACKAGES
        ],
        "bundle": {
            "asset": bundle_path.name,
            "size": len(bundle_bytes),
            "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        },
    }
    manifest_path = output_dir / "bananaflow-components.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path, bundle_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-app-version", default=FULL_VERSION)
    parser.add_argument("--max-app-version-exclusive", default="2.0.0")
    args = parser.parse_args()
    manifest, bundle = build(
        args.output, args.min_app_version, args.max_app_version_exclusive,
    )
    print(manifest)
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
