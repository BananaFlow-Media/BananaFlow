# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller spec for BananaFlow.

Windows
-------
Builds a one-folder distribution at ``dist/bananaflow/`` containing
``bananaflow.exe`` (GUI) + ``bananaflow-cli.exe`` plus every Qt/PySide6/
qfluentwidgets resource needed to run on a clean Windows machine.
Driven by ``scripts/build_windows.ps1`` (regenerates
``packaging/version_info.txt`` first).

macOS
-----
Builds ``dist/BananaFlow.app`` (a windowed .app bundle) with the headless
CLI binary alongside the GUI inside ``Contents/MacOS``. Driven by
``scripts/build_macos.sh`` (which then wraps the .app in a DMG).
Targets the host architecture (arm64 on Apple Silicon runners).

Both platforms
--------------
Staged FFmpeg binaries in ``packaging/ffmpeg/`` are bundled when
present, and Playwright Chromium (~300-400 MB) is bundled for fully
offline execution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

# ── Platform ───────────────────────────────────────────────────────────────
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# ── Paths ──────────────────────────────────────────────────────────────────
HERE = Path(SPECPATH).resolve()                 # packaging/
ROOT = HERE.parent                              # repo root

sys.path.insert(0, str(ROOT))                   # so version.py imports
from version import __version__, PRODUCT_NAME   # noqa: E402

# ── Hidden imports + data ──────────────────────────────────────────────────
# qfluentwidgets ships QSS / SVG resources via a generated Qt resource
# module loaded dynamically. PySide6 plugins (platforms, styles, etc.)
# are mostly auto-detected, but a few corner cases still need a nudge.

hiddenimports: list[str] = []
hiddenimports += collect_submodules('qfluentwidgets')
hiddenimports += collect_submodules('ytmusicapi')
hiddenimports += collect_submodules('mutagen')
# yt_dlp has a generated set of extractor modules; let PyInstaller's
# yt_dlp hook handle them when present, but force the top-level module
# in case the hook is missing on older PyInstaller versions.
hiddenimports += [
    'yt_dlp',
    'yt_dlp.utils',
    'syncedlyrics',
    'pyloudnorm',
    'soundfile',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
]

# NOTE on PO Token Provider plugins (yt_dlp_plugins.extractor.*) and
# frozen builds: no *hiddenimport* is added for a provider plugin — it
# isn't a Python import of this app, it's a yt-dlp plugin. yt-dlp's
# plugin loader (yt_dlp/plugins.py, pulled in transitively above)
# discovers plugins via sys.path *and* two OS-level directories:
# `%APPDATA%\yt-dlp\plugins\` and a `yt-dlp-plugins` folder next to the
# running executable (verified against default_plugin_paths()).
#
# This app bundles a provider the officially-supported way: the packaging
# staging step below copies packaging/yt-dlp-plugins/ into a
# `yt-dlp-plugins` folder next to bananaflow.exe, which yt-dlp can load after
# core.runtime_components registers the bundled plugin dir. A provider
# needs a JS runtime and backend too — see the `runtime` and
# `pot-provider-backend` staging below. See docs/release/RELEASING.md for the
# verification step and THIRD_PARTY_NOTICES.md for notices/source
# availability.

datas: list[tuple[str, str]] = []
datas += collect_data_files('qfluentwidgets')
datas += collect_data_files('ytmusicapi', includes=['locales/**/*'])
# yt_dlp ships extractor data; collect_data_files handles it.
datas += collect_data_files('yt_dlp')
# yt_dlp_ejs (YouTube JS player/signature solving) is a *separate*
# distribution from yt_dlp and ships its own bundled .js solver scripts
# (e.g. yt_dlp_ejs/yt/solver/core.min.js) as data, not Python code —
# PyInstaller's default modulegraph scan cannot discover them on its
# own. yt-dlp's own upstream PyInstaller hook (registered via the
# `pyinstaller40` entry point, auto-discovered — see
# yt_dlp/__pyinstaller/hook-yt_dlp.py in the installed package) already
# does `collect_data_files('yt_dlp_ejs', includes=['**/*.js'])`, so this
# line is a defensive duplicate: if that upstream hook is ever not
# picked up (older/incompatible PyInstaller, hook discovery disabled),
# the frozen build would otherwise *silently* lose PO Token/signature
# solving with no obvious error at runtime.
datas += collect_data_files('yt_dlp_ejs', includes=['**/*.js'])

