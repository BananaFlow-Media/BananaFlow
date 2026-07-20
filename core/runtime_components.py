"""
core/runtime_components.py  –  Bundled downloader-component support
====================================================================
Lets a packaged BananaFlow build ship the important YouTube-reliability
components *inside the app* so a normal user gets reliable downloads out
of the box — no pip, no plugin folders, no manual PO Token setup.

Three things can be bundled next to the app, using mechanisms that are
already officially supported by yt-dlp (nothing here monkey-patches or
reaches into yt-dlp internals):

  1. A PO Token Provider plugin
     ------------------------------------------------------------------
     Dropped into a ``yt-dlp-plugins`` folder next to the executable.
     yt-dlp's own plugin loader auto-discovers that folder (it searches
     ``<executable_dir>/yt-dlp-plugins/*/yt_dlp_plugins/…`` with zero
     configuration — verified against yt_dlp/plugins.py:default_plugin_paths).
     A conforming provider self-registers on import; BananaFlow never
     generates, scrapes, stores, or injects PO Tokens itself.

  2. A JavaScript runtime (Deno / Node)
     ------------------------------------------------------------------
     Dropped into a ``runtime`` folder next to the executable. yt-dlp
     needs a JS runtime to run YouTube's player logic (via yt-dlp-ejs)
     and a PO Token Provider needs one to generate tokens. BananaFlow finds
     runtimes on PATH (see utils.yt_dlp_opts._detect_js_runtimes), so
     :func:`activate_bundled_components` simply prepends the bundled
     runtime directory to PATH — again no yt-dlp internals touched.

  3. The bgutil Deno script backend
     ------------------------------------------------------------------
     Dropped into ``pot-provider-backend`` and passed to yt-dlp via the
     provider's official ``youtubepot-bgutilscript:server_home`` extractor
     arg. This is script mode, not an HTTP server, so BananaFlow does not bind
     a port or manage a long-running provider process.

These are **packaging build inputs**: the public Windows build stages
them the same way it stages FFmpeg (see packaging/bananaflow.spec). This
module is purely about *locating, activating, and reporting* whatever
was bundled — it never downloads or installs anything at runtime.

Why bundling is safe here
-------------------------
* Legally: bgutil-ytdlp-pot-provider is GPL v3. BananaFlow's own code is
  GPL-3.0-or-later, so the plugin is license-compatible for a GPL
  release when the required notices and source-availability information
  are kept in THIRD_PARTY_NOTICES.md / SOURCE_OFFER.md. PO Tokens are an
  anti-bot attestation, not DRM; BananaFlow never generates, scrapes, stores,
  or injects them itself.
* Technically: a provider is functionally useless without a capable JS
  runtime, so :func:`detect_bundled_components` reports the two together
  and YouTube Doctor never claims a provider is "ready" when the runtime
  it depends on is missing.

Design
------
* Zero GUI imports — pure stdlib. Safe to import from core/utils.
* All filesystem-only: never imports or executes a bundled plugin
  (that's yt-dlp's job, on its own terms).
* :func:`activate_bundled_components` is idempotent and best-effort — any
  failure degrades to "nothing bundled", never an exception at startup.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utils.paths import get_app_data_dir, get_install_dir, is_frozen
from utils.yt_dlp_opts import _JS_RUNTIME_EXE_NAMES, _JS_RUNTIMES_PREFERENCE

# yt-dlp's official executable-adjacent plugin folder name.
_PLUGINS_FOLDER_NAME = "yt-dlp-plugins"
# Where a bundled JS runtime binary (deno / node) is staged.
_RUNTIME_FOLDER_NAMES = ("runtime", "yt-dlp-runtime")
_POT_BACKEND_FOLDER_NAME = "pot-provider-backend"
_BGUTIL_DIST_NAME = "bgutil-ytdlp-pot-provider"
_BGUTIL_SERVER_REL = Path(_BGUTIL_DIST_NAME) / "server"
_BGUTIL_DENO_SCRIPT_REL = Path("src") / "generate_once.ts"
_BGUTIL_NODE_MODULES_REL = Path("node_modules")


# ──────────────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BundledComponents:
    """What BananaFlow found bundled with this build."""
    frozen:                   bool = False
    plugins_dir:              Optional[Path] = None      # existing yt-dlp-plugins dir, if any
    provider_modules:         list[str] = field(default_factory=list)  # getpot_* modules found
    runtime_dir:              Optional[Path] = None       # existing bundled runtime dir, if any
    js_runtime_name:          str = ""                    # "deno" / "node" / "" if none bundled
    js_runtime_path:          Optional[Path] = None
    provider_backend_dir:     Optional[Path] = None       # bgutil server/ dir for script mode
    provider_backend_version: str = ""
    provider_backend_mode:    str = ""                    # "script-deno" when ready for script mode
    provider_script_path:     Optional[Path] = None
    provider_node_modules_dir: Optional[Path] = None
    provider_cache_home:      Optional[Path] = None

    @property
    def has_bundled_provider(self) -> bool:
        return bool(self.provider_modules)

    @property
    def has_bundled_script_provider(self) -> bool:
        return "getpot_bgutil_script" in self.provider_modules

    @property
    def has_bundled_js_runtime(self) -> bool:
        return bool(self.js_runtime_name and self.js_runtime_path)

    @property
    def has_bundled_provider_backend(self) -> bool:
        return bool(
            self.provider_backend_dir
            and self.provider_script_path
            and self.provider_script_path.exists()
            and self.provider_node_modules_dir
            and self.provider_node_modules_dir.is_dir()
        )

    @property
    def has_full_po_provider_stack(self) -> bool:
        return (
            self.has_bundled_script_provider
            and self.has_bundled_js_runtime
            and self.js_runtime_name == "deno"
            and self.has_bundled_provider_backend
        )


# ──────────────────────────────────────────────────────────────────────────────
# Location helpers
# ──────────────────────────────────────────────────────────────────────────────

def _candidate_plugins_dirs() -> list[Path]:
    """Every place a bundled ``yt-dlp-plugins`` folder might live.

    The first (executable-adjacent) is the one yt-dlp itself auto-scans in
    a frozen build; the others cover a PyInstaller layout that dropped it
    under ``_internal`` and a source-checkout staging area used for dev
    testing.
    """
    install = get_install_dir()
    candidates = [install / _PLUGINS_FOLDER_NAME]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / _PLUGINS_FOLDER_NAME)
    candidates.append(install / "packaging" / _PLUGINS_FOLDER_NAME)
    return candidates


def find_bundled_plugins_dir() -> Optional[Path]:
    """First existing bundled ``yt-dlp-plugins`` directory, or None.
    Never raises — any failure means 'nothing bundled'."""
    try:
        candidates = _candidate_plugins_dirs()
    except Exception:
        return None
    for d in candidates:
        try:
            if d.is_dir():
                return d
        except OSError:
            continue
    return None


def _candidate_runtime_dirs() -> list[Path]:
    install = get_install_dir()
    candidates: list[Path] = []
    for name in _RUNTIME_FOLDER_NAMES:
        candidates.append(install / name)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for name in _RUNTIME_FOLDER_NAMES:
            candidates.append(Path(meipass) / name)
    for name in _RUNTIME_FOLDER_NAMES:
        candidates.append(install / "packaging" / name)
    return candidates


def _candidate_provider_backend_dirs() -> list[Path]:
    install = get_install_dir()
    candidates = [install / _POT_BACKEND_FOLDER_NAME / _BGUTIL_SERVER_REL]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / _POT_BACKEND_FOLDER_NAME / _BGUTIL_SERVER_REL)
    candidates.append(install / "packaging" / _POT_BACKEND_FOLDER_NAME / _BGUTIL_SERVER_REL)
    return candidates


def find_bundled_runtime() -> Optional[tuple[str, Path]]:
    """Return (runtime_name, exe_path) for a bundled Deno/Node, or None.

    Honors utils.yt_dlp_opts's runtime preference order so BananaFlow never
    bundles a runtime it wouldn't actually select. Never raises.
    """
    suffix = ".exe" if os.name == "nt" else ""
    try:
        directories = _candidate_runtime_dirs()
    except Exception:
        return None
    for directory in directories:
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        for name in _JS_RUNTIMES_PREFERENCE:
            exe = directory / f"{_JS_RUNTIME_EXE_NAMES[name]}{suffix}"
            if exe.exists():
                return name, exe
    return None


def find_bundled_provider_backend() -> Optional[Path]:
    """Return the bundled bgutil server/ directory for script mode, or None."""
    try:
        directories = _candidate_provider_backend_dirs()
    except Exception:
        return None
    for directory in directories:
        try:
            if not directory.is_dir():
                continue
            script = directory / _BGUTIL_DENO_SCRIPT_REL
            node_modules = directory / _BGUTIL_NODE_MODULES_REL
            if script.exists() and node_modules.is_dir():
                return directory
        except OSError:
            continue
    return None


def _provider_backend_version(server_home: Path) -> str:
    package_json = server_home / "package.json"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
        version = data.get("version")
        return str(version) if version else ""
    except Exception:
        return ""


def _provider_cache_home() -> Path:
    return get_app_data_dir() / "cache"


def scan_bundled_provider_modules(plugins_dir: Optional[Path]) -> list[str]:
    """Return the ``getpot_*`` provider module names bundled under
    ``plugins_dir`` — filesystem inspection only, never an import.

    Layout scanned (yt-dlp's convention):
        <plugins_dir>/<anything>/yt_dlp_plugins/extractor/getpot_*[.py]
    """
    if plugins_dir is None:
        return []
    found: set[str] = set()
    try:
        for extractor_dir in plugins_dir.glob("*/yt_dlp_plugins/extractor"):
            if not extractor_dir.is_dir():
                continue
            for entry in extractor_dir.iterdir():
                name = entry.stem if entry.is_file() else entry.name
                if not name or name.startswith("_"):
                    continue
                if name.lower().startswith("getpot"):
                    found.add(name)
    except OSError:
        return sorted(found)
    return sorted(found)


# ──────────────────────────────────────────────────────────────────────────────
# Detection + activation
# ──────────────────────────────────────────────────────────────────────────────

def detect_bundled_components() -> BundledComponents:
    """Report what is bundled. Pure inspection — no side effects, never raises."""
    try:
        frozen = is_frozen()
    except Exception:
        frozen = False
    info = BundledComponents(frozen=frozen)

    plugins_dir = find_bundled_plugins_dir()
    if plugins_dir is not None:
        info.plugins_dir = plugins_dir
        info.provider_modules = scan_bundled_provider_modules(plugins_dir)

    runtime = find_bundled_runtime()
    if runtime is not None:
        name, exe = runtime
        info.runtime_dir = exe.parent
        info.js_runtime_name = name
        info.js_runtime_path = exe

    backend = find_bundled_provider_backend()
    if backend is not None:
        info.provider_backend_dir = backend
        info.provider_backend_version = _provider_backend_version(backend)
        info.provider_backend_mode = "script-deno"
        info.provider_script_path = backend / _BGUTIL_DENO_SCRIPT_REL
        info.provider_node_modules_dir = backend / _BGUTIL_NODE_MODULES_REL
        info.provider_cache_home = _provider_cache_home()

    return info


_activated = False


def activate_bundled_components() -> BundledComponents:
    """Make bundled components usable, then report them. Idempotent.

    * Prepends a bundled JS runtime directory to PATH so
      ``shutil.which`` (used by utils.yt_dlp_opts) selects it.
    * Best-effort registers the bundled plugins dir with yt-dlp for any
      layout where it isn't already the executable-adjacent folder
      yt-dlp auto-discovers (e.g. source-dev, or PyInstaller placing it
      under ``_internal``). Guarded — yt-dlp global layout differences
      never raise here.

    Call once, early in startup, before any yt-dlp use.
    """
    global _activated
    info = detect_bundled_components()
    if _activated:
        return info

    if info.runtime_dir is not None:
        runtime_dir = str(info.runtime_dir)
        current = os.environ.get("PATH", "")
        if runtime_dir not in current.split(os.pathsep):
            os.environ["PATH"] = runtime_dir + os.pathsep + current

    if info.provider_backend_dir is not None:
        _configure_provider_script_environment(info)

    if info.plugins_dir is not None:
        _register_plugin_dir(info.plugins_dir)

    _activated = True
    return info


def _configure_provider_script_environment(info: BundledComponents) -> None:
    """Keep the bgutil Deno script cache inside BananaFlow app data.

    The upstream script honors XDG_CACHE_HOME on all platforms. Setting it
    here makes the provider cache predictable and lets the plugin's own Deno
    permission arguments line up with the path the script will use.
    """
    cache_home = info.provider_cache_home or _provider_cache_home()
    try:
        cache_home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_home))
    os.environ.setdefault("DENO_NO_PROMPT", "1")
    os.environ.setdefault("DENO_NO_UPDATE_CHECK", "1")
    os.environ.setdefault("FORCE_COLOR", "false")


def bundled_pot_provider_extractor_args() -> dict[str, dict[str, list[str]]]:
    """yt-dlp extractor args needed for the bundled bgutil script backend.

    This uses the provider's official configuration surface:
    ``youtubepot-bgutilscript:server_home=<server/>``. It never generates,
    stores, reads, or injects a PO Token itself.
    """
    info = detect_bundled_components()
    if not info.has_bundled_provider_backend:
        return {}
    return {
        "youtubepot-bgutilscript": {
            "server_home": [str(info.provider_backend_dir)],
        },
    }


def bundled_pot_provider_script_command(info: Optional[BundledComponents] = None) -> list[str]:
    """Return the Deno command that validates the bundled script backend.

    The command mirrors bgutil's own Deno script-provider arguments and runs
    only ``generate_once.ts --version``. It is a health check, not a token
    generation request.
    """
    info = info or detect_bundled_components()
    if not (
        info.provider_backend_dir
        and info.provider_script_path
        and info.provider_node_modules_dir
        and info.js_runtime_path
        and info.js_runtime_name == "deno"
    ):
        return []
    cache_home = info.provider_cache_home or _provider_cache_home()
    script_cache = cache_home / _BGUTIL_DIST_NAME
    return [
        str(info.js_runtime_path),
        "run",
        "--allow-env",
        "--allow-net",
        f"--allow-ffi={info.provider_node_modules_dir}",
        f"--allow-write={script_cache}",
        f"--allow-read={script_cache},{info.provider_node_modules_dir}",
        str(info.provider_script_path),
        "--version",
    ]


def _register_plugin_dir(plugins_dir: Path) -> None:
    """Best-effort: add ``plugins_dir`` to yt-dlp's plugin search so a
    bundled provider is found regardless of where it was placed.

    In a frozen build the executable-adjacent folder is already scanned
    by ``default``; this mainly helps source-dev and non-standard
    layouts. Wrapped in try/except because ``yt_dlp.plugins.plugin_dirs``
    is an internal global whose shape may vary across yt-dlp versions —
    if it isn't there, the official auto-discovery still applies.
    """
    try:
        from yt_dlp import plugins as ytdlp_plugins

        current = list(ytdlp_plugins.plugin_dirs.value)
        target = str(plugins_dir)
        if target not in current:
            ytdlp_plugins.plugin_dirs.value = [*current, target]
            # Drop any cached "no plugins" result so the new dir is scanned.
            if hasattr(ytdlp_plugins, "all_plugins_loaded"):
                ytdlp_plugins.all_plugins_loaded.value = False
    except Exception:
        pass
