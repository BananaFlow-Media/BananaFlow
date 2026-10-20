"""Prepare full-application updates from the exact official GitHub release.

Discovery, explicit download approval, and explicit install/restart approval
are separate UI actions. No function here closes the running application.
Preparation is blocking and belongs in a worker; launch is a small plan write
and process spawn after the UI has accepted safe shutdown.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import urlparse
import uuid

import httpx

from core.app_update_helper import (
    AppUpdateError, MAX_ARTIFACT_BYTES, VERSION_RE, atomic_json, read_json,
    update_lock_is_held, verify_artifact,
)
from core.update_checker import ReleaseInfo, _is_newer
from version import FULL_VERSION

OFFICIAL_REPO = "BananaFlow-Media/BananaFlow"
API_ROOT = f"https://api.github.com/repos/{OFFICIAL_REPO}"
ASSET_API_PREFIX = API_ROOT + "/releases/assets/"
MAX_RELEASE_BYTES = 2 * 1024 * 1024
_DOWNLOAD_HOSTS = {"api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
# Keep the exact historical Inno identity. Its AppId escapes the opening brace
# with "{{", but the two closing braces are literal, so the registered key
# really ends in "}}_is1". Changing it would create a different installation.
WINDOWS_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{225AE960-D765-430C-BD88-5DED07F420E4}}_is1"
)
Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class AppUpdateSupport:
    supported: bool
    reason: str = ""
    kind: str = "unsupported"
    target: Path | None = None
    executable: str = ""
    helper_path: Path | None = None
    architecture: str = ""
    install_mode: str = ""


@dataclass(frozen=True)
class PreparedAppUpdate:
    version: str
    artifact_path: Path
    kind: str
    state_path: Path
    helper_path: Path
    target: Path
    executable: str
    size: int
    sha256: str
    install_mode: str = ""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    api_url: str
    size: int
    sha256: str


def _update_root() -> Path:
    from utils.paths import get_app_data_dir
    return get_app_data_dir() / "app-updates"


def _windows_install_mode(target: Path) -> str:
    """Registry identity + exact path, not filename guesses or writable tests."""
    if sys.platform != "win32":
        return ""
    import winreg
    for hive, mode in ((winreg.HKEY_CURRENT_USER, "currentuser"), (winreg.HKEY_LOCAL_MACHINE, "allusers")):
        try:
            with winreg.OpenKey(hive, WINDOWS_UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
                location = winreg.QueryValueEx(handle, "InstallLocation")[0]
            if Path(location).resolve() == target:
                return mode
        except OSError:
            continue
    return ""


def app_update_support() -> AppUpdateSupport:
    """Fail closed for source/Linux, unknown architecture/layout and read-only apps."""
    if not getattr(sys, "frozen", False):
        return AppUpdateSupport(False, "source")
    executable = Path(sys.executable).resolve()
    machine = platform.machine().lower()
    if sys.platform == "win32":
        if machine not in {"amd64", "x86_64"}:
            return AppUpdateSupport(False, "architecture")
        target = executable.parent
        if executable.name.lower() != "bananaflow.exe":
            return AppUpdateSupport(False, "layout")
        mode = _windows_install_mode(target)
        if not mode and any(target.glob("unins*.exe")):
            return AppUpdateSupport(False, "unknown-installation")
        kind = "windows-installed" if mode else "windows-portable"
        helper = target / "bananaflow-updater.exe"
        relative = "bananaflow.exe"
        architecture = "x64"
        if not mode and not os.access(target.parent, os.W_OK):
            return AppUpdateSupport(False, "read-only")
    elif sys.platform == "darwin":
        if machine not in {"arm64", "aarch64"}:
            return AppUpdateSupport(False, "architecture")
        if executable.name != "bananaflow" or executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents":
            return AppUpdateSupport(False, "layout")
        target = executable.parent.parent.parent
        if target.suffix != ".app" or "/AppTranslocation/" in str(target) or str(target).startswith("/Volumes/"):
            return AppUpdateSupport(False, "read-only")
        if not os.access(target.parent, os.W_OK):
            return AppUpdateSupport(False, "read-only")
        kind, mode, architecture = "macos-app", "", "arm64"
        helper = executable.parent / "bananaflow-updater"
        relative = "Contents/MacOS/bananaflow"
    else:
        return AppUpdateSupport(False, "platform")
    if helper.is_symlink() or not helper.is_file():
        return AppUpdateSupport(False, "helper-missing")
    return AppUpdateSupport(True, "", kind, target, relative, helper, architecture, mode)


def _asset_name(version: str, support: AppUpdateSupport) -> str:
    if not VERSION_RE.fullmatch(version):
        raise AppUpdateError("The release version is invalid.")
    endings = {
        "windows-installed": "windows-x64-setup.exe",
        "windows-portable": "windows-x64-portable.zip",
        "macos-app": "macos-arm64.dmg",
    }
    if support.kind not in endings:
        raise AppUpdateError("This application cannot update itself; use the download website.")
    return f"BananaFlow-v{version}-{endings[support.kind]}"


def select_release_asset(release: dict, version: str, support: AppUpdateSupport) -> ReleaseAsset:
    """Only an exact ready asset of the exact official published tag qualifies."""
    if release.get("tag_name") != f"v{version}" or release.get("draft") is not False:
        raise AppUpdateError("The official release tag is missing, different, or unpublished.")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise AppUpdateError("The official release has no usable assets.")
    name = _asset_name(version, support)
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise AppUpdateError("The official release does not contain exactly one compatible package.")
    asset = matches[0]
    size = asset.get("size")
    if asset.get("state") != "uploaded" or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise AppUpdateError("The official package is not ready or has an unsafe size.")
    api_url = str(asset.get("url", ""))
    if not api_url.startswith(ASSET_API_PREFIX) or not api_url[len(ASSET_API_PREFIX):].isdigit():
        raise AppUpdateError("The package is not an exact official repository API asset.")
    digest = str(asset.get("digest", ""))
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise AppUpdateError("GitHub did not provide a usable package SHA-256 digest.")
    return ReleaseAsset(name, api_url, size, digest.split(":")[1].lower())


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"BananaFlow/{FULL_VERSION} app-updater",
    }


def _safe_transport_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https" and parsed.hostname in _DOWNLOAD_HOSTS
        and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
    )


def _fetch_release(client: httpx.Client, version: str) -> dict:
    with client.stream("GET", f"{API_ROOT}/releases/tags/v{version}", headers=_headers()) as response:
        response.raise_for_status()
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > MAX_RELEASE_BYTES:
                raise AppUpdateError("The release metadata exceeds the safe limit.")
    import json
    release = json.loads(chunks)
    if not isinstance(release, dict):
        raise AppUpdateError("The release metadata is malformed.")
    return release


def _download(client: httpx.Client, asset: ReleaseAsset, output: Path, progress: Progress | None, cancelled: Callable[[], bool] | None) -> None:
    headers = {**_headers(), "Accept": "application/octet-stream"}
    url = asset.api_url
    for _ in range(5):
        if not _safe_transport_url(url):
            raise AppUpdateError("The package download redirected outside approved HTTPS hosts.")
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            total = 0
            digest = hashlib.sha256()
            with output.open("xb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if cancelled is not None and cancelled():
                        raise AppUpdateError("The package download was cancelled.")
                    total += len(chunk)
                    if total > asset.size:
                        raise AppUpdateError("The download exceeded its authenticated size.")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress is not None:
                        progress(total, asset.size)
                handle.flush()
                os.fsync(handle.fileno())
            if total != asset.size or digest.hexdigest() != asset.sha256:
                raise AppUpdateError("The downloaded package failed size or SHA-256 verification.")
            return
    raise AppUpdateError("The package download redirected too many times.")


def _quarantine(artifact: Path) -> None:
    # Record Internet origin rather than treating Python downloads as trusted
    # local code. Do not clear an existing Zone.Identifier/quarantine.
    if sys.platform == "win32":
        with open(str(artifact) + ":Zone.Identifier", "w", encoding="utf-8") as handle:
            handle.write("[ZoneTransfer]\nZoneId=3\nHostUrl=https://github.com/BananaFlow-Media/BananaFlow\n")
    elif sys.platform == "darwin":
        os.setxattr(artifact, "com.apple.quarantine", f"0081;{int(time.time()):x};BananaFlow;".encode())


def prepare_app_update(
    release: ReleaseInfo, *, progress: Progress | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> PreparedAppUpdate:
    """Download/verify/stage only. Does not run installer or change live app."""
    support = app_update_support()
    if not support.supported:
        raise AppUpdateError("This installation needs the manual download website.")
    if not VERSION_RE.fullmatch(release.version) or not _is_newer(release.version, FULL_VERSION):
        raise AppUpdateError("The selected release is not newer than this application.")
    root = _update_root().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_active_helper(root)
    work = root / uuid.uuid4().hex
    work.mkdir(mode=0o700)
    try:
        with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False, trust_env=False) as client:
            metadata = _fetch_release(client, release.version)
            asset = select_release_asset(metadata, release.version, support)
            artifact = work / asset.name
            partial = work / (asset.name + ".partial")
            _download(client, asset, partial, progress, cancelled)
            os.replace(partial, artifact)
        _quarantine(artifact)
        helper = work / support.helper_path.name
        shutil.copy2(support.helper_path, helper)
        state = {
            "schema": 1, "version": release.version, "kind": support.kind,
            "artifact": str(artifact), "size": asset.size, "sha256": asset.sha256,
            "target": str(support.target), "executable": support.executable,
            "install_mode": support.install_mode, "helper": str(helper),
            "asset_api_url": asset.api_url, "asset_name": asset.name,
        }
        state_path = work / "prepared.json"
        atomic_json(state_path, state)
        atomic_json(root / "prepared.json", {"schema": 1, "directory": work.name})
        return _prepared_from_state(state_path, state, support, verify=False)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        if isinstance(exc, AppUpdateError):
            raise
        raise AppUpdateError("The application update could not be prepared.") from exc


def _prepared_from_state(path: Path, data: dict, support: AppUpdateSupport, *, verify: bool) -> PreparedAppUpdate:
    if (
        data.get("schema") != 1 or data.get("kind") != support.kind
        or data.get("target") != str(support.target) or data.get("executable") != support.executable
        or data.get("install_mode", "") != support.install_mode
        or not VERSION_RE.fullmatch(str(data.get("version", "")))
        or not _is_newer(data["version"], FULL_VERSION)
    ):
        raise AppUpdateError("The prepared update does not match this installation.")
    artifact = Path(data["artifact"])
    helper = Path(data["helper"])
    if (
        artifact.parent != path.parent or helper.parent != path.parent
        or artifact.name != _asset_name(data["version"], support)
        or helper.name != support.helper_path.name or helper.is_symlink() or not helper.is_file()
    ):
        raise AppUpdateError("The prepared update paths are unsafe.")
    if verify:
        verify_artifact(artifact, data["size"], data["sha256"])
    return PreparedAppUpdate(
        data["version"], artifact, data["kind"], path, helper, support.target,
        support.executable, data["size"], data["sha256"], support.install_mode,
    )


def get_prepared_app_update() -> PreparedAppUpdate | None:
    """Re-verify durable prepared state off the GUI thread. Never auto-install."""
    support = app_update_support()
    if not support.supported:
        return None
    root = _update_root().resolve()
    try:
        pointer = read_json(root / "prepared.json")
        directory = pointer.get("directory", "")
        if pointer.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(directory)):
            return None
        work = root / directory
        if work.is_symlink() or work.resolve() != work:
            return None
        state_path = work / "prepared.json"
        return _prepared_from_state(state_path, read_json(state_path), support, verify=True)
    except (OSError, ValueError, TypeError, KeyError, AppUpdateError):
        return None


def get_app_update_status() -> dict:
    """Read bounded last-attempt status, without network access or execution."""
    root = _update_root().resolve()
    try:
        pointer = read_json(root / "prepared.json")
        directory = pointer.get("directory", "")
        if not re.fullmatch(r"[0-9a-f]{32}", str(directory)):
            return {}
        work = root / directory
        if work.is_symlink() or work.resolve() != work:
            return {}
        data = read_json(work / "status.json")
        return {
            key: value[:1000] for key, value in data.items()
            if key in {"status", "version", "message", "backup"} and isinstance(value, str)
        }
    except (OSError, ValueError, TypeError, AppUpdateError):
        return {}


def _assert_no_active_helper(root: Path) -> None:
    try:
        if update_lock_is_held(root / "apply.lock"):
            raise AppUpdateError("An installer is already running. Wait for it to finish.")
    except OSError as exc:
        raise AppUpdateError("The updater lock could not be checked safely.") from exc


def launch_prepared_app_update(
    prepared: PreparedAppUpdate | None = None, *, smoke_result: Path | None = None,
) -> subprocess.Popen:
    """Arm an approved restart. The detached helper waits for this PID to exit.

    Pass the worker-validated PreparedAppUpdate to avoid hashing on the GUI
    thread. The helper verifies the artifact itself again before installation.
    """
    if prepared is None:
        raise AppUpdateError("Verify the prepared update in a worker before installing.")
    support = app_update_support()
    if (
        not support.supported or prepared.target != support.target or prepared.kind != support.kind
        or prepared.install_mode != support.install_mode or prepared.executable != support.executable
    ):
        raise AppUpdateError("The prepared update does not match this installation.")
    _assert_no_active_helper(prepared.state_path.parent.parent)
    arguments: list[str] = []
    if smoke_result is not None:
        # This hook is used only by the explicitly invoked packaged smoke,
        # which separately requires its marked disposable fixture directory.
        arguments = ["--app-update-smoke-result", str(smoke_result.resolve())]
    plan = {
        "schema": 1, "kind": prepared.kind, "version": prepared.version,
        "target": str(prepared.target), "executable": prepared.executable,
        "artifact": str(prepared.artifact_path), "size": prepared.size,
        "sha256": prepared.sha256, "parent_pid": os.getpid(),
        "install_mode": prepared.install_mode, "transaction": uuid.uuid4().hex,
        "relaunch_args": arguments,
    }
    plan_path = prepared.state_path.parent / f"plan-{plan['transaction']}.json"
    atomic_json(plan_path, plan)
    environment = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    kwargs: dict = {"cwd": str(plan_path.parent), "close_fds": True, "env": environment}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            [str(prepared.helper_path), "--apply", str(plan_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError as exc:
        raise AppUpdateError("The updater could not start. Reopen BananaFlow to retry the prepared update.") from exc
    # This is called after the event loop shuts down. Wait for the helper's
    # small readiness record, not a package rehash or an installation. A dead
    # helper must not make the user believe a restart has been armed.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status = read_json(plan_path.parent / "status.json")
            if status.get("transaction") == plan["transaction"]:
                if status.get("status") == "waiting":
                    return process
                if status.get("status") == "error":
                    raise AppUpdateError("The updater could not prepare the restart.")
        except (OSError, ValueError):
            pass
        if process.poll() is not None:
            raise AppUpdateError("The updater exited before preparing the restart.")
        time.sleep(0.1)
    plan_path.with_suffix(".cancel").touch()
    raise AppUpdateError("The updater did not become ready; installation was cancelled.")
