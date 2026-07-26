"""
utils/paths.py  –  Shared app-directory path helpers
=====================================================
Single source of truth for all paths under the BananaFlow app-data directory.
Also handles the frozen-EXE FFmpeg discovery used by core.downloader.
Zero GUI imports — pure stdlib only.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def get_app_data_dir() -> Path:
    """
    Return the platform-specific BananaFlow app-data directory.

    This is the single source of truth for the app-data location.
    ``config.py`` and ``utils.logging_config`` delegate here so all
    three never drift.

    Windows : %APPDATA%\\.bananaflow              (falls back to ~/.bananaflow)
    macOS   : ~/Library/Application Support/BananaFlow
    Linux   : $XDG_CONFIG_HOME/bananaflow         (falls back to ~/.bananaflow)
    """
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / ".bananaflow"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BananaFlow"
    # Linux / other POSIX: honour XDG when set, else hidden home dir.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "bananaflow"
    return Path.home() / ".bananaflow"


def get_app_cookies_path() -> Path:
    """Return the path where the cookie wizard saves Netscape-format cookies."""
    return get_app_data_dir() / "app_cookies.txt"


def get_app_browser_profile_dir() -> Path:
    """Return the persistent Chromium profile directory used by the cookie wizard.

    Keeping this separate from the user's real browser profile is the whole
    point: Playwright reads cookies straight from its own decrypted
    BrowserContext, so it never touches Chrome's DPAPI/App-Bound-Encryption
    protected cookie store. Persisting it (vs. a throwaway context) means
    Google's login/2FA/device-trust state survives across wizard runs.
    """
    return get_app_data_dir() / "browser_profile"


def get_log_dir() -> Path:
    """Return the directory used for rotating log files."""
    return get_app_data_dir() / "logs"


def get_tag_backup_dir() -> Path:
    """Return the directory used for tag-editor backup archives.

    Single source of truth for every backup read/write site (apply
    backups, restore pickers, the backup manager and restore journals),
    so Windows %APPDATA%, macOS Application Support and Linux XDG all
    resolve identically everywhere.
    """
    return get_app_data_dir() / "tag_backups"


def get_tag_action_presets_path() -> Path:
    """Return the tag-editor action-preset store path."""
    return get_app_data_dir() / "tag_action_presets.json"


def is_frozen() -> bool:
    """Return True when running from a PyInstaller-frozen EXE.

    Used by the update system to decide whether runtime components
    (yt-dlp / yt-dlp-ejs) can be upgraded in place with pip (source
    checkout) or only via a full app update (packaged build, where the
    dependencies are baked into the EXE).
    """
    return bool(getattr(sys, "frozen", False))


def get_install_dir() -> Path:
    """Return the directory the app is installed in.

    When running from a PyInstaller-frozen EXE, this is the folder
    containing ``bananaflow.exe``. When running from source, this is the
    repo root (the parent of the ``utils`` package).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_bundled_ffmpeg_dir() -> Optional[Path]:
    """Return the folder containing bundled ffmpeg.exe / ffprobe.exe, or None.

    The Windows EXE build script may copy LGPL FFmpeg binaries into
    ``packaging/ffmpeg/`` and PyInstaller relocates them to sit next
    to ``bananaflow.exe``. Source checkouts use the same convention if the
    developer dropped binaries there.

    Returns the directory path when both ``ffmpeg.exe`` and
    ``ffprobe.exe`` are present, otherwise ``None`` so yt-dlp falls
    back to PATH.
    """
    install = get_install_dir()
    candidates = [
        install,                  # next to bananaflow.exe (frozen install)
        install / "ffmpeg",       # nested folder (alternative layout)
        install / "packaging" / "ffmpeg",  # source checkout dev layout
    ]
    # PyInstaller 6.x's default one-folder layout collects bundled
    # binaries into an executable-adjacent "_internal" folder rather
    # than next to the EXE itself (the `--contents-directory` default).
    # sys._MEIPASS always points at wherever that actually is - the
    # same pattern core.runtime_components already uses for the PO
    # Token Provider/Deno discovery - so check it instead of hardcoding
    # the "_internal" folder name. Verified via a real Phase 5 build:
    # bananaflow.spec's binaries=[(ffmpeg_path, '.')] landed the files in
    # dist/bananaflow/_internal/, not dist/bananaflow/, so this candidate was
    # required for a real build to ever find its own bundled FFmpeg.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass))
    # macOS .app bundle: PyInstaller may place binaries under
    # Contents/MacOS (== install), Contents/Frameworks, or
    # Contents/Resources, with symlinks between them. Add all three so
    # discovery succeeds regardless of where PyInstaller dropped them.
    if sys.platform == "darwin" and install.name == "MacOS":
        contents = install.parent
        candidates += [
            contents / "Frameworks",
            contents / "Resources",
        ]
    suffix = ".exe" if os.name == "nt" else ""
    for d in candidates:
        ff = d / f"ffmpeg{suffix}"
        fp = d / f"ffprobe{suffix}"
        if ff.exists() and fp.exists():
            return d
    return None