# Bundled Playwright Chromium browser (~300-400 MB).
# Windows: Chromium on Windows is a flat folder of DLLs/EXEs that
# PyInstaller can handle without issues.
#
# macOS: Chromium is NOT added here. PyInstaller 6.x scans every file in
# datas for executable binaries and tries to codesign them. The nested
# Google Chrome for Testing.app/Contents/Frameworks structure cannot be
# re-codesigned with ad-hoc identity, causing a build failure.
# Solution: copy ms-playwright into the .app manually AFTER PyInstaller
# finishes (see the CI workflow step "Copy Chromium into bundle").
# main.py / cli.py set PLAYWRIGHT_BROWSERS_PATH = _MEIPASS/ms-playwright
# at startup so Playwright finds the browsers there.
if IS_WIN:
    _local_app_data = os.environ.get('LOCALAPPDATA') or os.path.join(
        os.environ.get('USERPROFILE', ''), 'AppData', 'Local'
    )
    ms_playwright_dir = Path(_local_app_data) / 'ms-playwright'
    if ms_playwright_dir.exists():
        for p_dir in ms_playwright_dir.iterdir():
            if not p_dir.is_dir() or p_dir.name == '.links':
                continue
            if p_dir.name.startswith('ffmpeg-'):
                # Playwright's own bundled ffmpeg is for screen-recording a
                # browser session -- a feature no BananaFlow code path calls
                # (issue #46; measured in docs/performance/PACKAGE_AND_RUNTIME_PROFILE.md
                # Section A.2). yt-dlp's own real FFmpeg (~192 MB, staged
                # separately below) covers every actual media-handling need.
                continue
            datas.append((str(p_dir), f"ms-playwright/{p_dir.name}"))
# Distribution metadata for packages that read their own version via
# importlib.metadata. Avoids ``PackageNotFoundError`` at runtime.
# yt-dlp and yt-dlp-ejs are also required by core.component_updates so
# the frozen build's component-update check can see what it bundles.
for pkg in ('yt-dlp', 'yt-dlp-ejs', 'mutagen', 'ytmusicapi', 'PySide6'):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        # copy_metadata raises on dotted/normalised name mismatches.
        # Best-effort: silently skip; the EXE will fall back to
        # version='unknown' for any package that needs it.
        pass

# Bundled FFmpeg / ffprobe (LGPL build). Stage them in
# packaging/ffmpeg/ before invoking PyInstaller; if the folder is
# absent we ship without them and the user gets the preflight warning
# at startup. Binary names differ by platform (no .exe on macOS).
# utils.paths.get_bundled_ffmpeg_dir() knows every place PyInstaller
# may drop them inside a .app bundle.
FFMPEG_DIR = HERE / 'ffmpeg'
_ffmpeg_names = ('ffmpeg.exe', 'ffprobe.exe') if IS_WIN else ('ffmpeg', 'ffprobe')
binaries: list[tuple[str, str]] = []
if FFMPEG_DIR.exists():
    for name in _ffmpeg_names:
        src = FFMPEG_DIR / name
        if src.exists():
            # Place the binaries inside the app folder so the runtime
            # can locate them next to the executable.
            binaries.append((str(src), '.'))

