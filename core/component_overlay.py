"""Verified per-user downloader-component overlays for packaged builds.

The installed application is immutable.  A reviewed component bundle is
therefore downloaded into versioned app-data storage, health-checked in a
fresh BananaFlow process, and selected atomically for the *next* launch.
The running process never swaps the downloader implementation underneath an
active operation.

Trust is anchored in the official BananaFlow GitHub repository.  The channel
manifest and bundle are release assets whose SHA-256 digests and sizes are
returned by GitHub's authenticated Releases API.  Both values are checked
before parsing or unpacking; URLs supplied by the manifest are never trusted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import httpx

from utils.paths import get_app_data_dir, is_frozen
from version import FULL_VERSION

logger = logging.getLogger(__name__)

CHANNEL_TAG = "component-channel-v1"
MANIFEST_ASSET_NAME = "bananaflow-components.json"
RELEASE_API_URL = (
    "https://api.github.com/repos/BananaFlow-Media/BananaFlow/releases/tags/"
    + CHANNEL_TAG
)
_ASSET_API_PREFIX = (
    "https://api.github.com/repos/BananaFlow-Media/BananaFlow/releases/assets/"
)
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_EXPANDED_BYTES = 96 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# A fresh signed control record lets an installed overlay work offline for a
# limited period.  After that, failure to contact the official control plane
# disables only the optional overlay; the bundled components still work.
_CONTROL_MAX_AGE_SECONDS = 24 * 60 * 60
_CONTROL_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


class ComponentUpdateError(RuntimeError):
    """A safe, user-displayable component-channel failure."""


@dataclass(frozen=True)
class VerifiedAsset:
    name: str
    api_url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ComponentManifest:
    bundle_id: str
    bundle_asset: str
    bundle_size: int
    bundle_sha256: str
    min_app_version: str
    max_app_version_exclusive: str
    packages: tuple[tuple[str, str], ...]
    disabled: bool = False
    superseded_by: str = ""
    revoked_bundle_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallResult:
    bundle_id: str
    versions: tuple[tuple[str, str], ...]
    restart_required: bool = True


def _version_tuple(value: str) -> tuple[int, ...]:
    import re

    return tuple(int(part) for part in re.findall(r"\d+", value or ""))


def _version_at_least(value: str, floor: str) -> bool:
    left, right = _version_tuple(value), _version_tuple(floor)
    width = max(len(left), len(right))
    return bool(left and right) and left + (0,) * (width - len(left)) >= right + (0,) * (width - len(right))


def _version_before(value: str, ceiling: str) -> bool:
    left, right = _version_tuple(value), _version_tuple(ceiling)
    width = max(len(left), len(right))
    return bool(left and right) and left + (0,) * (width - len(left)) < right + (0,) * (width - len(right))


def _is_compatible(app_version: str, minimum: str, maximum: str) -> bool:
    return _version_at_least(app_version, minimum) and _version_before(app_version, maximum)


def _safe_bundle_id(value: object) -> str | None:
    text = value if isinstance(value, str) else ""
    if text and all(ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in text):
        return text
    return None


def component_root() -> Path:
    return get_app_data_dir() / "components" / "downloader"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_github_digest(value: object) -> str:
    text = str(value or "")
    algorithm, separator, digest = text.partition(":")
    if separator != ":" or algorithm.lower() != "sha256" or len(digest) != 64:
        raise ComponentUpdateError("GitHub did not provide a usable SHA-256 asset digest.")
    try:
        bytes.fromhex(digest)
    except ValueError as exc:
        raise ComponentUpdateError("GitHub returned a malformed asset digest.") from exc
    return digest.lower()


def _asset_from_release(release: dict[str, Any], name: str, maximum: int) -> VerifiedAsset:
    matches = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(matches) != 1:
        raise ComponentUpdateError(f"The official component channel does not contain exactly one {name} asset.")
    asset = matches[0]
    if asset.get("state") != "uploaded":
        raise ComponentUpdateError(f"The {name} asset is not ready.")
    try:
        size = int(asset["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ComponentUpdateError(f"The {name} asset has no valid size.") from exc
    if size <= 0 or size > maximum:
        raise ComponentUpdateError(f"The {name} asset size is outside the safe limit.")
    api_url = str(asset.get("url") or "")
    if not api_url.startswith(_ASSET_API_PREFIX) or not api_url[len(_ASSET_API_PREFIX):].isdigit():
        raise ComponentUpdateError(f"The {name} asset does not belong to the official repository API.")
    return VerifiedAsset(name, api_url, size, _parse_github_digest(asset.get("digest")))


def parse_manifest(raw: bytes, *, app_version: str | None = FULL_VERSION) -> ComponentManifest:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ComponentUpdateError("The component manifest exceeds the safe size limit.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComponentUpdateError("The component manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("channel") != CHANNEL_TAG:
        raise ComponentUpdateError("The component manifest schema or channel is unsupported.")

    bundle = data.get("bundle")
    compatibility = data.get("compatibility")
    packages = data.get("packages")
    if not isinstance(bundle, dict) or not isinstance(compatibility, dict) or not isinstance(packages, list):
        raise ComponentUpdateError("The component manifest is incomplete.")
    try:
        bundle_id = str(data["bundle_id"])
        asset_name = str(bundle["asset"])
        bundle_size = int(bundle["size"])
        bundle_sha = str(bundle["sha256"]).lower()
        minimum = str(compatibility["min_app_version"])
        maximum = str(compatibility["max_app_version_exclusive"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ComponentUpdateError("The component manifest contains invalid fields.") from exc

    if not _safe_bundle_id(bundle_id):
        raise ComponentUpdateError("The component bundle identifier is unsafe.")
    if asset_name != f"bananaflow-components-{bundle_id}.zip":
        raise ComponentUpdateError("The component bundle asset name does not match its identifier.")
    if bundle_size <= 0 or bundle_size > _MAX_BUNDLE_BYTES:
        raise ComponentUpdateError("The component bundle size is outside the safe limit.")
    if len(bundle_sha) != 64:
        raise ComponentUpdateError("The component bundle digest is malformed.")
    try:
        bytes.fromhex(bundle_sha)
    except ValueError as exc:
        raise ComponentUpdateError("The component bundle digest is malformed.") from exc

    parsed_packages: list[tuple[str, str]] = []
    for package in packages:
        if not isinstance(package, dict):
            raise ComponentUpdateError("The component package list is malformed.")
        name, version = str(package.get("name") or ""), str(package.get("version") or "")
        if name not in {"yt-dlp", "yt-dlp-ejs"} or not version:
            raise ComponentUpdateError("The component package list contains an unsupported entry.")
        parsed_packages.append((name, version))
    if {name for name, _ in parsed_packages} != {"yt-dlp", "yt-dlp-ejs"}:
        raise ComponentUpdateError("The component bundle must contain yt-dlp and yt-dlp-ejs.")

    disabled = data.get("disabled", False)
    revoked = data.get("revoked_bundle_ids", [])
    superseded_by = data.get("superseded_by", "")
    if not isinstance(disabled, bool) or not isinstance(revoked, list) or not isinstance(superseded_by, str):
        raise ComponentUpdateError("The component control state is malformed.")
    revoked_ids: list[str] = []
    for revoked_id in revoked:
        safe_id = _safe_bundle_id(revoked_id)
        if safe_id is None:
            raise ComponentUpdateError("The component control state contains an unsafe identifier.")
        revoked_ids.append(safe_id)
    if len(set(revoked_ids)) != len(revoked_ids):
        raise ComponentUpdateError("The component control state contains duplicate identifiers.")
    if app_version is not None and not _is_compatible(app_version, minimum, maximum):
        raise ComponentUpdateError(
            f"This component bundle is not compatible with BananaFlow {app_version}."
        )
    return ComponentManifest(
        bundle_id=bundle_id,
        bundle_asset=asset_name,
        bundle_size=bundle_size,
        bundle_sha256=bundle_sha,
        min_app_version=minimum,
        max_app_version_exclusive=maximum,
        packages=tuple(parsed_packages),
        disabled=disabled,
        superseded_by=superseded_by,
        revoked_bundle_ids=tuple(revoked_ids),
    )


def _download_asset(client: httpx.Client, asset: VerifiedAsset, headers: dict[str, str]) -> bytes:
    """Download exactly the authenticated asset size, never an unbounded body."""
    chunks: list[bytes] = []
    total = 0
    digest = hashlib.sha256()
    with client.stream("GET", asset.api_url, headers=headers) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > asset.size:
                raise ComponentUpdateError(
                    f"The downloaded {asset.name} exceeded its authenticated size."
                )
            digest.update(chunk)
            chunks.append(chunk)
    if total != asset.size:
        raise ComponentUpdateError(f"The downloaded {asset.name} size does not match GitHub metadata.")
    if digest.hexdigest() != asset.sha256:
        raise ComponentUpdateError(f"The downloaded {asset.name} failed SHA-256 verification.")
    return b"".join(chunks)


def _request_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"BananaFlow/{FULL_VERSION} component-updater",
    }


def _fetch_channel_manifest(client: httpx.Client) -> ComponentManifest:
    """Fetch and authenticate control metadata without downloading a bundle."""
    headers = _request_headers()
    response = client.get(RELEASE_API_URL, headers=headers)
    response.raise_for_status()
    release = response.json()
    if release.get("tag_name") != CHANNEL_TAG or release.get("draft") is True:
        raise ComponentUpdateError("The official component channel release is unavailable.")
    manifest_asset = _asset_from_release(release, MANIFEST_ASSET_NAME, _MAX_MANIFEST_BYTES)
    return parse_manifest(_download_asset(
        client, manifest_asset, {**headers, "Accept": "application/octet-stream"},
    ), app_version=None)


def _fetch_release_and_assets(client: httpx.Client) -> tuple[ComponentManifest, bytes]:
    headers = _request_headers()
    response = client.get(RELEASE_API_URL, headers=headers)
    response.raise_for_status()
    release = response.json()
    if release.get("tag_name") != CHANNEL_TAG or release.get("draft") is True:
        raise ComponentUpdateError("The official component channel release is unavailable.")

    manifest_asset = _asset_from_release(release, MANIFEST_ASSET_NAME, _MAX_MANIFEST_BYTES)
    manifest = parse_manifest(_download_asset(
        client, manifest_asset, {**headers, "Accept": "application/octet-stream"},
    ))
    if manifest.disabled:
        detail = f" Superseded by {manifest.superseded_by}." if manifest.superseded_by else ""
        raise ComponentUpdateError("The component update channel is temporarily disabled." + detail)
    if manifest.bundle_id in manifest.revoked_bundle_ids:
        raise ComponentUpdateError("The component update channel revoked its current bundle.")

    bundle_asset = _asset_from_release(release, manifest.bundle_asset, _MAX_BUNDLE_BYTES)
    if bundle_asset.size != manifest.bundle_size or bundle_asset.sha256 != manifest.bundle_sha256:
        raise ComponentUpdateError("GitHub metadata and the component manifest disagree about the bundle.")
    return manifest, _download_asset(
        client, bundle_asset, {**headers, "Accept": "application/octet-stream"},
    )


def _safe_extract_bundle(bundle: bytes, destination: Path) -> None:
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_ARCHIVE_MEMBERS:
            raise ComponentUpdateError("The component archive has an unsafe number of files.")
        expanded = sum(info.file_size for info in infos)
        if expanded > _MAX_EXPANDED_BYTES:
            raise ComponentUpdateError("The expanded component archive exceeds the safe limit.")
        for info in infos:
            raw_name = info.filename
            path = PurePosixPath(raw_name)
            unix_mode = (info.external_attr >> 16) & 0o170000
            if (
                not raw_name
                or "\\" in raw_name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
                or unix_mode == stat.S_IFLNK
            ):
                raise ComponentUpdateError("The component archive contains an unsafe path.")
            target = destination.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

    for required in ("yt_dlp/__init__.py", "yt_dlp_ejs/__init__.py"):
        if not (destination / required).is_file():
            raise ComponentUpdateError(f"The component archive is missing {required}.")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _tree_sha256(root: Path) -> str:
    """Digest extracted bytes and relative names to detect later corruption."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ComponentUpdateError("A prepared component overlay contains a symbolic link.")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _read_state(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "active.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("schema") == 1 else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _bundle_site_packages(root: Path, bundle_id: str) -> Path:
    return root / "bundles" / bundle_id / "site-packages"


