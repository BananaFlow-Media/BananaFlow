"""Offline behavioral regressions for FFmpeg release supply-chain staging."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import struct
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("stage_ffmpeg", ROOT / "scripts" / "stage_ffmpeg.py")
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)
mac = stage.macos


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_manifest_contains_complete_versioned_source_set():
    pins = mac.load_pins()
    assert {s["name"] for s in pins["sources"]} == mac.SOURCE_NAMES
    assert pins["deployment_target"] == "11.0"
    assert all("latest" not in s["url"] and s["checksum_source"] for s in pins["sources"])


@pytest.mark.parametrize("field,value", [
    ("sha256", "00"),
    ("version", "latest"),
    ("url", "http://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz"),
    ("url", "https://untrusted.invalid/ffmpeg-8.1.3.tar.xz"),
    ("url", "https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz?mutable=1"),
    ("url", "https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz#fragment"),
    ("archive", "../../escape.tar.xz"),
    ("license_files", ["../COPYING"]),
])
def test_source_manifest_rejects_unpinned_or_unsafe_input(tmp_path, field, value):
    pins = mac.load_pins()
    pins["sources"][0][field] = value
    with pytest.raises(ValueError):
        mac.load_pins(_write_json(tmp_path / "pins.json", pins))


def test_source_manifest_rejects_incomplete_set(tmp_path):
    pins = mac.load_pins()
    pins["sources"].pop()
    with pytest.raises(ValueError, match="complete"):
        mac.load_pins(_write_json(tmp_path / "pins.json", pins))


def test_windows_pin_is_a_retained_month_end():
    pin = stage.load_windows_pin()
    assert pin["tag"] == "autobuild-2026-09-30-13-08"
    assert pin["sha256"] == "c73ad424c5f9d94dd48815b4d3531e57130cb9f5c3dc8f21f4aa4e1fdb0658e3"


@pytest.mark.parametrize("field,value", [
    ("tag", "latest"),
    ("tag", "autobuild-2026-09-07-15-39"),
    ("sha256", "unverified"),
    ("url", "https://example.invalid/ffmpeg.zip"),
    ("archive", "ffmpeg-N-127032-g6ae491a26c-win64-gpl.zip"),
])
def test_windows_rejects_expiring_or_unverified_assets(tmp_path, field, value):
    pin = stage.load_windows_pin()
    pin[field] = value
    with pytest.raises(ValueError):
        stage.load_windows_pin(_write_json(tmp_path / "pin.json", pin))


class _Response(io.BytesIO):
    url = "https://ffmpeg.org/releases/pinned.tar.xz"


def test_download_accepts_matching_hash_and_never_fetches_checksum(tmp_path, monkeypatch):
    payload = b"the reviewed source archive"
    calls = []
    def open_url(request, timeout):
        calls.append(request.full_url)
        assert timeout == 60
        return _Response(payload)
    monkeypatch.setattr(mac.urllib.request, "urlopen", open_url)
    source = {"url": _Response.url, "sha256": hashlib.sha256(payload).hexdigest()}
    target = tmp_path / "source"
    mac.download_verified(source, target)
    assert target.read_bytes() == payload
    assert calls == [source["url"]]


def test_download_bad_hash_fails_closed_and_removes_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(mac.urllib.request, "urlopen", lambda *a, **k: _Response(b"tampered"))
    target = tmp_path / "source"
    expected = hashlib.sha256(b"reviewed").hexdigest()
    with pytest.raises(ValueError, match=expected):
        mac.download_verified({"url": _Response.url, "sha256": expected}, target)
    assert not target.exists()


def test_download_http_redirect_downgrade_is_rejected(tmp_path, monkeypatch):
    response = _Response(b"bytes")
    response.url = "http://mirror.invalid/source"
    monkeypatch.setattr(mac.urllib.request, "urlopen", lambda *a, **k: response)
    with pytest.raises(ValueError, match="non-HTTPS"):
        mac.download_verified({"url": _Response.url, "sha256": "0" * 64}, tmp_path / "source")


def test_download_honors_retry_after_but_does_not_retry_denials(tmp_path, monkeypatch):
    payload = b"reviewed"
    source = {"url": _Response.url, "sha256": hashlib.sha256(payload).hexdigest()}
    sleeps = []
    calls = []
    def open_url(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(source["url"], 429, "rate limit", {"Retry-After": "3"}, None)
        return _Response(payload)
    monkeypatch.setattr(mac.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(mac.time, "sleep", sleeps.append)
    mac.download_verified(source, tmp_path / "source")
    assert sleeps == [3] and len(calls) == 2
    calls.clear()
    def denied(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError(source["url"], 403, "denied", {}, None)
    monkeypatch.setattr(mac.urllib.request, "urlopen", denied)
    with pytest.raises(urllib.error.HTTPError):
        mac.download_verified(source, tmp_path / "source")
    assert len(calls) == 1


def _tar(path, members):
    with tarfile.open(path, "w") as archive:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o4755
            if kind == "file":
                info.size = 4
                archive.addfile(info, io.BytesIO(b"data"))
            else:
                info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                info.linkname = "outside"
                archive.addfile(info)
    return path


def test_source_extracts_regular_files_without_privileged_bits(tmp_path):
    archive = _tar(tmp_path / "test.tar", [("ffmpeg-8.1.3/configure", "file")])
    directory = mac.extract_source(archive, tmp_path / "out", {"name": "ffmpeg", "version": "8.1.3"})
    assert (directory / "configure").read_bytes() == b"data"
    assert (directory / "configure").stat().st_mode & 0o7777 == 0o755


@pytest.mark.parametrize("name,kind", [
    ("ffmpeg-8.1.3/../../escape", "file"),
    ("/absolute", "file"),
    ("other-root/configure", "file"),
    ("ffmpeg-8.1.3/symlink", "symlink"),
    ("ffmpeg-8.1.3/hardlink", "hardlink"),
    ("ffmpeg-8.1.3/back\\slash", "file"),
])
def test_source_extraction_preflights_all_members(tmp_path, name, kind):
    archive = _tar(tmp_path / "test.tar", [("ffmpeg-8.1.3/configure", "file"), (name, kind)])
    with pytest.raises(ValueError, match="Unsafe"):
        mac.extract_source(archive, tmp_path / "out", {"name": "ffmpeg", "version": "8.1.3"})
    assert not (tmp_path / "out").exists()


def test_bad_source_never_reaches_extraction_or_compilation(tmp_path, monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mac.shutil, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(mac.urllib.request, "urlopen", lambda *a, **k: _Response(b"wrong archive"))
    monkeypatch.setattr(mac, "extract_source", lambda *a: pytest.fail("unverified source was extracted"))
    monkeypatch.setattr(mac, "run", lambda *a, **k: pytest.fail("unverified source was executed"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        mac.build(tmp_path / "staged")
    assert not (tmp_path / "staged").exists()


def test_offline_source_archives_still_require_pinned_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mac.shutil, "which", lambda tool: "/usr/bin/" + tool)
    source = mac.load_pins()["sources"][0]
    (tmp_path / source["archive"]).write_bytes(b"local but unverified")
    monkeypatch.setattr(mac, "download_verified", lambda *a: pytest.fail("offline build attempted a download"))
    monkeypatch.setattr(mac, "extract_source", lambda *a: pytest.fail("unverified local archive was extracted"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        mac.build(tmp_path / "staged", source_dir=tmp_path)


def test_source_bundle_and_cache_roundtrip_without_executing_native_code(tmp_path, monkeypatch):
    """Exercise complete staging/evidence wiring with the compiler mocked.

    Native architecture/license/codec correctness remains a release CI gate,
    not a claim made by this deterministic Linux test.
    """
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mac.shutil, "which", lambda tool: "/usr/bin/" + tool)
    pins = mac.load_pins()
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    for source in pins["sources"]:
        root = f"{source['name']}-{source['version']}"
        archive = _tar(source_dir / source["archive"], [
            (f"{root}/{name}", "file") for name in ["configure"] + source["license_files"]
        ])
        source["sha256"] = mac.sha256(archive)
    monkeypatch.setattr(mac, "load_pins", lambda: pins)
    monkeypatch.setattr(mac, "download_verified", lambda *a: pytest.fail("offline build attempted a download"))
    commands = []
    def compile_stub(args, cwd=None, env=None):
        commands.append(args)
        if cwd and cwd.name.startswith("ffmpeg-") and args[0] == "make":
            for name in ("ffmpeg", "ffprobe"):
                (cwd / name).write_bytes(name.encode())
        return ""
    monkeypatch.setattr(mac, "run", compile_stub)
    inspections = []
    def inspect_stub(directory, version):
        inspections.append(directory)
        return {name: {"sha256": mac.sha256(directory / name)} for name in ("ffmpeg", "ffprobe")}
    monkeypatch.setattr(mac, "inspect_macos_binaries", inspect_stub)
    smokes = []
    monkeypatch.setattr(mac, "smoke_audio", lambda *args: smokes.append(args))
    destination = tmp_path / "staged"
    bundle = tmp_path / "source-bundle.tar.xz"
    mac.build(destination, source_dir=source_dir, source_bundle=bundle)
    assert mac.cache_matches(destination, pins, bundle)
    evidence = json.loads((destination / "ffmpeg-build.json").read_text())
    assert evidence["source_bundle_sha256"] == mac.sha256(bundle)
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
        assert "scripts/build_ffmpeg_macos.py" in names
        assert "packaging/ffmpeg-macos-sources.json" in names
        assert "README.txt" in names
        assert all(f"sources/{s['archive']}" in names for s in pins["sources"])
        assert b"--source-dir sources" in archive.extractfile("README.txt").read()
    compile_count = len(commands)
    mac.build(destination, source_dir=source_dir, source_bundle=bundle)
    assert len(commands) == compile_count
    assert len(inspections) == 2 and len(smokes) == 2
    assert inspections[1] == destination


@pytest.mark.parametrize("machine", ["x86_64", "AMD64"])
def test_native_macos_gate_rejects_rosetta_before_download(tmp_path, monkeypatch, machine):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    monkeypatch.setattr(mac.platform, "machine", lambda: machine)
    monkeypatch.setattr(mac, "download_verified", lambda *a: pytest.fail("download should not start"))
    with pytest.raises(RuntimeError, match="native macOS arm64"):
        mac.build(tmp_path)


def test_lgpl_and_version_validation():
    mac.verify_lgpl("ffmpeg version 8.1.3\nconfiguration: --disable-gpl", "GNU Lesser General Public License", "ffmpeg", "8.1.3")
    mac.verify_lgpl("ffmpeg version N-127032-g6ae491a26c-20260930", "GNU Lesser General Public License", "ffmpeg", "N-127032-g6ae491a26c")


@pytest.mark.parametrize("version,license_text", [
    ("ffmpeg version 8.1.2", "GNU Lesser General Public License"),
    ("ffmpeg version 8.1.30", "GNU Lesser General Public License"),
    ("ffmpeg version 8.1.3\nconfiguration: --enable-gpl", "GNU Lesser General Public License"),
    ("ffmpeg version 8.1.3\nconfiguration: --enable-nonfree", "GNU Lesser General Public License"),
    ("ffmpeg version 8.1.3", "GNU General Public License"),
])
def test_wrong_version_or_license_is_rejected(version, license_text):
    with pytest.raises(ValueError):
        mac.verify_lgpl(version, license_text, "ffmpeg", "8.1.3")


@pytest.mark.parametrize("library", ["/opt/homebrew/lib/libopus.dylib", "@rpath/libavcodec.dylib", "/usr/local/lib/libogg.dylib"])
def test_macos_rejects_non_system_runtime_dependencies(library):
    with pytest.raises(ValueError, match="non-system"):
        mac.verify_system_libraries(f"ffmpeg:\n\t{library} (compatibility version 1.0.0, current version 1.0.0)")


def test_macos_accepts_only_system_libraries():
    mac.verify_system_libraries("ffmpeg:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n\t/System/Library/Frameworks/Security.framework/Versions/A/Security (compatibility version 1.0.0)")


def _windows_zip(path, pin, *, missing=None, symlink=False, duplicate=False):
    root = pin["archive"].removesuffix(".zip")
    with zipfile.ZipFile(path, "w") as archive:
        for relative in ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE.txt"):
            if relative == missing:
                continue
            info = zipfile.ZipInfo(f"{root}/{relative}")
            if symlink and relative == "bin/ffmpeg.exe":
                info.external_attr = 0o120777 << 16
            archive.writestr(info, b"candidate")
        if duplicate:
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr(f"{root}/bin/ffmpeg.exe", b"second")
        archive.writestr("../../unrelated", b"never extracted")
    return path


def test_windows_extracts_only_exact_required_paths(tmp_path):
    pin = stage.load_windows_pin()
    archive = _windows_zip(tmp_path / "test.zip", pin)
    stage.extract_windows_archive(archive, tmp_path / "out", pin)
    assert sorted(p.relative_to(tmp_path / "out").as_posix() for p in (tmp_path / "out").rglob("*") if p.is_file()) == [
        "ffmpeg.exe", "ffprobe.exe", "licenses/ffmpeg-LICENSE.txt",
    ]
    assert not (tmp_path / "unrelated").exists()


@pytest.mark.parametrize("kwargs", [{"missing": "bin/ffprobe.exe"}, {"missing": "LICENSE.txt"}, {"symlink": True}, {"duplicate": True}])
def test_windows_refuses_missing_duplicate_or_linked_members(tmp_path, kwargs):
    pin = stage.load_windows_pin()
    archive = _windows_zip(tmp_path / "test.zip", pin, **kwargs)
    with pytest.raises(ValueError):
        stage.extract_windows_archive(archive, tmp_path / "out", pin)


def test_windows_binary_machine_is_validated(tmp_path):
    header = bytearray(64)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(header + b"PE\0\0" + struct.pack("<H", 0x8664))
    stage.verify_windows_x64(binary)
    binary.write_bytes(header + b"PE\0\0" + struct.pack("<H", 0xAA64))
    with pytest.raises(ValueError, match="x64"):
        stage.verify_windows_x64(binary)


def test_every_supported_audio_format_is_encoded_probed_and_decoded(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0].endswith("ffprobe"):
            extension = Path(command[-1]).suffix[1:]
            return json.dumps({"streams": [{"codec_name": mac.REQUIRED_ENCODERS[extension][1]}]})
        return ""
    monkeypatch.setattr(mac, "run", run)
    mac.smoke_audio(tmp_path / "ffmpeg", tmp_path / "ffprobe", tmp_path)
    assert len(calls) == 18
    assert {call[call.index("-c:a") + 1] for call in calls if "-c:a" in call} == {v[0] for v in mac.REQUIRED_ENCODERS.values()}
    assert len([call for call in calls if "null" in call]) == 6


def test_audio_smoke_rejects_incorrect_probe_result(tmp_path, monkeypatch):
    monkeypatch.setattr(mac, "run", lambda command, **kw: '{"streams": []}' if command[0].endswith("ffprobe") else "")
    with pytest.raises(ValueError, match="audio smoke"):
        mac.smoke_audio(tmp_path / "ffmpeg", tmp_path / "ffprobe", tmp_path)


def _cache(tmp_path):
    pins = mac.load_pins()
    directory = tmp_path / "staged"
    directory.mkdir()
    licenses = directory / "licenses"
    licenses.mkdir()
    evidence = {
        "target": pins["target"], "sources": pins["sources"],
        "source_manifest_sha256": mac.sha256(mac.PINS),
        "build_recipe_sha256": mac.sha256(Path(mac.__file__)),
        "binaries": {}, "license_sha256": {},
    }
    for name in ("ffmpeg", "ffprobe"):
        (directory / name).write_bytes(name.encode())
        evidence["binaries"][name] = {"sha256": mac.sha256(directory / name)}
    for source in pins["sources"]:
        for filename in source["license_files"]:
            path = licenses / f"{source['name']}-{filename}"
            path.write_bytes(b"license")
            evidence["license_sha256"][path.name] = mac.sha256(path)
    bundle = tmp_path / "sources.tar.xz"
    bundle.write_bytes(b"sources")
    evidence["source_bundle_sha256"] = mac.sha256(bundle)
    _write_json(directory / "ffmpeg-build.json", evidence)
    return directory, pins, bundle, evidence


def test_cache_requires_hashes_of_both_binaries_sources_and_notices(tmp_path):
    directory, pins, bundle, evidence = _cache(tmp_path)
    assert mac.cache_matches(directory, pins, bundle)
    (directory / "ffprobe").write_bytes(b"tampered")
    assert not mac.cache_matches(directory, pins, bundle)


@pytest.mark.parametrize("field", ["build_recipe_sha256", "source_manifest_sha256", "source_bundle_sha256"])
def test_cache_is_invalidated_by_recipe_manifest_or_source_change(tmp_path, field):
    directory, pins, bundle, evidence = _cache(tmp_path)
    evidence[field] = "0" * 64
    _write_json(directory / "ffmpeg-build.json", evidence)
    assert not mac.cache_matches(directory, pins, bundle)


def test_arbitrary_prestaged_executables_are_not_a_verified_cache(tmp_path):
    for name in ("ffmpeg", "ffprobe"):
        (tmp_path / name).write_bytes(b"arbitrary executable")
    assert not mac.cache_matches(tmp_path, mac.load_pins(), None)