# ── Bundled downloader components (opt-in, staged like FFmpeg) ─────────────
# BananaFlow can ship the important YouTube-reliability components *inside*
# the app so a normal user gets reliable downloads out of the box (no
# pip, no plugin folders, no manual PO Token setup). The public Windows
# build requires the bgutil plugin, Deno runtime, and Deno script backend.
# core.runtime_components locates, activates, and configures them for
# yt-dlp's official provider mechanism.
#
#   packaging/yt-dlp-plugins/<pkg>/yt_dlp_plugins/extractor/getpot_*.py
#       A PO Token Provider plugin (e.g. bgutil-ytdlp-pot-provider — GPL
#       v3, see THIRD_PARTY_NOTICES.md for the license notice. Staged by
#       packaging/stage_pot_provider.py from the pinned PyPI package.
#       Copied next to the executable into a `yt-dlp-plugins` folder,
#       which yt-dlp's own loader auto-discovers with no configuration.
#
#   packaging/runtime/<deno|node>[.exe]
#       A JavaScript runtime a PO Token Provider / yt-dlp-ejs needs to
#       run YouTube's player logic. Copied into a `runtime` folder that
#       core.runtime_components prepends to PATH at startup.
#
#   packaging/pot-provider-backend/bgutil-ytdlp-pot-provider/server/
#       Upstream bgutil Deno script backend plus production node_modules.
#       BananaFlow passes this path to yt-dlp as youtubepot-bgutilscript:
#       server_home. No PO Token is generated, stored, or injected by
#       BananaFlow itself.
#
# License notices/source handling for any bundled third-party
# binaries/plugins must be added to THIRD_PARTY_NOTICES.md before
# shipping.
def _stage_tree(src_root: Path, dest_prefix: str) -> list[tuple[str, str]]:
    """Return datas entries copying every file under ``src_root`` while
    preserving its layout under ``dest_prefix`` (raw copy — no PyInstaller
    binary analysis, so a standalone runtime is shipped verbatim)."""
    entries: list[tuple[str, str]] = []
    if not src_root.exists():
        return entries
    for path in src_root.rglob('*'):
        if path.is_file():
            rel_parent = path.parent.relative_to(src_root)
            dest = dest_prefix if rel_parent == Path('.') else f'{dest_prefix}/{rel_parent.as_posix()}'
            entries.append((str(path), dest))
    return entries

datas += _stage_tree(HERE / 'yt-dlp-plugins', 'yt-dlp-plugins')
datas += _stage_tree(HERE / 'runtime', 'runtime')
datas += _stage_tree(HERE / 'pot-provider-backend', 'pot-provider-backend')

# The app's own custom icons. ui.app_window.CustomIcon loads an SVG at
# runtime from a path built off ui/app_window.py's __file__
# (<module_dir>/assets/<name>_<black|white>.svg), so the frozen build needs
# ui/assets/ dropped next to the ui package inside _internal. Without this
# the Converter's sidebar icon silently fails to load and Qt logs, on every
# repaint, "Cannot open file ...ui\assets\document_arrow_right_black.svg".
# It is the only nav item using a local-SVG CustomIcon (all others are
# qfluentwidgets FluentIcons, bundled with that package), which is why only
# the Converter icon went missing in the packaged build.
datas += _stage_tree(ROOT / 'ui' / 'assets', 'ui/assets')

# Application icon — production derivatives taken from the approved
# BananaFlow brand asset package (see packaging/BRAND_ASSETS.md for
# provenance). Windows uses .ico, macOS uses .icns.
ICON = HERE / ('bananaflow.ico' if IS_WIN else 'bananaflow.icns')
icon_path = str(ICON) if ICON.exists() else None

# Generated VS_VERSIONINFO (Windows-only). The build script writes this
# file just before PyInstaller runs; it is meaningless on macOS.
VERSION_FILE = HERE / 'version_info.txt'
version_file = str(VERSION_FILE) if (IS_WIN and VERSION_FILE.exists()) else None

# ── Excludes ───────────────────────────────────────────────────────────────
# Pull these modules OUT of the bundle. They are dev-only or pulled in
# transitively but never used at runtime by the GUI.