def run_component_healthcheck(site_packages: Path) -> int:
    """Hidden subprocess target used before selecting a downloaded bundle."""
    try:
        resolved = site_packages.resolve(strict=True)
        sys.path.insert(0, str(resolved))
        import yt_dlp
        import yt_dlp_ejs

        if not getattr(yt_dlp, "version", None) or not Path(yt_dlp_ejs.__file__).is_file():
            return 2
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}):
            pass
        return 0
    except Exception:
        logger.exception("Downloaded component health check failed")
        return 2


def _healthcheck_command(site_packages: Path) -> list[str]:
    if is_frozen():
        return [sys.executable, "--component-healthcheck", str(site_packages)]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"), "--component-healthcheck", str(site_packages)]


def _run_healthcheck(site_packages: Path) -> None:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            _healthcheck_command(site_packages),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComponentUpdateError("The component health check could not complete.") from exc
    if result.returncode != 0:
        raise ComponentUpdateError("The downloaded components failed their isolated health check.")


def should_activate_component_overlay(*, argv: list[str], frozen: bool) -> bool:
    """Whether normal startup should select and clean an overlay.

    The frozen health-check subprocess receives a path below the overlay's
    private ``staging`` directory.  Normal activation deliberately removes
    abandoned staging data, so running it in that child would delete the
    bundle immediately before validating it.
    """
    return frozen and "--component-healthcheck" not in argv


