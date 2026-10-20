"""Hidden, local-only packaged updater test hooks.

No network or production release validation is bypassed by ordinary startup.
These hooks require an explicit CLI mode and a marked disposable sandbox;
their prepared local fixture still goes through the real artifact verifier,
prepared-state reader, support detection, detached helper and relaunch.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import traceback
import uuid


MARKER = ".bananaflow-update-smoke"
SENTINELS = ("downloads.db", "tag_backups/keep.bin", "paused_batches.json")


def fixture_request(path: Path) -> dict:
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("Smoke request must be an absolute, non-link path")
    request = json.loads(path.read_text(encoding="utf-8"))
    root = Path(request["root"])
    if (
        root.resolve() != root or not (root / MARKER).is_file()
        or not root.name.startswith("bf-upd-")
        or not path.is_relative_to(root)
    ):
        raise ValueError("Smoke request is not inside a marked disposable sandbox")
    for key in ("profile", "home", "appdata", "result"):
        value = Path(request[key])
        if value.resolve() != value or not value.is_relative_to(root):
            raise ValueError(f"Smoke {key} escapes its disposable sandbox")
    return request


def smoke_qt_platform() -> str:
    # The real macOS frameless window dereferences its winId as an NSView.
    # Qt's offscreen plugin cannot provide that native handle. Use the same
    # Cocoa backend as normal macOS startup, not a mocked/disabled window.
    return "cocoa" if sys.platform == "darwin" else "offscreen"


def configure_smoke_environment(result_path: Path) -> dict:
    """Run before logging, including when LaunchServices discards parent env."""
    request = fixture_request(result_path.parent / "request.json")
    if Path(request["result"]) != result_path:
        raise ValueError("Unexpected smoke result destination")
    os.environ["HOME"] = request["home"]
    os.environ["USERPROFILE"] = request["home"]
    os.environ["APPDATA"] = request["appdata"]
    os.environ["QT_QPA_PLATFORM"] = smoke_qt_platform()
    return request


def snapshot() -> dict:
    from config import AppConfig
    from utils.paths import get_app_data_dir
    from version import FULL_VERSION

    config = AppConfig()
    profile = get_app_data_dir().resolve()
    return {
        "version": FULL_VERSION,
        "executable": str(Path(sys.executable).resolve()),
        "pid": os.getpid(),
        "profile": str(profile),
        "preferences": {key: config.get(key) for key in (
            "language", "theme", "output_dir", "check_updates",
        )},
        "sentinels": {
            name: hashlib.sha256((profile / name).read_bytes()).hexdigest()
            for name in SENTINELS
        },
    }


def report_relaunch(result_path: Path) -> int:
    from core.app_update_helper import atomic_json
    from ui.app_update_smoke import run_gui_update_smoke

    request = configure_smoke_environment(result_path)
    try:
        # Exact-byte preservation is observed before normal startup can initialize
        # schemas or save window geometry. GUI health is independently required.
        result = snapshot()
        result["gui"] = run_gui_update_smoke()
        result["ok"] = (
            bool(getattr(sys, "frozen", False))
            and result["version"] == request.get("expected_relaunch_version", request["new_version"])
            and result["profile"] == request["profile"]
            and result["gui"].get("ok", False)
        )
    except Exception as error:
        # A windowed PyInstaller unhandled-error dialog can wait forever on CI.
        # Record only within the already validated sandbox and still fail.
        result = {"ok": False, "stage": "replacement-gui",
                  "error": str(error), "traceback": traceback.format_exc()}
    atomic_json(result_path, result)
    return 0 if result["ok"] else 1


def run_update_smoke() -> int:
    from core.app_update_helper import atomic_json

    request_path = Path(os.environ["BANANAFLOW_UPDATE_SMOKE_REQUEST"])
    request = fixture_request(request_path)
    try:
        return _run_update_smoke(request_path, request)
    except Exception as error:
        atomic_json(request_path.parent / "failure.json", {
            "ok": False, "stage": "old-application-handoff",
            "error": str(error), "traceback": traceback.format_exc(),
        })
        return 1


def _run_update_smoke(request_path: Path, request: dict) -> int:
    from core.app_update_helper import atomic_json, verify_artifact
    from core.app_updates import (
        app_update_support, get_prepared_app_update, launch_prepared_app_update,
    )
    from utils.paths import get_app_data_dir

    def phase(name: str) -> None:
        atomic_json(request_path.parent / "progress.json", {"phase": name})

    phase("validating-old-package")
    configure_smoke_environment(Path(request["result"]))
    before = snapshot()
    if not getattr(sys, "frozen", False) or before["version"] != request["old_version"]:
        raise RuntimeError("The old frozen package is not the requested fixture")
    phase("detecting-installation")
    support = app_update_support()
    if not support.supported or support.kind != request["kind"]:
        raise RuntimeError(f"Packaged update unsupported: {support}")
    root = Path(request["root"])
    if support.target is None or not support.target.is_relative_to(root):
        raise RuntimeError("Refusing to replace an application outside the sandbox")
    artifact = Path(request["artifact"])
    # The artifact comes from this job's package build, not a release API mock.
    phase("verifying-artifact")
    verify_artifact(artifact, request["size"], request["sha256"])
    update_root = get_app_data_dir().resolve() / "app-updates"
    work = update_root / uuid.uuid4().hex
    work.mkdir(parents=True)
    local_artifact = work / artifact.name
    shutil.copy2(artifact, local_artifact)
    helper = work / support.helper_path.name
    shutil.copy2(support.helper_path, helper)
    state = {
        "schema": 1, "version": request["new_version"], "kind": support.kind,
        "artifact": str(local_artifact), "size": request["size"],
        "sha256": request["sha256"], "target": str(support.target),
        "executable": support.executable, "install_mode": support.install_mode,
        "helper": str(helper),
    }
    atomic_json(work / "prepared.json", state)
    atomic_json(update_root / "prepared.json", {"schema": 1, "directory": work.name})
    phase("verifying-prepared-state")
    prepared = get_prepared_app_update()
    if prepared is None:
        raise RuntimeError("Production prepared-state verification rejected the fixture")
    from ui.app_update_smoke import run_gui_update_smoke
    phase("real-gui-safe-restart")
    gui = run_gui_update_smoke(prepared)
    if not gui.get("ok") or not gui.get("safe_restart_requested"):
        raise RuntimeError(f"The old packaged GUI refused safe restart: {gui}")
    # Compare the accepted, saved shutdown state, not pre-initialization bytes.
    before = snapshot()
    before["gui"] = gui
    atomic_json(request_path.parent / "before.json", before)
    phase("launching-helper")
    child = launch_prepared_app_update(prepared, smoke_result=Path(request["result"]))
    atomic_json(request_path.parent / "handoff.json", {
        "helper_pid": child.pid, "parent_pid": os.getpid(),
        "status_path": str(work / "status.json"),
    })
    phase("handoff-complete")
    # Returning releases all old-package handles. The helper must wait for this
    # actual frozen process, not a mocked PID, before installing/replacing.
    return 0
