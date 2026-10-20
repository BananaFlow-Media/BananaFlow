#!/usr/bin/env python3
"""Build native LGPL FFmpeg/ffprobe from verified, versioned upstream sources.

This is the shared CI/local macOS staging path. It never downloads third-party
macOS executables or trusts previously staged binaries. The build is native,
not a Rosetta cross-build. Only pinned static codec libraries and macOS system
libraries may be linked. Run on Apple Silicon with Xcode command-line tools
and pkg-config installed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PINS = ROOT / "packaging" / "ffmpeg-macos-sources.json"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
SOURCE_NAMES = {"ffmpeg", "lame", "opus", "libogg", "libvorbis"}
REQUIRED_ENCODERS = {
    "mp3": ("libmp3lame", "mp3"),
    "m4a": ("aac", "aac"),
    "flac": ("flac", "flac"),
    "opus": ("libopus", "opus"),
    "ogg": ("libvorbis", "vorbis"),
    "wav": ("pcm_s16le", "pcm_s16le"),
}


def load_pins(path: Path = PINS) -> dict:
    pins = json.loads(path.read_text(encoding="utf-8"))
    if pins.get("schema_version") != 1 or pins.get("target") != "macos-arm64":
        raise ValueError("Unsupported FFmpeg source manifest")
    sources = pins["sources"]
    if len(sources) != len(SOURCE_NAMES) or {s["name"] for s in sources} != SOURCE_NAMES:
        raise ValueError("The reviewed FFmpeg source set must be complete and unique")
    for source in sources:
        name, version = source["name"], source["version"]
        archive = source["archive"]
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            raise ValueError(f"Unpinned version for {name}")
        if archive not in (f"{name}-{version}.tar.xz", f"{name}-{version}.tar.gz"):
            raise ValueError(f"Unexpected source archive for {name}")
        url = urllib.parse.urlsplit(source["url"])
        if (
            url.scheme != "https" or url.query or url.fragment or url.username
            or url.hostname not in {"ffmpeg.org", "downloads.sourceforge.net", "downloads.xiph.org"}
            or not url.path.endswith("/" + archive)
        ):
            raise ValueError(f"Unapproved source URL for {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError(f"Missing pinned SHA-256 for {name}")
        if not source["license_files"] or any(
            Path(f).name != f or f in {".", ".."} for f in source["license_files"]
        ):
            raise ValueError(f"Unsafe license filename for {name}")
    return pins


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"FFmpeg source checksum mismatch for {path.name}: expected {expected}, actual {actual}")


def download_verified(source: dict, destination: Path) -> None:
    """Never execute/extract bytes until they match the reviewed repository pin."""
    request = urllib.request.Request(source["url"], headers={"User-Agent": "BananaFlow-release-build"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
                if urllib.parse.urlsplit(response.url).scheme != "https":
                    raise ValueError("Source download redirected to a non-HTTPS URL")
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("FFmpeg source archive exceeds size limit")
                    out.write(block)
            break
        except urllib.error.HTTPError as error:
            destination.unlink(missing_ok=True)
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            # Honor a bounded Retry-After; do not change hosts or evade a denial.
            retry_after = error.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 10 * (attempt + 1)
            if delay > 120:
                raise
            time.sleep(max(1, delay))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    try:
        verify_archive(destination, source["sha256"])
    except ValueError:
        destination.unlink(missing_ok=True)
        raise


def extract_source(archive: Path, destination: Path, source: dict) -> Path:
    """Preflight all members, refusing links, traversal and oversized archives."""
    root_name = f"{source['name']}-{source['version']}"
    with tarfile.open(archive, "r:*") as handle:
        members = handle.getmembers()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute() or ".." in path.parts or not path.parts
                or path.parts[0] != root_name or "\\" in member.name
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"Unsafe FFmpeg source member: {member.name}")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("FFmpeg source exceeds extracted size limit")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.extractfile(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                # Strip set-id/sticky/group-write bits while preserving configure's executable bit.
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return destination / root_name


def run(args: list[str], *, cwd: Path | None = None, env: dict | None = None) -> str:
    print("+ " + " ".join(map(str, args)), flush=True)
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="", flush=True)
    return result.stdout


def verify_lgpl(version_output: str, license_output: str, tool: str, expected_version: str) -> None:
    if not re.search(rf"^{tool} version {re.escape(expected_version)}(?:-|\s|$)", version_output, re.M):
        raise ValueError(f"{tool} does not report pinned version {expected_version}")
    if re.search(r"--enable-(?:gpl|nonfree)(?:\s|$)", version_output):
        raise ValueError(f"{tool} enabled GPL/nonfree components")
    if "GNU Lesser General Public License" not in license_output:
        raise ValueError(f"{tool} does not report an LGPL license")


def verify_system_libraries(otool_output: str) -> None:
    lines = otool_output.splitlines()
    if len(lines) < 2:
        raise ValueError("otool did not report the binary's linked libraries")
    for line in lines[1:]:
        library = line.strip().split(" (", 1)[0]
        if not library.startswith(("/usr/lib/", "/System/Library/")):
            raise ValueError(f"FFmpeg depends on a non-system library: {library}")


def smoke_audio(ffmpeg: Path, ffprobe: Path, scratch: Path) -> None:
    wav = scratch / "input.wav"
    with wave.open(str(wav), "wb") as audio:
        audio.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0" * 48000 * 2 * 2)
    for extension, (encoder, codec) in REQUIRED_ENCODERS.items():
        output = scratch / f"encoded.{extension}"
        run([str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(wav), "-c:a", encoder, str(output)])
        probe = json.loads(run([str(ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "json", str(output)]))
        if not probe.get("streams") or probe["streams"][0].get("codec_name") != codec:
            raise ValueError(f"FFmpeg audio smoke failed for {extension}")
        run([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(output), "-f", "null", "-"])


def inspect_macos_binaries(directory: Path, version: str) -> dict:
    evidence = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = directory / name
        if run(["lipo", "-archs", str(binary)]).strip() != "arm64":
            raise ValueError(f"{name} is not exclusively arm64")
        libraries = run(["otool", "-L", str(binary)])
        verify_system_libraries(libraries)
        version_output = run([str(binary), "-version"])
        license_output = run([str(binary), "-L"])
        verify_lgpl(version_output, license_output, name, version)
        evidence[name] = {
            "sha256": sha256(binary), "version": version_output,
            "license": license_output, "linked_libraries": libraries,
        }
    return evidence


def cache_matches(destination: Path, pins: dict, source_bundle: Path | None) -> bool:
    """A local build cache is not trusted merely because executables exist.

    Metadata is generated only after a successful verified build. Require exact
    source/recipe identity and binary/source hashes; callers must still inspect
    architecture/license/linkage and run codecs again. This is a build cache,
    not a signature or an authenticity claim about an attacker-controlled host.
    """
    try:
        evidence = json.loads((destination / "ffmpeg-build.json").read_text(encoding="utf-8"))
        if (
            evidence["target"] != pins["target"]
            or evidence["sources"] != pins["sources"]
            or evidence["source_manifest_sha256"] != sha256(PINS)
            or evidence["build_recipe_sha256"] != sha256(Path(__file__))
        ):
            return False
        for name in ("ffmpeg", "ffprobe"):
            if sha256(destination / name) != evidence["binaries"][name]["sha256"]:
                return False
        for source in pins["sources"]:
            for filename in source["license_files"]:
                path = destination / "licenses" / f"{source['name']}-{filename}"
                if sha256(path) != evidence["license_sha256"][path.name]:
                    return False
        if source_bundle and sha256(source_bundle) != evidence["source_bundle_sha256"]:
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def build(
    destination: Path, *, source_bundle: Path | None = None,
    source_dir: Path | None = None,
) -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("FFmpeg release staging requires native macOS arm64 (not Rosetta)")
    for tool in ("clang", "make", "pkg-config", "lipo", "otool"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing build prerequisite: {tool}; install Xcode command-line tools and pkg-config")
    pins = load_pins()
    version = next(s["version"] for s in pins["sources"] if s["name"] == "ffmpeg")
    with tempfile.TemporaryDirectory(prefix="bananaflow-ffmpeg-") as temp:
        work = Path(temp)
        if cache_matches(destination, pins, source_bundle):
            inspect_macos_binaries(destination, version)
            smoke_audio(destination / "ffmpeg", destination / "ffprobe", work)
            print("Revalidated the matching native LGPL FFmpeg build cache")
            return
        prefix = work / "prefix"
        archives = work / "archives"
        archives.mkdir()
        sources = {}
        for source in pins["sources"]:
            archive = archives / source["archive"]
            if source_dir is None:
                download_verified(source, archive)
            else:
                # Rebuild the conveyed source bundle without network access.
                # Verify the copied bytes through the same pin, never trust a
                # local archive merely because its filename/version matches.
                shutil.copyfile(source_dir / source["archive"], archive)
                verify_archive(archive, source["sha256"])
            sources[source["name"]] = extract_source(archive, work / "src", source)

        env = os.environ.copy()
        target = pins["deployment_target"]
        env.update({
            "CC": "clang", "CXX": "clang++", "MACOSX_DEPLOYMENT_TARGET": target,
            "CFLAGS": f"-O2 -fPIC -arch arm64 -mmacosx-version-min={target}",
            "CXXFLAGS": f"-O2 -fPIC -arch arm64 -mmacosx-version-min={target}",
            "CPPFLAGS": f"-I{prefix}/include",
            "LDFLAGS": f"-arch arm64 -mmacosx-version-min={target} -L{prefix}/lib",
            "PKG_CONFIG_LIBDIR": str(prefix / "lib" / "pkgconfig"),
            "PKG_CONFIG_PATH": "",
        })
        jobs = str(os.cpu_count() or 2)
        for name in ("lame", "opus", "libogg", "libvorbis"):
            args = ["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static", "--disable-dependency-tracking"]
            if name == "lame":
                # The release's old config.guess predates Apple Silicon. The
                # explicit compiler flags above still select native arm64.
                args += ["--build=arm-apple-darwin", "--disable-frontend", "--disable-decoder", "--disable-nasm"]
            if name == "opus":
                args += ["--disable-doc", "--disable-extra-programs"]
            run(args, cwd=sources[name], env=env)
            run(["make", "-j", jobs], cwd=sources[name], env=env)
            run(["make", "install"], cwd=sources[name], env=env)

        configure = [
            "./configure", f"--prefix={prefix}", "--arch=aarch64", "--target-os=darwin",
            "--cc=clang", "--cxx=clang++", "--disable-shared", "--enable-static",
            "--disable-autodetect", "--disable-gpl", "--disable-nonfree",
            "--disable-doc", "--disable-debug", "--disable-ffplay",
            "--enable-pthreads", "--enable-audiotoolbox", "--enable-videotoolbox",
            "--enable-securetransport", "--enable-zlib",
            "--enable-libmp3lame", "--enable-libopus", "--enable-libvorbis",
            "--pkg-config-flags=--static",
            f"--extra-cflags={env['CFLAGS']} -I{prefix}/include",
            f"--extra-ldflags={env['LDFLAGS']}",
        ]
        run(configure, cwd=sources["ffmpeg"], env=env)
        run(["make", "-j", jobs], cwd=sources["ffmpeg"], env=env)
        evidence = {
            "target": pins["target"], "sources": pins["sources"], "configure": configure,
            "source_manifest_sha256": sha256(PINS),
            "build_recipe_sha256": sha256(Path(__file__)),
            "binaries": inspect_macos_binaries(sources["ffmpeg"], version),
        }
        smoke = work / "smoke"
        smoke.mkdir()
        smoke_audio(sources["ffmpeg"] / "ffmpeg", sources["ffmpeg"] / "ffprobe", smoke)

        # Stage only after both binaries and every audio roundtrip have passed.
        destination.mkdir(parents=True, exist_ok=True)
        licenses = destination / "licenses"
        licenses.mkdir(exist_ok=True)
        for source in pins["sources"]:
            for filename in source["license_files"]:
                shutil.copyfile(sources[source["name"]] / filename, licenses / f"{source['name']}-{filename}")
        evidence["license_sha256"] = {
            path.name: sha256(path) for path in licenses.iterdir() if path.is_file()
        }
        for name in ("ffmpeg", "ffprobe"):
            shutil.copy2(sources["ffmpeg"] / name, destination / name)
        (destination / "ffmpeg-build.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        if source_bundle:
            source_bundle.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(source_bundle, "w:xz") as bundle:
                for archive in sorted(archives.iterdir()):
                    bundle.add(archive, arcname=f"sources/{archive.name}")
                bundle.add(Path(__file__), arcname="scripts/build_ffmpeg_macos.py")
                bundle.add(PINS, arcname="packaging/ffmpeg-macos-sources.json")
                bundle.add(destination / "ffmpeg-build.json", arcname="ffmpeg-build.json")
                bundle.add(licenses, arcname="licenses")
                readme = (
                    "BananaFlow macOS arm64 FFmpeg corresponding source\n\n"
                    "This bundle includes the exact verified upstream archives, "
                    "LGPL/BSD notices, build recipe and binary build evidence.\n"
                    "On native Apple Silicon macOS, install Xcode command-line "
                    "tools and pkg-config, extract this bundle, then run:\n\n"
                    "python3 scripts/build_ffmpeg_macos.py --source-dir sources "
                    "--destination packaging/ffmpeg\n\n"
                    "Source archive hashes are checked before extraction. No "
                    "source downloads occur with --source-dir. Compiler/SDK "
                    "versions can affect binary hashes; this is a reproducible "
                    "recipe, not a bit-for-bit reproducibility claim.\n"
                ).encode("utf-8")
                info = tarfile.TarInfo("README.txt")
                info.size = len(readme)
                info.mode = 0o644
                bundle.addfile(info, io.BytesIO(readme))
            evidence["source_bundle_sha256"] = sha256(source_bundle)
            (destination / "ffmpeg-build.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"Verified native LGPL FFmpeg {version} staged in {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "packaging" / "ffmpeg")
    parser.add_argument("--source-bundle", type=Path, help="Retain original verified source archives and the build recipe")
    parser.add_argument("--source-dir", type=Path, help="Use local source archives, still verified against every pinned hash")
    args = parser.parse_args()
    try:
        build(args.destination, source_bundle=args.source_bundle, source_dir=args.source_dir)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stdout:
            print(error.stdout, file=sys.stderr)
        print(f"FFmpeg staging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