def install_verified_component_update(
    *,
    root: Path | None = None,
    fetcher: Callable[[], tuple[ComponentManifest, bytes]] | None = None,
    healthcheck: Callable[[Path], None] = _run_healthcheck,
) -> InstallResult:
    """Download, verify, prepare, health-check and select a component bundle."""
    target_root = root or component_root()
    target_root.mkdir(parents=True, exist_ok=True)

    if fetcher is None:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            manifest, bundle = _fetch_release_and_assets(client)
    else:
        manifest, bundle = fetcher()

    if (
        manifest.disabled
        or manifest.bundle_id in manifest.revoked_bundle_ids
        or not _is_compatible(FULL_VERSION, manifest.min_app_version, manifest.max_app_version_exclusive)
    ):
        raise ComponentUpdateError("This component bundle is not eligible for activation.")
    if len(bundle) != manifest.bundle_size or _sha256(bundle) != manifest.bundle_sha256:
        raise ComponentUpdateError("The component bundle failed manifest integrity verification.")

    final_dir = target_root / "bundles" / manifest.bundle_id
    site_packages = final_dir / "site-packages"
    if not site_packages.is_dir():
        staging_parent = target_root / "staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=manifest.bundle_id + "-", dir=staging_parent))
        try:
            staged_site = staging / "site-packages"
            staged_site.mkdir()
            _safe_extract_bundle(bundle, staged_site)
            _atomic_write_json(staging / "manifest.json", {
                "schema": 1,
                "bundle_id": manifest.bundle_id,
                "sha256": manifest.bundle_sha256,
                "packages": dict(manifest.packages),
                "min_app_version": manifest.min_app_version,
                "max_app_version_exclusive": manifest.max_app_version_exclusive,
                "tree_sha256": _tree_sha256(staged_site),
            })
            healthcheck(staged_site)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, final_dir)
            except FileExistsError:
                pass
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    else:
        healthcheck(site_packages)

    state = _read_state(target_root)
    previous = state.get("active") if state.get("active") != manifest.bundle_id else state.get("previous")
    _atomic_write_json(target_root / "active.json", {
        "schema": 1,
        "active": manifest.bundle_id,
        "previous": previous or "",
    })
    _write_control(target_root, manifest)
    return InstallResult(manifest.bundle_id, manifest.packages)


