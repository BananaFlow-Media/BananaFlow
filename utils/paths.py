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


def _set_hidden_attribute(path: Path) -> bool:
    """Apply the Windows Hidden file attribute so the batch workspace never
    shows up in a normal Explorer/dir listing.

    Returns True if the attribute is set (or if we're on a non-Windows
    platform, where the leading-dot name already hides the folder by
    convention — matches get_app_data_dir's ``.bananaflow``). Returns
    False, with a logged warning, when the Windows call genuinely failed —
    the caller stays functional either way (hiding is cosmetic; an
    un-hidden workspace still works and is still cleaned up), but a real
    failure is never silently reported as success.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x02
        # SetFileAttributesW returns 0 (FALSE) on failure. Checking the
        # return value is the whole point: without it a failed call was
        # indistinguishable from a successful one.
        ok = ctypes.windll.kernel32.SetFileAttributesW(  # type: ignore[attr-defined]
            str(path), FILE_ATTRIBUTE_HIDDEN
        )
        if not ok:
            err = ctypes.windll.kernel32.GetLastError()  # type: ignore[attr-defined]
            logger.warning(
                "[paths] Could not set Hidden attribute on %s (winerror=%s); "
                "workspace will be visible but still functional", path, err,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[paths] Setting Hidden attribute on %s raised %s; "
            "workspace will be visible but still functional", path, exc,
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
    candidates = [
        _workspace_container(Path(base_output_dir).expanduser().resolve()),
        get_app_data_dir() / "download_workspaces",
    ]

    last_exc: Optional[OSError] = None
    for container in candidates:
        try:
            workspace = container / name
            workspace.mkdir(parents=True, exist_ok=True)
            # Hide the container (the boundary that keeps the whole subtree
            # out of a normal Explorer window) and the batch dir itself.
            _set_hidden_attribute(container)
            _set_hidden_attribute(workspace)
            return workspace
        except OSError as exc:
            last_exc = exc
            logger.warning(
                "[paths] Could not create batch workspace under %s: %s", container, exc,
            )
    # Both the same-volume and the app-data fallback failed — surface it so
    # the orchestrator errors the jobs rather than writing visible partials.
    raise last_exc if last_exc is not None else OSError("no workspace location available")