def get_ffmpeg_executable() -> Optional[str]:
    """Return the path to ffmpeg, preferring the bundled binary.

    Used by ``error_handler.check_ffmpeg`` and the doctor diagnostic
    so the "FFmpeg: OK" report reflects what yt-dlp will actually
    invoke at runtime, not just whatever happens to be on PATH.
    """
    bundled = get_bundled_ffmpeg_dir()
    if bundled is not None:
        suffix = ".exe" if os.name == "nt" else ""
        ff = bundled / f"ffmpeg{suffix}"
        if ff.exists():
            return str(ff)
    return shutil.which("ffmpeg")


def get_ffprobe_executable() -> Optional[str]:
    """Return the path to ffprobe, preferring the bundled binary.

    Mirrors ``get_ffmpeg_executable`` so the converter's verification
    step probes with the same FFmpeg build that performed the encode.
    """
    bundled = get_bundled_ffmpeg_dir()
    if bundled is not None:
        suffix = ".exe" if os.name == "nt" else ""
        fp = bundled / f"ffprobe{suffix}"
        if fp.exists():
            return str(fp)
    return shutil.which("ffprobe")


# ──────────────────────────────────────────────────────────────────────────────
# Batch download workspace (core.download_orchestrator / core.downloader)
# ──────────────────────────────────────────────────────────────────────────────

_WORKSPACE_CONTAINER_NAME = ".bananaflow_tmp"
# The app-data fallback container, used when the same-volume container under
# the user's output directory cannot be created or hidden.
_APPDATA_CONTAINER_NAME = "download_workspaces"

# Persisted list of output directories BananaFlow has actually created a batch
# workspace under. This is what later makes workspace ownership *provable*:
# a directory is BananaFlow-owned because it is contained in a container we
# created under a root we recorded — not because its name happens to look
# like one of ours. It is also the only way to rediscover a workspace left
# under an output directory the user has since changed away from.
#
# Bounded, oldest-first: a user who keeps changing their output directory
# cannot grow the file without limit.
_OUTPUT_ROOTS_FILENAME = "known_output_roots.json"
_MAX_KNOWN_OUTPUT_ROOTS = 64


def _known_output_roots_path() -> Path:
    return get_app_data_dir() / _OUTPUT_ROOTS_FILENAME


def known_output_roots() -> list[Path]:
    """Every output directory BananaFlow has recorded a batch workspace
    under, oldest first. Never raises — a missing or corrupt file yields an
    empty list, which is the safe answer for both of its consumers (own
    nothing / discover nothing) rather than a startup failure."""
    import json

    try:
        raw = _known_output_roots_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning(
            "[paths] Ignoring corrupt known-output-roots file %s",
            _known_output_roots_path(),
        )
        return []
    if not isinstance(data, list):
        return []
    return [Path(item) for item in data if isinstance(item, str) and item.strip()]


def register_output_root(path) -> None:
    """Record ``path`` as an output directory BananaFlow owns a workspace
    container under.

    Idempotent and best-effort: a write failure is logged, never raised —
    losing the record costs a later cleanup sweep, never the correctness of
    the download itself. Written atomically so a crash mid-write cannot
    leave a half file that would read back as corrupt."""
    import json
    import tempfile

    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return
    entries = [str(p) for p in known_output_roots()]
    if str(resolved) in entries:
        return
    entries.append(str(resolved))
    if len(entries) > _MAX_KNOWN_OUTPUT_ROOTS:
        entries = entries[-_MAX_KNOWN_OUTPUT_ROOTS:]

    target = _known_output_roots_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, str(target))
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as exc:
        logger.warning("[paths] Could not record output root %s: %s", resolved, exc)


# ── Workspace ownership (the authority behind every workspace deletion) ──────

def _owned_workspace_containers() -> list[Path]:
    """Every workspace container BananaFlow actually owns right now: the
    fixed app-data fallback container, plus ``<root>/.bananaflow_tmp`` for
    each recorded output root (see register_output_root). Canonicalised, so
    the containment checks below compare real filesystem locations."""
    candidates = [get_app_data_dir() / _APPDATA_CONTAINER_NAME]
    for root in known_output_roots():
        candidates.append(root / _WORKSPACE_CONTAINER_NAME)
    containers: list[Path] = []
    for candidate in candidates:
        try:
            containers.append(candidate.resolve())
        except (OSError, RuntimeError):
            continue
    return containers


