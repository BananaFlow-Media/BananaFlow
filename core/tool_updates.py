"""Versioned, verified executable tools for supported packaged installations."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

import httpx

from core.app_update_helper import AppUpdateError, _acquire_lock, atomic_json, read_json, verify_artifact
from core.app_updates import ReleaseAsset, _download, _fetch_tag_release, _quarantine, select_verified_asset
from utils.paths import get_app_data_dir, is_frozen
from version import FULL_VERSION, MAINTENANCE_REVISION

logger = logging.getLogger(__name__)
TOOLS = {"ffmpeg": ("FFmpeg / FFprobe", ("ffmpeg", "ffprobe")), "deno": ("Deno", ("deno",))}
MAX_TOOL_BYTES = 512 * 1024 * 1024
CONTROL_AGE_SECONDS = 24 * 60 * 60
_LICENSE_NAMES = {"LICENSE", "NOTICE", "COPYING"}
_active: dict[str, tuple[int, Path]] = {}


@dataclass(frozen=True)
class ToolOffer:
    tool: str
    revision: int
    asset: ReleaseAsset


def _platform_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "windows-x64"
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return "macos-arm64"
    return ""


def _channel() -> str:
    return f"runtime-channel-v{FULL_VERSION}-r{MAINTENANCE_REVISION}"


def _root() -> Path:
    return get_app_data_dir() / "components" / _channel() / _platform_key()


def tool_status_key(tool: str) -> str:
    return f"runtime:{tool}:{FULL_VERSION}:r{MAINTENANCE_REVISION}"


def active_tool_dir(tool: str) -> Path | None:
    selected = _active.get(tool)
    return selected[1] if selected else None


def _catalog(client: httpx.Client) -> list[ToolOffer]:
    target = _platform_key()
    tag = _channel()
    release = _fetch_tag_release(client, tag, optional=True)
    if release is None:
        return []
    assets = release.get("assets")
    if (
        release.get("tag_name") != tag or release.get("draft") is not False
        or release.get("prerelease") is not True or not isinstance(assets, list)
    ):
        raise AppUpdateError("The official runtime channel is invalid.")
    pattern = re.compile(rf"bananaflow-(ffmpeg|deno)-{target}-r([1-9][0-9]{{0,5}})\.zip")
    offers = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        match = pattern.fullmatch(name) if isinstance(name, str) else None
        if match:
            offers.append(ToolOffer(match[1], int(match[2]), select_verified_asset(
                release, name, maximum=MAX_TOOL_BYTES,
            )))
    return offers


def _latest(offers: list[ToolOffer], tool: str) -> ToolOffer | None:
    return max((offer for offer in offers if offer.tool == tool), key=lambda offer: offer.revision, default=None)


def check_tools(timeout: float):
    from core.component_updates import ComponentStatus

    if not is_frozen() or not _platform_key():
        return []
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            offers = _catalog(client)
    except (OSError, ValueError, TypeError, AppUpdateError, httpx.HTTPError):
        return [ComponentStatus(tool_status_key(tool), title, check_ok=False) for tool, (title, _) in TOOLS.items()]
    statuses = []
    for tool, (title, _) in TOOLS.items():
        offer = _latest(offers, tool)
        if offer is None:
            continue
        revision = _active.get(tool, (0, None))[0]
        statuses.append(ComponentStatus(
            tool_status_key(tool), title, f"r{revision}", f"r{offer.revision}",
            update_available=offer.revision > revision, check_ok=True,
        ))
    return statuses


def _executable_names(tool: str) -> tuple[str, ...]:
    suffix = ".exe" if sys.platform == "win32" else ""
    return tuple(name + suffix for name in TOOLS[tool][1])


def _unpack(archive_path: Path, destination: Path, executables: tuple[str, ...]) -> dict:
    files = {}
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        names = {member.filename for member in members}
        if (
            len(members) != len(names) or not set(executables) <= names
            or not names <= set(executables) | _LICENSE_NAMES
            or sum(member.file_size for member in members) > MAX_TOOL_BYTES
        ):
            raise AppUpdateError("The runtime archive has unexpected files or size.")
        for member in members:
            name = member.filename
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (
                member.orig_filename != name or member.file_size <= 0
                or member.is_dir() or mode not in {0, stat.S_IFREG}
            ):
                raise AppUpdateError("The runtime archive includes an invalid file.")
            target = destination / name
            with archive.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if name in executables:
                target.chmod(0o700)
                _quarantine(target)
                with target.open("rb") as source:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                files[name] = {"size": member.file_size, "sha256": digest.hexdigest()}
    return files


def _healthcheck(directory: Path, executables: tuple[str, ...]) -> None:
    for name in executables:
        kind = Path(name).stem
        flag = "--version" if kind == "deno" else "-version"
        result = subprocess.run(
            [str(directory / name), flag], cwd=directory,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        expected = f"{kind} " if kind == "deno" else f"{kind} version "
        if result.returncode or not result.stdout.startswith(expected.encode("ascii")):
            raise AppUpdateError("An updated runtime failed its health check.")


def _read_optional(path: Path) -> dict:
    try:
        return read_json(path)
    except (AppUpdateError, OSError, ValueError):
        return {}


def _write_control(root: Path, offers: list[ToolOffer]) -> dict:
    control = {
        "checked_at": int(time.time()),
        "allowed": {offer.asset.name: offer.asset.sha256 for offer in offers},
    }
    atomic_json(root / "control.json", control)
    return control["allowed"]


def _cached_control(root: Path) -> dict | None:
    cached = _read_optional(root / "control.json")
    checked_at = cached.get("checked_at")
    if (
        type(checked_at) is int and 0 <= int(time.time()) - checked_at <= CONTROL_AGE_SECONDS
        and isinstance(cached.get("allowed"), dict)
    ):
        return cached["allowed"]
    return None


def _control(root: Path) -> dict:
    cached = _cached_control(root)
    if cached is not None:
        return cached
    with _acquire_lock(root / "install.lock", uuid.uuid4().hex):
        cached = _cached_control(root)
        if cached is not None:
            return cached
        with httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False) as client:
            return _write_control(root, _catalog(client))


def _validated_selection(root: Path, tool: str, record: object, allowed: dict) -> tuple[int, Path] | None:
    if not isinstance(record, dict):
        return None
    revision = record.get("revision")
    directory = record.get("directory")
    files = record.get("files")
    expected = set(_executable_names(tool))
    if (
        type(revision) is not int or not 0 < revision <= 999999
        or not isinstance(directory, str) or not re.fullmatch("[a-f0-9]{32}", directory)
        or not isinstance(files, dict) or set(files) != expected
    ):
        return None
    name = f"bananaflow-{tool}-{_platform_key()}-r{revision}.zip"
    if not isinstance(record.get("sha256"), str) or allowed.get(name) != record["sha256"]:
        return None
    path = root / tool / directory
    try:
        if not path.is_dir() or path.resolve() != path or {entry.name for entry in path.iterdir()} - expected - _LICENSE_NAMES:
            return None
    except (OSError, RuntimeError):
        return None
    for filename in expected:
        receipt = files[filename]
        if not isinstance(receipt, dict):
            return None
        try:
            verify_artifact(path / filename, receipt["size"], receipt["sha256"])
        except (AppUpdateError, KeyError, ValueError, TypeError, OSError):
            return None
    return revision, path


def activate_tool_overlays() -> None:
    """Select verified tools once in a fresh GUI or CLI process."""
    if not is_frozen() or not _platform_key():
        return
    root = _root().resolve()
    states = {tool: _read_optional(root / tool / "active.json") for tool in TOOLS}
    if not any(states.values()):
        return
    try:
        allowed = _control(root)
    except (AppUpdateError, OSError, ValueError, TypeError, httpx.HTTPError):
        logger.warning("Updated runtime authorization unavailable; using bundled runtimes")
        return
    for tool, state in states.items():
        for slot in ("active", "previous"):
            try:
                selection = _validated_selection(root, tool, state.get(slot), allowed)
            except (OSError, RuntimeError):
                continue
            if selection is not None:
                _active[tool] = selection
                break


def install_tool_update(tool: str, version: str) -> None:
    if tool not in TOOLS or not is_frozen() or not _platform_key():
        raise AppUpdateError("This installation cannot update that runtime.")
    root = _root().resolve()
    group = root / tool
    group.mkdir(parents=True, mode=0o700, exist_ok=True)
    install_id = uuid.uuid4().hex
    with _acquire_lock(root / "install.lock", install_id):
        with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False, trust_env=False) as client:
            offers = _catalog(client)
            offer = _latest(offers, tool)
            if offer is None or version != f"r{offer.revision}" or offer.revision <= _active.get(tool, (0, None))[0]:
                raise AppUpdateError("The runtime update changed; check for updates again.")
            state = _read_optional(group / "active.json")
            allowed = _write_control(root, offers)
            current = _validated_selection(root, tool, state.get("active"), allowed)
            if current is not None and current[0] >= offer.revision:
                return
            previous = state.get("active") if current else state.get("previous")
            with tempfile.TemporaryDirectory(dir=group, prefix="staging-") as staging:
                scratch = Path(staging)
                archive = scratch / "runtime.zip"
                payload = scratch / "payload"
                payload.mkdir()
                _download(client, offer.asset, archive, None, None)
                executables = _executable_names(tool)
                files = _unpack(archive, payload, executables)
                _healthcheck(payload, executables)
                os.replace(payload, group / install_id)
                atomic_json(group / "active.json", {
                    "active": {
                        "directory": install_id, "revision": offer.revision,
                        "sha256": offer.asset.sha256, "files": files,
                    },
                    "previous": previous,
                })
