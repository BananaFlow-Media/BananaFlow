"""
tests/test_packaging.py  –  Packaging manifest guards
========================================================================
Reliability-hardening Phase 5A: yt_dlp_ejs ships its YouTube JS
player/signature-solving scripts as *data* files (e.g.
yt_dlp_ejs/yt/solver/core.min.js), which PyInstaller's default
modulegraph scan cannot discover on its own. yt-dlp's own upstream
PyInstaller hook normally handles this, but packaging/bananaflow.spec also
collects it explicitly as a defensive duplicate (see the comment there)
— this test just guards against that line being silently removed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The source-level packaged smoke constructs the *full* AppWindow under the
# offscreen QPA platform. On macOS CI that segfaults during construction
# (returncode -11, before the smoke writes any result) — a PySide6/Qt
# offscreen-platform limitation on macOS, not a product defect: real macOS users
# run the cocoa platform with a real display, and the frozen .app is verified on
# macOS by the release workflow's packaged `bananaflow-cli --version` (which
# must match `version.FULL_VERSION`) and
# `--doctor` steps. Reaching the Tag Editor GUI on a real macOS display is a
# manual acceptance item (docs/release/RELEASING.md). This is
# a targeted, documented skip of two tests whose offscreen premise does not hold
# on macOS — not a blanket skip; every other test runs on macOS.
_skip_offscreen_gui_on_macos = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="offscreen full-AppWindow construction segfaults on macOS CI; the "
           "packaged .app CLI smoke covers macOS and the GUI smoke is a manual check",
)

# Top-level modules that must ship in the wheel for the console entry
# points to import. `main` and `cli` are the entry-point targets; the
# rest are what those two import (directly or transitively) from the
# repository root at module import time.
_REQUIRED_TOP_LEVEL_MODULES = {
    "main", "cli", "config", "config_migrate", "error_handler", "version",
}


def _pyproject_text() -> str:
    return (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_entry_point_modules_are_included_in_wheel():
    """A non-editable ``pip install`` used to produce broken ``bananaflow`` /
    ``bananaflow-cli`` scripts: packages.find only collects core*/ui*/utils*,
    so the top-level entry-point modules never made it into the wheel.
    Guard that every required top-level module is listed in
    ``[tool.setuptools] py-modules``.

    Parsed with a regex (not tomllib) so this also runs on Python 3.10.
    """
    text = _pyproject_text()
    m = re.search(r"(?ms)^py-modules\s*=\s*\[(.*?)\]", text)
    assert m is not None, "pyproject.toml is missing [tool.setuptools] py-modules"
    listed = set(re.findall(r'"([^"]+)"', m.group(1)))
    missing = _REQUIRED_TOP_LEVEL_MODULES - listed
    assert not missing, (
        f"py-modules is missing top-level modules required by the console "
        f"entry points: {sorted(missing)}"
    )


def test_console_entry_points_target_packaged_modules():
    """The [project.scripts] targets must be modules covered by py-modules."""
    text = _pyproject_text()
    targets = re.findall(r'(?m)^(?:bananaflow[\w-]*)\s*=\s*"([\w.]+):', text)
    assert targets, "pyproject.toml declares no bananaflow console scripts"
    m = re.search(r"(?ms)^py-modules\s*=\s*\[(.*?)\]", text)
    listed = set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()
    for module in targets:
        top_level = module.split(".")[0]
        assert top_level in listed, (
            f"console script targets {module!r} but {top_level!r} is not in py-modules"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Release workflow ↔ build script contract
# ──────────────────────────────────────────────────────────────────────────────
# The Windows release workflow once invoked PyInstaller directly, silently
# skipping the PO Token Provider / Deno staging that scripts/build_windows.ps1
# performs — shipping an EXE that contradicted the documented "PO-ready out
# of the box" promise. These guards keep CI delegated to the build script
# and keep the PO-readiness verification step in place.

def _windows_release_workflow() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8"
    )


def test_release_workflow_builds_via_build_script_not_raw_pyinstaller():
    workflow = _windows_release_workflow()
    assert "build_windows.ps1" in workflow, (
        "release-windows.yml must delegate the build to scripts/build_windows.ps1 "
        "so CI ships the same product a maintainer builds locally"
    )
    assert "python -m PyInstaller" not in workflow, (
        "release-windows.yml must not invoke PyInstaller directly — that "
        "bypasses the PO Token Provider / Deno staging in build_windows.ps1"
    )


def test_release_workflow_stages_deno_runtime():
    workflow = _windows_release_workflow()
    assert "fetch_deno_runtime.ps1" in workflow, (
        "release-windows.yml must stage the pinned, checksum-verified Deno "
        "runtime before building (build_windows.ps1 requires it)"
    )


def test_release_workflow_verifies_po_readiness_of_frozen_build():
    workflow = _windows_release_workflow()
    for marker in (
        "getpot_bgutil_script.py",
        "generate_once.ts",
        "runtime\\deno.exe",
        "--doctor",
        "PO Token Provider ready",
    ):
        assert marker in workflow, (
            f"release-windows.yml lost its PO-readiness verification "
            f"(missing marker: {marker!r})"
        )


def test_spec_explicitly_collects_yt_dlp_ejs_data_files():
    spec_text = (REPO_ROOT / "packaging" / "bananaflow.spec").read_text(encoding="utf-8")
    assert "collect_data_files('yt_dlp_ejs'" in spec_text, (
        "packaging/bananaflow.spec must explicitly collect yt_dlp_ejs's data "
        "files (its .js solver scripts) as a defensive duplicate of "
        "yt-dlp's own PyInstaller hook — see the comment above this "
        "line in the spec file for why."
    )


def test_every_custom_icon_svg_exists_and_is_bundled():
    """The Converter sidebar icon went missing in the packaged build because
    ui.app_window.CustomIcon loads an SVG from ui/assets/ at runtime, but the
    spec never bundled that folder — so the frozen app had no
    ui/assets/document_arrow_right_black.svg (Qt logged "Cannot open file"
    on every repaint). Guard both halves: every CustomIcon("name") used in
    the code has its black+white SVGs in source, AND the spec stages
    ui/assets into the bundle.
    """
    ui_root = REPO_ROOT / "ui"
    referenced: set[str] = set()
    for path in ui_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for match in re.finditer(r'CustomIcon\(\s*["\']([^"\']+)["\']', path.read_text(encoding="utf-8")):
            referenced.add(match.group(1))

    assert referenced, "expected at least one CustomIcon(...) reference in ui/"

    missing_files: list[str] = []
    for name in sorted(referenced):
        for variant in ("black", "white"):
            svg = ui_root / "assets" / f"{name}_{variant}.svg"
            if not svg.is_file():
                missing_files.append(str(svg.relative_to(REPO_ROOT)))
    assert not missing_files, (
        "CustomIcon references SVG files that do not exist in ui/assets/:\n  "
        + "\n  ".join(missing_files)
    )

    spec_text = (REPO_ROOT / "packaging" / "bananaflow.spec").read_text(encoding="utf-8")
    assert "_stage_tree(ROOT / 'ui' / 'assets', 'ui/assets')" in spec_text, (
        "packaging/bananaflow.spec must stage ui/assets into the bundle, or "
        "every CustomIcon (e.g. the Converter's) loses its icon in the frozen "
        "build even though the SVG exists in source."
    )


def test_spec_excludes_the_unused_qt_multimedia_duplicate_ffmpeg():
    """Issue #32: Qt Multimedia is never imported anywhere in this app (only
    pulled in because collect_submodules('qfluentwidgets') force-includes its
    unused qfluentwidgets.multimedia submodule), and bundles its own
    ~17.9 MB FFmpeg-backed plugin alongside yt-dlp's real ~192 MB FFmpeg.
    Verified live: a real PyInstaller build with this exclusion removes
    PySide6/plugins/multimedia/ffmpegmediaplugin.dll and the Qt6Multimedia*
    DLLs/pyd files, and the packaged Tag Editor smoke test (12/12 steps,
    including artwork image decode) still passes cleanly."""
    spec_text = (REPO_ROOT / "packaging" / "bananaflow.spec").read_text(encoding="utf-8")
    excludes_block = re.search(r"excludes\s*=\s*\[(.*?)\]", spec_text, re.S)
    assert excludes_block, "could not find the excludes = [...] list in packaging/bananaflow.spec"
    for module in ("PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "qfluentwidgets.multimedia"):
        assert f"'{module}'" in excludes_block.group(1), (
            f"{module!r} must be excluded from the PyInstaller build"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Issue #46: Playwright's browser cache ships an ffmpeg-* directory (~3.4 MB)
# used only for its screen-recording feature (record_video / record_har), which
# no BananaFlow code path calls (docs/performance/PACKAGE_AND_RUNTIME_PROFILE.md
# Section A.2). yt-dlp's real FFmpeg is staged separately and covers every
# actual media-handling need.
#
# There is more than one place that copies that cache into a shipped artifact,
# which is exactly how the first fix went wrong: it patched only
# packaging/bananaflow.spec (Windows) while .github/workflows/release-macos.yml
# kept copying "$CACHE"/ffmpeg-* into the .app, and the test only read the spec
# so it stayed green. The set below is asserted as a whole, so a third bundling
# site cannot be added without this test noticing and demanding the same
# exclusion.
# ──────────────────────────────────────────────────────────────────────────────

# path -> a substring proving that site skips the ffmpeg-* directory.
_PLAYWRIGHT_BUNDLING_SITES = {
    "packaging/bananaflow.spec": "p_dir.name.startswith('ffmpeg-')",
    ".github/workflows/release-macos.yml": 'grep -q \'^ffmpeg-\'',
}

# Files that merely name the cache directory (resolve a path, install into it)
# without copying it into a shipped artifact.
_MENTIONS_WITHOUT_BUNDLING = {
    "main.py", "cli.py", "scripts/install_playwright.ps1", "tests/test_packaging.py",
    "tests/test_channel_tab_discoverer_phase6.py",
}


def _files_mentioning_the_playwright_cache() -> set[str]:
    found = set()
    for pattern in ("*.py", "*.ps1", "*.yml", "*.spec", "*.sh", "*.bat"):
        for path in REPO_ROOT.rglob(pattern):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith((".", "venv/", "build/", "dist/")):
                continue
            try:
                if "ms-playwright" in path.read_text(encoding="utf-8", errors="ignore"):
                    found.add(rel)
            except OSError:
                continue
    # rglob skips dotted top-level dirs above, so add back the workflows we do want.
    for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        if "ms-playwright" in path.read_text(encoding="utf-8", errors="ignore"):
            found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_every_playwright_bundling_site_is_known():
    """A new place that copies Playwright's cache into a shipped artifact must
    be classified here — either as a bundling site (and then it has to exclude
    ffmpeg-*, see the test below) or as a mention that ships nothing."""
    unclassified = _files_mentioning_the_playwright_cache() - set(
        _PLAYWRIGHT_BUNDLING_SITES) - _MENTIONS_WITHOUT_BUNDLING
    assert not unclassified, (
        "these files reference Playwright's ms-playwright cache but are not "
        "classified in tests/test_packaging.py — if one of them copies the "
        f"cache into a shipped artifact it must exclude ffmpeg-* too: {sorted(unclassified)}"
    )


@pytest.mark.parametrize("rel_path, exclusion_marker", sorted(_PLAYWRIGHT_BUNDLING_SITES.items()))
def test_playwright_bundling_site_excludes_the_unused_recording_ffmpeg(rel_path, exclusion_marker):
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert exclusion_marker in text, (
        f"{rel_path} bundles Playwright's browser cache but no longer skips "
        f"its unused screen-recording ffmpeg-* directory (issue #46) — "
        f"expected to find {exclusion_marker!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Phase 14 (TE-PKG-01/02): the packaged build must be able to reach every
# module the Tag Editor imports. PyInstaller's modulegraph follows ordinary
# import statements, so a module is only reachable if production actually
# imports it — a file that exists but is never imported would be absent from
# the frozen build and fail only at runtime, in front of a user.
# ──────────────────────────────────────────────────────────────────────────────

_TAG_EDITOR_RUNTIME_MODULES = (
    # Phase 12 import/export/reports/playlists
    "core.metadata_csv", "core.metadata_io", "core.metadata_reports",
    "core.playlist_export", "core.preset_transfer",
    "ui.workers.metadata_io_worker", "ui.panels.metadata_editor.io_dialog",
    # Phase 13 filesystem monitoring
    "core.filesystem_monitoring", "core.file_refresh_service",
    "ui.services.filesystem_watch_service",
    "ui.workers.filesystem_refresh_worker",
    "ui.controllers.incremental_workspace_updater",
    "ui.dialogs.external_change_dialog",
    # Phase 14 accessibility helpers
    "ui.a11y", "ui.direction",
)

#: Lives under core/ but is a measurement harness, not shipped behaviour: the
#: Phase 13 benchmarks import it, production never does. Asserted below so the
#: distinction stays deliberate rather than becoming an accidental omission.
_BENCHMARK_ONLY_MODULES = ("core.tag_editor_performance",)


def test_tag_editor_runtime_modules_import_cleanly():
    """Every module the packaged Tag Editor needs must import on its own."""
    import importlib
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    for name in _TAG_EDITOR_RUNTIME_MODULES:
        assert importlib.import_module(name) is not None, name


def test_tag_editor_runtime_modules_are_reachable_from_the_entry_point():
    """A module PyInstaller cannot see from main.py will not ship.

    Walks real import statements from the application entry point, so a module
    that is only ever imported from a test would be caught here rather than in
    a packaged build.
    """
    import ast
    from collections import deque

    def module_path(name: str) -> Path | None:
        candidate = REPO_ROOT / Path(*name.split("/")) if "/" in name else None
        parts = name.split(".")
        for suffix in (Path(*parts).with_suffix(".py"), Path(*parts) / "__init__.py"):
            path = REPO_ROOT / suffix
            if path.is_file():
                return path
        return candidate

    def imported_names(path: Path) -> set[str]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            return set()
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        return {name for name in names
                if name.split(".")[0] in {"core", "ui", "utils", "config", "main", "cli"}}

    reached: set[str] = set()
    queue = deque(["main"])
    while queue:
        name = queue.popleft()
        if name in reached:
            continue
        path = module_path(name)
        if path is None or not path.is_file():
            continue
        reached.add(name)
        queue.extend(imported_names(path) - reached)

    missing = [name for name in _TAG_EDITOR_RUNTIME_MODULES if name not in reached]
    assert not missing, (
        "these modules are never imported from the entry point and would be "
        f"absent from a packaged build: {missing}")

    # The benchmark harness must stay out of the shipped graph: it exists to
    # measure the product, not to run inside it.
    leaked = [name for name in _BENCHMARK_ONLY_MODULES if name in reached]
    assert not leaked, f"benchmark-only modules reached from production: {leaked}"


def test_spec_bundles_the_qt_and_image_plugins_the_tag_editor_needs():
    spec_text = (REPO_ROOT / "packaging" / "bananaflow.spec").read_text(encoding="utf-8")
    # Artwork decoding and the Hebrew/theme resources must survive freezing.
    assert "mutagen" in spec_text, "mutagen must be collected for tag reading"
    assert "PySide6" in spec_text or "qfluentwidgets" in spec_text


# ──────────────────────────────────────────────────────────────────────────────
# TE-PKG-03: the previous packaged check proved the EXE starts and the module
# archive is complete, but never actually opened the Tag Editor, constructed a
# dialog, or decoded an image through the packaged Qt plugins. This exercises
# the real `--internal-smoke-test tag-editor` entry point (core/internal_smoke_test.py)
# from the source tree, which is what a packaged EXE also runs when invoked the
# same way. The packaged-executable run itself is a separate, manual evidence
# step (a recorded release-hardening decision)
# because it requires a fresh PyInstaller build, not something this suite builds.
# ──────────────────────────────────────────────────────────────────────────────


def _run_packaged_smoke(scratch, result_path):
    """Run the internal smoke and return (summary, completed_process).

    Reads the summary from BANANAFLOW_SMOKE_RESULT_FILE — the smoke's own primary,
    reliable channel — falling back to stdout. bananaflow.exe is windowed
    (console=False) so its stdout is not connected; the result file is what the
    real packaged run relies on, and a windowed offscreen process can also exit
    natively at Qt teardown *after* writing that file. Raises with the child's
    return code and stderr on failure, so a crash is diagnosable rather than an
    opaque "no JSON summary".
    """
    import json
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["BANANAFLOW_SMOKE_RESULT_FILE"] = str(result_path)
    # Pin the app-data dir into the scratch on every platform:
    # get_app_data_dir() keys on APPDATA (Windows), XDG_CONFIG_HOME (Linux) and
    # HOME (macOS, ~/Library), so set them all or the scratch stays empty and the
    # "wrote user data" assertion fails off-Windows.
    env["APPDATA"] = str(scratch)
    env["XDG_CONFIG_HOME"] = str(scratch)
    env["HOME"] = str(scratch)
    env["USERPROFILE"] = str(scratch)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--internal-smoke-test", "tag-editor"],
        cwd=str(REPO_ROOT), env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120)

    raw = None
    if result_path.exists():
        raw = result_path.read_text(encoding="utf-8")
    elif (start := proc.stdout.find("{")) != -1:
        raw = proc.stdout[start:]
    assert raw, (
        "the smoke produced no result file and no JSON on stdout "
        f"(returncode {proc.returncode}).\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(raw), proc


@_skip_offscreen_gui_on_macos
def test_internal_tag_editor_smoke_entry_point_succeeds(tmp_path):
    import json
    import os
    import subprocess
    import sys

    scratch = tmp_path / "appdata"
    scratch.mkdir()
    summary, _ = _run_packaged_smoke(scratch, tmp_path / "smoke-result.json")
    failed = [step for step in summary["steps"] if not step["ok"]]
    # A native exit during Qt/offscreen interpreter teardown is a documented,
    # pre-existing condition unrelated to this smoke test; what matters is that
    # every step completed and reported success before that happened, which the
    # result file (written before teardown) captures.
    assert summary["ok"] and not failed, f"smoke test steps failed: {failed}\nfull summary: {summary}"

    # No user data escaped the scratch APPDATA/HOME this test supplied.
    written = list(scratch.rglob("*"))
    assert written, "the smoke test did not write any user data at all"
    for path in written:
        assert str(path).startswith(str(scratch))

    # The draft store resolved inside the supplied APPDATA (F-13).
    isolation = [s for s in summary["steps"] if s["step"] == "draft_store_uses_app_data_dir"]
    assert isolation and isolation[0]["ok"], "the smoke must prove its own draft isolation"
    assert str(scratch) in isolation[0]["detail"]


@_skip_offscreen_gui_on_macos
def test_internal_smoke_ignores_a_draft_outside_its_appdata(tmp_path):
    """A draft in the launching user's home must not reach the packaged smoke.

    This is the F-13 regression. The draft store used to read ``Path.home()``
    regardless of ``APPDATA``, so a stray draft — in the audit, dead pytest
    garbage left behind by a crashed run — made the packaged release gate block
    forever on a modal recovery prompt nobody could answer, and the smoke could
    not isolate itself from it even though it controlled ``APPDATA``.
    """
    import json
    import os
    import subprocess
    import sys

    scratch = tmp_path / "appdata"
    fake_home = tmp_path / "home"
    scratch.mkdir()
    fake_home.mkdir()

    # A syntactically valid draft at the *legacy* location, outside APPDATA.
    legacy = fake_home / ".bananaflow" / "tag_drafts" / "tag_editor_pending.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "schema": 1, "created": "2026-01-01T00:00:00", "root": str(tmp_path),
        "session_id": "outsider", "app_version": "", "generation": 1, "revision": 1,
        "excluded_ids": [], "targets": {},
        "records": [{
            "item_id": 1, "field": "title", "original_value": "a",
            "previous_value": None, "proposed_value": "b", "operation": "set",
            "origin": "manual", "revision": 1, "excluded": False,
            "capability": "", "diagnostic": "", "source_provider": "",
            "source_attribution": "", "source_url": "",
        }],
    }), encoding="utf-8")

    result_path = tmp_path / "smoke-result.json"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["BANANAFLOW_SMOKE_RESULT_FILE"] = str(result_path)
    # HOME/USERPROFILE point at fake_home so the *legacy* draft path resolves
    # there. The canonical app-data key is pinned to scratch where the platform
    # honours it (APPDATA on Windows, XDG on Linux); on macOS the canonical
    # location is HOME/Library, i.e. under fake_home but distinct from the legacy
    # ~/.bananaflow dir — which is exactly the point: canonical, not legacy.
    env["APPDATA"] = str(scratch)
    env["USERPROFILE"] = str(fake_home)
    env["HOME"] = str(fake_home)
    env["XDG_CONFIG_HOME"] = str(scratch)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)

    # A timeout here is the exact failure being regressed against: before the
    # fix this call hung until killed (reproduced twice at 150s in the audit).
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--internal-smoke-test", "tag-editor"],
        cwd=str(REPO_ROOT), env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120)

    raw = result_path.read_text(encoding="utf-8") if result_path.exists() else (
        proc.stdout[proc.stdout.find("{"):] if "{" in proc.stdout else "")
    assert raw, (f"the smoke produced no result (returncode {proc.returncode}).\n"
                 f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    summary = json.loads(raw)
    failed = [step for step in summary["steps"] if not step["ok"]]
    assert summary["ok"] and not failed, f"smoke test steps failed: {failed}"

    isolation = [s for s in summary["steps"] if s["step"] == "draft_store_uses_app_data_dir"]
    assert isolation and isolation[0]["ok"]
    # The smoke's draft store must be the CANONICAL location, never the legacy
    # ~/.bananaflow/tag_drafts dir that the outside draft lives in.
    legacy_dir = str(fake_home / ".bananaflow" / "tag_drafts")
    assert legacy_dir not in isolation[0]["detail"], (
        "the smoke resolved its draft store into the legacy home-directory location"
    )

    # The outside draft was neither read into the smoke nor damaged by it.
    assert legacy.exists(), "the smoke must not touch a draft outside its app-data dir"
    assert json.loads(legacy.read_text(encoding="utf-8"))["session_id"] == "outsider"