def _is_workspace_path_resolved(resolved: Path) -> bool:
    """Containment check on an ALREADY-CANONICALISED path. Never call this
    directly with an un-resolved path — see is_workspace_path."""
    for container in _owned_workspace_containers():
        if resolved == container or container in resolved.parents:
            return True
    return False


def is_workspace_path(path: Path) -> bool:
    """Whether ``path`` is inside (or is) a BananaFlow batch-workspace tree.

    Ownership is proven by CONTAINMENT under a container BananaFlow
    actually created — ``<recorded output root>/.bananaflow_tmp`` or the
    app-data ``download_workspaces`` — not by directory name. The earlier
    rule accepted any path whose own name started with ``batch-``, or any
    path with a component named ``.bananaflow_tmp`` /
    ``download_workspaces`` anywhere above it. Those are ordinary names a
    user's own library can contain: a folder called ``batch-2019``, or
    anything nested under a folder someone happened to call
    ``download_workspaces``, would have been handed straight to a recursive
    delete by the cancellation and stale-sweep cleanups. Being contained in
    a container we recorded creating is a fact about this installation, not
    a guess from a string.

    Resolves the path FIRST (collapsing ``..``/``.`` segments and symlinks)
    before checking containment — a lexical check on the path as given
    would accept a traversal-style path (e.g.
    ``.../.bananaflow_tmp/batch-x/../../../etc``) that actually lands
    outside any BananaFlow workspace. A path that cannot be resolved is
    treated as NOT a workspace path — fail closed."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    return _is_workspace_path_resolved(resolved)


def _prune_empty_workspace_parents(node: Path) -> None:
    """rmdir empty BananaFlow-owned parent directories, stopping at the first
    non-empty or non-owned level. Stops at the workspace container, so it can
    never reach the user's output directory (which is not contained in any
    container); rmdir also refuses any level a concurrent batch still
    occupies."""
    try:
        node = Path(node).resolve()
    except (OSError, RuntimeError):
        return
    while _is_workspace_path_resolved(node):
        try:
            node.rmdir()
        except OSError:
            break
        node = node.parent


def remove_workspace_tree(path: Path) -> None:
    """Recursively remove a BananaFlow workspace directory (a batch container
    or a single job's subdir) and peel back any now-empty owned parents.

    Resolves ``path`` once and uses that SAME canonical path for both the
    ownership check and the actual removal — see is_workspace_path for why
    a name-based check (or checking one path while deleting a different,
    un-resolved one) is not safe. Refuses to remove anything that does not
    resolve inside a container BananaFlow recorded creating, so a cleanup
    sweep can never escape into — and delete — the user's own files. Never
    raises: a cleanup failure must not turn an otherwise-fine batch into an
    error."""
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError) as exc:
        logger.warning("[paths] refusing to remove %s — could not resolve: %s", path, exc)
        return
    if not _is_workspace_path_resolved(resolved):
        logger.warning(
            "[paths] refusing to remove %s (resolved: %s) — not inside a "
            "BananaFlow-owned workspace container", path, resolved,
        )
        return
    try:
        shutil.rmtree(resolved, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[paths] workspace removal failed for %s: %s", resolved, exc)
        return
    _prune_empty_workspace_parents(resolved.parent)


def _set_hidden_attribute(
    path: Path, *, attempts: int = 3, retry_delay_s: float = 0.15, hidden: bool = True,
) -> bool:
    """Apply the Windows Hidden file attribute so the batch workspace never
    shows up in a normal Explorer/dir listing.

    Retries a few times: a directory that was just created can transiently
    fail this call (e.g. an antivirus/indexer briefly holding a handle right
    after creation) — a bare single attempt would report "exposed" for a
    purely transient condition that a millisecond-scale retry resolves.

    Returns True if the attribute ends up set (or if we're on a non-Windows
    platform, where the leading-dot name already hides the folder by
    convention — matches get_app_data_dir's ``.bananaflow``). Returns
    False, with a logged warning, only when every attempt genuinely failed —
    the caller stays functional either way (hiding is cosmetic; an
    un-hidden workspace still works and is still cleaned up), but a real,
    persistent failure is never silently reported as success.

    ``hidden=False`` clears the attribute instead. The cross-volume publish
    needs that: its staging temp is hidden from the moment it is created
    (so a partial copy is never visible), and the attribute has to come off
    again just before the temp is renamed into place — os.replace carries
    the source's attributes with it, so a still-hidden temp would publish
    the user's finished file as a hidden file.
    """
    if os.name != "nt":
        return True
    import ctypes
    FILE_ATTRIBUTE_HIDDEN = 0x02
    FILE_ATTRIBUTE_NORMAL = 0x80
    target_attrs = FILE_ATTRIBUTE_HIDDEN if hidden else FILE_ATTRIBUTE_NORMAL
    last_err = None
    for attempt in range(attempts):
        try:
            ok = ctypes.windll.kernel32.SetFileAttributesW(  # type: ignore[attr-defined]
                str(path), target_attrs
            )
            if ok:
                return True
            last_err = ctypes.windll.kernel32.GetLastError()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        if attempt < attempts - 1:
            time.sleep(retry_delay_s)
    logger.warning(
        "[paths] Could not %s Hidden attribute on %s after %d attempt(s) "
        "(%s); workspace will be visible but still functional",
        "set" if hidden else "clear", path, attempts, last_err,
    )
    return False


def _workspace_container(base: Path) -> Path:
    return base / _WORKSPACE_CONTAINER_NAME


def make_batch_workspace(base_output_dir: str) -> Path:
    """Create a fresh, uniquely-named batch workspace and return it.

    Downloads, conversion, artwork and metadata post-processing all happen
    here instead of directly inside the user's visible output folder — the
    finished file is only moved into the real output directory once it is
    completely ready (see core.downloader's atomic-publish step).

    Preferred location is ``base_output_dir/.bananaflow_tmp/batch-<id>`` so
    the workspace is on the same filesystem/volume as the final
    destination, making the later publish a pure atomic ``os.replace``.
    If that cannot be created (e.g. a read-only or attribute-restricted
    output dir), it falls back to the app-data directory — the download
    still stays fully isolated (never writes visible partials into the
    user's output folder); the publish step transparently handles the
    resulting cross-volume move. Only if BOTH locations fail does this
    raise OSError, so the caller never has to choose between "isolate" and
    "download at all" — it always isolates.

    The container is given the Windows Hidden attribute (not just a
    dot-prefixed name) so it never appears in a normal Explorer window.
    """
    import uuid

    name = f"batch-{uuid.uuid4().hex[:12]}"
    try:
        same_volume_root: Optional[Path] = Path(base_output_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        same_volume_root = None
    candidates = [
        _workspace_container(same_volume_root) if same_volume_root is not None else None,
        get_app_data_dir() / _APPDATA_CONTAINER_NAME,
    ]

    last_exc: Optional[OSError] = None
    for container in candidates:
        if container is None:
            continue
        try:
            workspace = container / name
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            last_exc = exc
            logger.warning(
                "[paths] Could not create batch workspace under %s: %s", container, exc,
            )
            continue
        # Hide the container (the boundary that keeps the whole subtree out
        # of a normal Explorer window) and the batch dir itself. Genuinely
        # hidden is a product requirement, not a cosmetic nicety — a
        # location that can't satisfy it is treated the same as one that
        # can't even create the directory: try the next candidate instead
        # of silently exposing the workspace.
        container_hidden = _set_hidden_attribute(container)
        workspace_hidden = _set_hidden_attribute(workspace)
        if container_hidden and workspace_hidden:
            # Record the root we actually placed a container under, so later
            # cleanup can PROVE this tree is ours (containment under a
            # recorded container) and can still find it after the user
            # changes their configured output directory. The app-data
            # fallback container needs no record — its location is fixed.
            if same_volume_root is not None and container.parent == same_volume_root:
                register_output_root(same_volume_root)
            return workspace
        shutil.rmtree(workspace, ignore_errors=True)
        last_exc = OSError(f"Could not apply the Hidden attribute under {container}")
        logger.warning(
            "[paths] Rejecting workspace location %s: could not be made hidden", container,
        )
    # Every candidate location either couldn't be created or couldn't be
    # hidden — surface it so the orchestrator errors the jobs rather than
    # writing partials into a workspace the user was promised is invisible.
    raise last_exc if last_exc is not None else OSError("no workspace location available")


# Matches core.downloader.DownloadEngine.PUBLISH_TMP_SUFFIX. Duplicated as a
# literal (not imported) so this stdlib-only module never has to import
# core.downloader, which pulls in yt-dlp.
_PUBLISH_TMP_SUFFIX = ".bananaflow-publish-tmp"


def sweep_stale_publish_temp_files(base_dirs) -> list[Path]:
    """Remove leftover cross-volume publish temp files directly in the given
    output directories — the residue of a crash between the copy and the
    final rename in DownloadEngine._atomic_place (same-volume publishes
    never create one; they're a pure rename). ``base_dirs`` is any iterable
    of output-dir paths; every recorded output root is swept as well, so a
    temp stranded under a directory the user has since changed away from is
    still reclaimed. Never raises; returns the paths removed."""
    removed: list[Path] = []
    seen: set[Path] = set()
    for base in list(base_dirs or ()) + known_output_roots():
        try:
            resolved = Path(base).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        try:
            entries = list(resolved.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(_PUBLISH_TMP_SUFFIX) and entry.is_file():
                try:
                    entry.unlink()
                    removed.append(entry)
                except OSError as exc:
                    logger.warning("[paths] Could not remove stale publish temp %s: %s", entry, exc)
    return removed