def _write_control(root: Path, manifest: ComponentManifest, *, checked_at: int | None = None) -> None:
    _atomic_write_json(root / "control.json", {
        "schema": 1,
        "checked_at": int(time.time()) if checked_at is None else checked_at,
        "channel_disabled": manifest.disabled,
        "revoked_bundle_ids": list(manifest.revoked_bundle_ids),
    })


def _read_control(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((root / "control.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        checked_at = data.get("checked_at")
        revoked = data.get("revoked_bundle_ids")
        if (
            data.get("schema") != 1
            or not isinstance(checked_at, int)
            or checked_at < 0
            or not isinstance(data.get("channel_disabled"), bool)
            or not isinstance(revoked, list)
            or any(_safe_bundle_id(bundle_id) is None for bundle_id in revoked)
        ):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _fetch_current_control() -> ComponentManifest:
    with httpx.Client(timeout=_CONTROL_HTTP_TIMEOUT, follow_redirects=True) as client:
        return _fetch_channel_manifest(client)


def _control_for_activation(
    root: Path,
    *,
    fetcher: Callable[[], ComponentManifest],
    now: int | None = None,
) -> dict[str, Any] | None:
    current_time = int(time.time()) if now is None else now
    control = _read_control(root)
    if control is not None and 0 <= current_time - control["checked_at"] <= _CONTROL_MAX_AGE_SECONDS:
        return control
    try:
        manifest = fetcher()
        _write_control(root, manifest, checked_at=current_time)
    except (ComponentUpdateError, httpx.HTTPError, OSError, ValueError):
        logger.warning("Component control record is stale and could not be refreshed; using bundled components", exc_info=True)
        return None
    return _read_control(root)


def _valid_bundle(root: Path, bundle_id: object, control: dict[str, Any]) -> Path | None:
    if not isinstance(bundle_id, str) or not bundle_id:
        return None
    site_packages = _bundle_site_packages(root, bundle_id)
    marker_path = site_packages.parent / "manifest.json"
    required = (site_packages / "yt_dlp" / "__init__.py", site_packages / "yt_dlp_ejs" / "__init__.py")
    if not marker_path.is_file() or not all(path.is_file() for path in required):
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("schema") != 1 or marker.get("bundle_id") != bundle_id:
            return None
        minimum = marker.get("min_app_version")
        maximum = marker.get("max_app_version_exclusive")
        if (
            not isinstance(minimum, str)
            or not isinstance(maximum, str)
            or not _is_compatible(FULL_VERSION, minimum, maximum)
            or control["channel_disabled"]
            or bundle_id in control["revoked_bundle_ids"]
        ):
            return None
        expected_tree = str(marker.get("tree_sha256") or "")
        if len(expected_tree) != 64 or _tree_sha256(site_packages) != expected_tree:
            return None
    except (OSError, json.JSONDecodeError, ComponentUpdateError):
        return None
    return site_packages


def activate_component_overlay(
    *,
    root: Path | None = None,
    control_fetcher: Callable[[], ComponentManifest] = _fetch_current_control,
    now: int | None = None,
) -> Path | None:
    """Prepend the selected valid overlay, falling back atomically if needed."""
    target_root = root or component_root()
    # A hard stop during preparation can leave only unselected staging data.
    # It is never an activation candidate and is safe to discard on launch.
    staging = target_root / "staging"
    if staging.exists():
        try:
            shutil.rmtree(staging)
        except OSError:
            logger.warning("Could not remove abandoned component staging data", exc_info=True)
    state = _read_state(target_root)
    control = _control_for_activation(target_root, fetcher=control_fetcher, now=now)
    if control is None:
        return None
    active_id, previous_id = state.get("active"), state.get("previous")
    selected = _valid_bundle(target_root, active_id, control)
    selected_id = active_id
    if selected is None:
        selected = _valid_bundle(target_root, previous_id, control)
        selected_id = previous_id
        if selected is not None:
            _atomic_write_json(target_root / "active.json", {
                "schema": 1, "active": previous_id, "previous": "",
            })
            logger.warning("Rolled back invalid component overlay %r to %r", active_id, previous_id)
    if selected is None:
        return None
    selected_text = str(selected)
    if selected_text not in sys.path:
        sys.path.insert(0, selected_text)
    logger.info("Activated verified component overlay %s", selected_id)
    return selected