excludes = [
    'tkinter',
    'pytest',
    'pytest_mock',
    'unittest',
    # numpy/scipy/matplotlib are not direct deps; if a transitive dep
    # pulls them, the EXE gets large for no reason.
    'matplotlib',
    'tests',
    'tools',
    # Qt Multimedia is never imported by this app (verified: no reference
    # anywhere in core/, ui/, utils/, main.py, cli.py). It's only pulled in
    # because collect_submodules('qfluentwidgets') above force-includes
    # qfluentwidgets.multimedia, an unused submodule of a dependency we
    # never call into. Its own bundled FFmpeg-backed Qt plugin
    # (PySide6/plugins/multimedia/ffmpegmediaplugin.dll, ~17.9 MB) is a
    # second, unrelated FFmpeg copy alongside yt-dlp's real ~192 MB one
    # (issue #32). Excluding here removes that plugin and its Qt6Multimedia*
    # DLLs/pyd files from the bundle; qfluentwidgets.multimedia itself is
    # never invoked at runtime, so losing its (already-broken-without-this-
    # dependency) import has no effect.
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'qfluentwidgets.multimedia',
]

# ── Analysis ───────────────────────────────────────────────────────────────

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ── EXE ────────────────────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='bananaflow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX trips many AV scanners; not worth the size win.
    console=False,       # GUI app — no console window.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
    version=version_file,
)

# ── Second EXE: headless CLI sharing the same Analysis ─────────────────────
# Runs the same backend (DownloadOrchestrator, PlaylistParser, …) with
# the cli.py entry point. console=True so users get stdout/stderr.
# Reusing ``pyz`` means there is no second copy of the Python runtime —
# both EXEs share every bundled module.

cli_a = Analysis(
    [str(ROOT / 'cli.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
cli_pyz = PYZ(cli_a.pure)

cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name='bananaflow-cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,        # CLI needs a console window.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
    version=version_file,
)

# ── COLLECT (one-folder dist with both executables) ────────────────────────
# Windows  → dist/bananaflow/   (bananaflow.exe + bananaflow-cli.exe)
# macOS    → dist/bananaflow/   then wrapped into dist/BananaFlow.app by BUNDLE
#
# Note: On macOS, we use codesign_identity=None to skip code-signing binaries
# (which would fail on Playwright Chromium bundles that are already complex).
# We'll ad-hoc sign the final .app bundle after PyInstaller finishes instead.

coll = COLLECT(
    exe,
    cli_exe,
    a.binaries,
    a.datas,
    cli_a.binaries,
    cli_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='bananaflow',
    codesign_identity=None if IS_MAC else None,  # Skip signing Playwright et al.
)

# ── BUNDLE (macOS .app) ────────────────────────────────────────────────────
# Wrap the collected folder into a proper .app. The CLI binary travels
# inside Contents/MacOS so power users can still invoke
# ``BananaFlow.app/Contents/MacOS/bananaflow-cli``.
if IS_MAC:
    app = BUNDLE(
        coll,
        name='BananaFlow.app',
        icon=icon_path,
        bundle_identifier='media.bananaflow.app',
        version=__version__,
        info_plist={
            'CFBundleName': PRODUCT_NAME,
            'CFBundleDisplayName': PRODUCT_NAME,
            'CFBundleShortVersionString': __version__,
            'CFBundleVersion': __version__,
            'NSHighResolutionCapable': True,
            # The app uses Qt's own dark/light handling, so allow the
            # system appearance instead of forcing legacy Aqua.
            'NSRequiresAquaSystemAppearance': False,
            # Minimum supported macOS (Big Sur — first Apple Silicon OS).
            'LSMinimumSystemVersion': '11.0',
            # No special hardware/entitlement claims; this is a plain
            # GUI download utility.
            'LSApplicationCategoryType': 'public.app-category.utilities',
        },
    )
