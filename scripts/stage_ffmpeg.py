#!/usr/bin/env python3
"""Stage checksum-pinned, license-checked FFmpeg for the selected release target."""

from __future__ import annotations

import argparse
import calendar
import json
import re
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

# This script runs both directly and via import in deterministic tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_ffmpeg_macos as macos

ROOT = Path(__file__).resolve().parent.parent
WINDOWS_PINS = ROOT / "packaging" / "ffmpeg-windows.json"


def load_windows_pin(path: Path = WINDOWS_PINS) -> dict:
    pin = json.loads(path.read_text(encoding="utf-8"))
    if pin.get("schema_version") != 1 or pin.get("target") != "windows-x64":
        raise ValueError("Unsupported Windows FFmpeg manifest")
    match = re.fullmatch(r"autobuild-(\d{4})-(\d{2})-(\d{2})-\d{2}-\d{2}", pin["tag"])
    if not match:
        raise ValueError("Windows FFmpeg requires an exact dated release tag")
    year, month, day = map(int, match.groups())
    if day != calendar.monthrange(year, month)[1]:
        raise ValueError("Use a retained month-end BtbN release, not an expiring daily build")
    if not re.fullmatch(r"N-\d+-g[0-9a-f]+", pin["version"]):
        raise ValueError("Windows FFmpeg version is not pinned")
    if pin["archive"] != f"ffmpeg-{pin['version']}-win64-lgpl.zip":
        raise ValueError("Windows FFmpeg must use the reviewed static x64 LGPL asset")
    expected_url = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{pin['tag']}/{pin['archive']}"
    if pin["url"] != expected_url or not re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]):
        raise ValueError("Windows FFmpeg requires an exact URL and SHA-256")
    return pin


def verify_windows_x64(binary: Path) -> None:
    with binary.open("rb") as handle:
        header = handle.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError(f"{binary.name} is not a Windows executable")
        offset = struct.unpack_from("<I", header, 60)[0]
        handle.seek(offset)
        pe = handle.read(6)
        if len(pe) != 6 or pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664:
            raise ValueError(f"{binary.name} is not an x64 Windows executable")


def extract_windows_archive(archive: Path, destination: Path, pin: dict) -> None:
    """Extract only the two exact executable paths and the upstream license."""
    root = pin["archive"].removesuffix(".zip")
    required = {
        f"{root}/bin/ffmpeg.exe": "ffmpeg.exe",
        f"{root}/bin/ffprobe.exe": "ffprobe.exe",
        f"{root}/LICENSE.txt": "licenses/ffmpeg-LICENSE.txt",
    }
    with zipfile.ZipFile(archive) as handle:
        names = handle.namelist()
        for name, relative in required.items():
            if names.count(name) != 1:
                raise ValueError(f"FFmpeg archive must contain exactly one {name}")
            info = handle.getinfo(name)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"FFmpeg archive member cannot be a symlink: {name}")
            if info.file_size > macos.MAX_EXTRACTED_BYTES:
                raise ValueError(f"FFmpeg archive member exceeds size limit: {name}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def stage_windows(destination: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows FFmpeg staging/verification must run on Windows")
    pin = load_windows_pin()
    with tempfile.TemporaryDirectory(prefix="bananaflow-ffmpeg-") as temp:
        work = Path(temp)
        archive = work / pin["archive"]
        macos.download_verified(pin, archive)
        candidate = work / "candidate"
        extract_windows_archive(archive, candidate, pin)
        evidence = {"target": pin["target"], "input": pin, "binaries": {}}
        for name in ("ffmpeg", "ffprobe"):
            binary = candidate / f"{name}.exe"
            verify_windows_x64(binary)
            version = macos.run([str(binary), "-version"])
            license_text = macos.run([str(binary), "-L"])
            macos.verify_lgpl(version, license_text, name, pin["version"])
            evidence["binaries"][name] = {
                "sha256": macos.sha256(binary), "version": version, "license": license_text,
            }
        smoke = work / "smoke"
        smoke.mkdir()
        macos.smoke_audio(candidate / "ffmpeg.exe", candidate / "ffprobe.exe", smoke)
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            shutil.copy2(candidate / name, destination / name)
        shutil.copytree(candidate / "licenses", destination / "licenses", dirs_exist_ok=True)
        (destination / "ffmpeg-build.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"Verified LGPL FFmpeg {pin['version']} staged in {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows-x64", "macos-arm64"), required=True)
    parser.add_argument("--destination", type=Path, default=ROOT / "packaging" / "ffmpeg")
    args = parser.parse_args()
    try:
        if args.platform == "windows-x64":
            stage_windows(args.destination)
        else:
            macos.build(args.destination, source_bundle=ROOT / "packaging" / "ffmpeg-source-bundle.tar.xz")
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, macos.subprocess.SubprocessError) as error:
        if isinstance(error, macos.subprocess.CalledProcessError) and error.stdout:
            print(error.stdout, file=sys.stderr)
        print(f"FFmpeg staging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
