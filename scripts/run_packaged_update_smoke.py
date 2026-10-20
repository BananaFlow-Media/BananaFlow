"""Exercise actual platform installation, replacement and packaged relaunch.

No release is published. Everything runs in a marked disposable user profile.
The published baseline intentionally tests migration via the candidate helper:
older public binaries do not contain an application updater.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.app_update_smoke import MARKER, SENTINELS, smoke_qt_platform  # noqa: E402


# Reviewed, publicly released baseline, pinned independently of the CI network
# response. A moved/replaced GitHub asset must fail closed.
BASELINE_VERSION = "1.2.0"
BASELINE = {
    "windows-portable": ("windows-x64-portable.zip", 890441428,
        "a75b942d0b5dcbdde39f290cfd721f5554139d998193a31a21b97b6e38d20cf6"),
    "windows-installed": ("windows-x64-setup.exe", 495235905,
        "97b7070392ff79d0827e4e003135d84288363bd5308cbe097bf2612517c9665a"),
    "macos-app": ("macos-arm64.dmg", 519385631,
        "8d1d686b41e170e675e9eeea83ba4140106ca46ebc7e31aa713954990601a687"),
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@contextmanager
def disposable_sandbox():
    """Retain a failed fixture while its helper may still be recovering.

    An error status precedes the helper's old-app recovery attempt. Deleting
    its working tree then would race that recovery and could hide the primary
    exception with a Windows directory-in-use error. These marked profiles
    contain only synthetic test data and are discarded with the CI runner.
    Successful runs still require complete cleanup.
    """
    root = Path(tempfile.mkdtemp(prefix="bf-upd-")).resolve()
    try:
        yield root
    except BaseException:
        print(
            f"Preserving failed updater sandbox until runner teardown: {root}. "
            "No helper or application process was terminated.",
            file=sys.stderr, flush=True,
        )
        raise
    else:
        try:
            shutil.rmtree(root)
        except OSError:
            print(f"Successful updater sandbox cleanup failed: {root}",
                  file=sys.stderr, flush=True)
            raise


def run(args: list[str], *, env: dict | None = None, timeout: int = 600) -> str:
    try:
        completed = subprocess.run(args, env=env, check=True, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.CalledProcessError as error:
        # These commands run only in the marked disposable fixture. Preserve
        # native tool/GUI diagnostics in the CI job log rather than reporting
        # an opaque exit code after capture_output swallowed the root cause.
        print(f"Failed command ({error.returncode}): {args}", file=sys.stderr, flush=True)
        for output in (error.stdout, error.stderr):
            if output:
                print(output, file=sys.stderr, flush=True)
        raise
    return completed.stdout


def find_artifact(folder: Path, kind: str) -> Path:
    ending = BASELINE[kind][0]
    matches = list(folder.glob(f"BananaFlow-v*-{ending}"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {ending} artifact in {folder}")
    return matches[0].resolve()


def fetch_baseline(folder: Path, kind: str) -> Path:
    ending, size, expected = BASELINE[kind]
    name = f"BananaFlow-v{BASELINE_VERSION}-{ending}"
    destination = folder / name
    url = f"https://github.com/BananaFlow-Media/BananaFlow/releases/download/v{BASELINE_VERSION}/{name}"
    request = urllib.request.Request(url, headers={"User-Agent": "BananaFlow-packaged-update-CI"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        received = 0
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > size:
                raise RuntimeError("Published baseline exceeds its pinned size")
            output.write(chunk)
    if destination.stat().st_size != size or digest(destination) != expected:
        raise RuntimeError("Published baseline checksum/size mismatch")
    return destination


def install(artifact: Path, target: Path, kind: str, evidence: Path) -> None:
    if kind == "windows-installed":
        run([str(artifact), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             "/SP-", "/CURRENTUSER", f"/DIR={target}", f"/LOG={evidence / 'install.log'}"])
    elif kind == "windows-portable":
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(target)
    else:
        mount = target.parent / "initial-mount"
        mount.mkdir()
        attached = False
        try:
            run(["hdiutil", "attach", str(artifact), "-readonly", "-nobrowse",
                 "-noautoopen", "-mountpoint", str(mount)])
            attached = True
            run(["ditto", str(mount / "BananaFlow.app"), str(target)])
        finally:
            if attached:
                run(["hdiutil", "detach", str(mount)])
            mount.rmdir()


def seed_profile(case: Path) -> tuple[Path, dict]:
    from config_migrate import CURRENT_VERSION

    home, appdata = case / "home", case / "appdata"
    home.mkdir()
    appdata.mkdir()
    profile = (appdata / ".bananaflow" if sys.platform == "win32"
               else home / "Library/Application Support/BananaFlow")
    profile.mkdir(parents=True)
    downloads = case / "downloads"
    downloads.mkdir()
    (downloads / "do-not-touch.mp3").write_bytes(b"existing-user-download")
    write_json(profile / "config.json", {
        "config_version": CURRENT_VERSION, "language": "he", "theme": "dark",
        "output_dir": str(downloads), "check_updates": False,
    })
    with sqlite3.connect(profile / "downloads.db") as database:
        database.execute("CREATE TABLE updater_canary (value TEXT NOT NULL)")
        database.execute("INSERT INTO updater_canary VALUES ('keep-history')")
    (profile / "tag_backups").mkdir()
    (profile / "tag_backups/keep.bin").write_bytes(b"existing-backup-\x00\xff")
    (profile / "paused_batches.json").write_text('{"fixture":"keep-paused-work"}', encoding="utf-8")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), APPDATA=str(appdata),
               QT_QPA_PLATFORM=smoke_qt_platform())
    return profile, env


def fingerprint(profile: Path) -> dict:
    config = json.loads((profile / "config.json").read_text(encoding="utf-8"))
    return {
        "preferences": {name: config[name] for name in (
            "language", "theme", "output_dir", "check_updates")},
        "sentinels": {name: digest(profile / name) for name in SENTINELS},
    }


def wait_for_result(result: Path, status: Path, timeout: int = 600, *, expected_error: bool = False) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status.is_file():
            value = json.loads(status.read_text(encoding="utf-8"))
            if value.get("status") == "error" and not expected_error:
                raise RuntimeError(f"Updater failed: {value}")
        if result.is_file():
            value = json.loads(result.read_text(encoding="utf-8"))
            if not value.get("ok"):
                raise RuntimeError(f"Relaunched package failed: {value}")
            return value
        time.sleep(0.5)
    raise TimeoutError("The replacement application did not report successful relaunch")


def wait_for_process_exit(pid: int, *, timeout: int = 60) -> None:
    """Observe termination before another GUI run or filesystem cleanup."""
    from core.app_update_helper import process_alive

    deadline = time.monotonic() + timeout
    while process_alive(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError("The relaunched GUI did not finish process teardown")
        time.sleep(0.2)


def verify_native_gui_exit(request: dict, env: dict, evidence: Path) -> dict:
    """Supervise the installed new GUI to prove native exit, not only a JSON write.

    The actual updater relaunch is separately tested through ShellExecute or
    LaunchServices. Those APIs do not expose its exit code to this harness, so
    a second run of the identical installed GUI must also terminate cleanly.
    """
    case = Path(request["result"]).parent / "native-exit"
    case.mkdir()
    native_request = {**request, "result": str(case / "relaunch.json")}
    write_json(case / "request.json", native_request)
    output = run([
        str(Path(request["target"]) / request["executable"]),
        "--app-update-smoke-result", native_request["result"],
    ], env=env, timeout=180)  # check=True requires exit zero, including teardown
    result = json.loads(Path(native_request["result"]).read_text(encoding="utf-8"))
    if (
        not result.get("ok") or not result.get("gui", {}).get("shutdown_accepted")
        or result["gui"].get("qt_platform") != smoke_qt_platform()
    ):
        raise RuntimeError("The installed replacement GUI did not close safely")
    result["supervised_process_exit_code"] = 0
    write_json(evidence / "replacement-native-exit.json", result)
    (evidence / "replacement-native-exit.stdout.txt").write_text(output, encoding="utf-8")
    return result


def exercise_rejected_artifact(request: dict, current_dist: Path, env: dict, cli: Path, evidence: Path) -> dict:
    """A real packaged helper must reject corrupt integrity and restore the GUI."""
    from core.app_update_helper import atomic_json

    case = Path(request["result"]).parent / "rejected-artifact"
    case.mkdir()
    failure_request = {
        **request, "result": str(case / "relaunch.json"),
        "expected_relaunch_version": request["old_version"],
    }
    atomic_json(case / "request.json", failure_request)
    artifact = Path(request["artifact"])
    local = case / artifact.name
    try:
        os.link(artifact, local)
    except OSError:
        shutil.copy2(artifact, local)
    relative = ("BananaFlow.app/Contents/MacOS/bananaflow-updater"
                if sys.platform == "darwin" else "bananaflow/bananaflow-updater.exe")
    helper = case / Path(relative).name
    shutil.copy2(current_dist / relative, helper)
    target_exe = Path(request["target"]) / request["executable"]
    original_executable = digest(target_exe)
    before = fingerprint(Path(request["profile"]))
    old_process = subprocess.Popen([str(cli), "--version"], env=env)
    plan = {
        "schema": 1, "kind": request["kind"], "version": request["new_version"],
        "target": request["target"], "executable": request["executable"],
        "artifact": str(local), "size": request["size"], "sha256": "0" * 64,
        "parent_pid": old_process.pid, "transaction": uuid.uuid4().hex,
        "install_mode": "currentuser" if request["kind"] == "windows-installed" else "",
        "relaunch_args": ["--app-update-smoke-result", failure_request["result"]],
    }
    atomic_json(case / "plan.json", plan)
    updater = subprocess.Popen([str(helper), "--apply", str(case / "plan.json")], env=env)
    old_process.wait(timeout=120)  # reap before helper's parent-PID check
    if updater.wait(timeout=180) != 1:
        raise RuntimeError("A deliberately mismatched artifact did not fail closed")
    status = json.loads((case / "status.json").read_text(encoding="utf-8"))
    if status.get("status") != "error" or "sha-256" not in status.get("message", "").lower():
        raise RuntimeError(f"Expected an integrity failure, got {status}")
    result = wait_for_result(Path(failure_request["result"]), case / "status.json", expected_error=True)
    wait_for_process_exit(result["pid"])
    if (
        result["version"] != request["old_version"]
        or digest(target_exe) != original_executable
        or any(result[key] != before[key] for key in ("preferences", "sentinels"))
    ):
        raise RuntimeError("Rejected artifact changed the old application or user data")
    evidence.mkdir(parents=True, exist_ok=True)
    summary = {"ok": True, "helper_exit_code": 1, "status": status,
               "old_application_unchanged": True, "recovered_gui": result}
    write_json(evidence / "rejected-artifact.json", summary)
    # Remove the extra large artifact reference once the real helper is done.
    local.unlink()
    return summary


def published_handoff(request: dict, old_exe: Path, current_dist: Path, env: dict) -> tuple[subprocess.Popen, Path]:
    """Upgrade a real older public layout using the candidate's real helper."""
    from core.app_update_helper import atomic_json

    case = Path(request["result"]).parent
    work = case / "published-update"
    work.mkdir()
    artifact = Path(request["artifact"])
    local = work / artifact.name
    shutil.copy2(artifact, local)
    relative = ("BananaFlow.app/Contents/MacOS/bananaflow-updater"
                if sys.platform == "darwin" else "bananaflow/bananaflow-updater.exe")
    helper = work / Path(relative).name
    shutil.copy2(current_dist / relative, helper)
    # The published GUI was already exercised before taking the preservation
    # snapshot. The CLI is another actual process of that same old package.
    old_cli = old_exe.with_name("bananaflow-cli.exe" if sys.platform == "win32" else "bananaflow-cli")
    old_process = subprocess.Popen([str(old_cli), "--version"], env=env)
    plan = {
        "schema": 1, "kind": request["kind"], "version": request["new_version"],
        "target": request["target"], "executable": request["executable"],
        "artifact": str(local), "size": request["size"], "sha256": request["sha256"],
        "parent_pid": old_process.pid, "transaction": uuid.uuid4().hex,
        "install_mode": "currentuser" if request["kind"] == "windows-installed" else "",
        "relaunch_args": ["--app-update-smoke-result", request["result"]],
    }
    atomic_json(work / "plan.json", plan)
    subprocess.Popen([str(helper), "--apply", str(work / "plan.json")], env=env)
    return old_process, work / "status.json"


def exercise(root: Path, kind: str, args: argparse.Namespace, versions: dict) -> dict:
    case = root / kind
    case.mkdir()
    evidence = args.evidence_dir / kind
    evidence.mkdir(parents=True, exist_ok=True)
    old = (fetch_baseline(case, kind) if args.published_baseline
           else find_artifact(args.old_dist, kind))
    new = find_artifact(args.new_dist, kind)
    target = case / ("BananaFlow.app" if kind == "macos-app" else "app")
    profile, env = seed_profile(case)
    install(old, target, kind, evidence)
    executable = "Contents/MacOS/bananaflow" if kind == "macos-app" else "bananaflow.exe"
    cli = target / ("Contents/MacOS/bananaflow-cli" if kind == "macos-app" else "bananaflow-cli.exe")
    old_version = BASELINE_VERSION if args.published_baseline else versions["old"]
    old_cli = run([str(cli), "--version"], env=env).strip()
    if old_version not in old_cli:
        raise RuntimeError(f"Installed old CLI reports wrong version: {old_cli}")
    # Test obsolete tree removal, not just overwriting an unchanged file.
    obsolete = target / ("Contents/Resources/updater-obsolete.txt" if kind == "macos-app"
                         else "_internal/yt_dlp-obsolete.dist-info/updater-obsolete.txt")
    obsolete.parent.mkdir(parents=True, exist_ok=True)
    obsolete.write_text("old-package-only", encoding="utf-8")
    if args.published_baseline:
        # Let the old app initialize/migrate its own data before snapshotting
        # it, so real history schema initialization is not mistaken for damage
        # by the updater.
        env["BANANAFLOW_SMOKE_RESULT_FILE"] = str(case / "old-gui.json")
        old_gui_process = subprocess.Popen(
            [str(target / executable), "--internal-smoke-test", "tag-editor"], env=env)
        old_gui_code = old_gui_process.wait(timeout=180)
        old_gui = json.loads((case / "old-gui.json").read_text(encoding="utf-8"))
        if not old_gui.get("ok") or any(not step["ok"] for step in old_gui["steps"]):
            raise RuntimeError("Published baseline GUI smoke did not pass")
        if old_gui_code != 0:
            raise RuntimeError(f"Published baseline GUI exited unexpectedly: {old_gui_code}")
    before = fingerprint(profile)
    request = {
        "root": str(root), "profile": str(profile), "home": env["HOME"],
        "appdata": env["APPDATA"], "kind": kind, "target": str(target),
        "executable": executable, "old_version": old_version,
        "new_version": versions["new"], "artifact": str(new),
        "size": new.stat().st_size, "sha256": digest(new),
        "result": str(case / "relaunch.json"),
    }
    write_json(case / "request.json", request)
    env["BANANAFLOW_UPDATE_SMOKE_REQUEST"] = str(case / "request.json")
    failure_evidence = None
    if not args.published_baseline:
        failure_evidence = exercise_rejected_artifact(request, args.new_dist, env, cli, evidence)
    if args.published_baseline:
        process, status = published_handoff(request, target / executable, args.new_dist, env)
    else:
        process = subprocess.Popen([str(target / executable), "--internal-smoke-test", "app-update"], env=env)
        old_code = process.wait(timeout=120)
        before = json.loads((case / "before.json").read_text(encoding="utf-8"))
        if old_code != 0:
            raise RuntimeError(f"The old packaged application's updater handoff failed: {old_code}")
        handoff = json.loads((case / "handoff.json").read_text(encoding="utf-8"))
        if handoff["parent_pid"] != process.pid:
            raise RuntimeError("The updater did not wait for the actual old application")
        status = Path(handoff["status_path"])
    completed = False
    try:
        # Reap before waiting for the helper: on POSIX an unreaped child's PID
        # remains observable, so the helper correctly keeps waiting for it.
        code = process.wait(timeout=120)
        if code != 0:
            raise RuntimeError(f"Old packaged process exited unexpectedly: {code}")
        result = wait_for_result(Path(request["result"]), status)
        wait_for_process_exit(result["pid"])
        if result["version"] != versions["new"] or Path(result["executable"]) != target / executable:
            raise RuntimeError("Relaunch used the wrong package, path, or version")
        if result["gui"].get("qt_platform") != smoke_qt_platform():
            raise RuntimeError("Relaunch did not use the required real GUI backend")
        if result["pid"] == process.pid or result["profile"] != str(profile):
            raise RuntimeError("The app did not restart into the preserved user profile")
        for key in ("preferences", "sentinels"):
            if result[key] != before[key]:
                raise RuntimeError(f"Update changed preserved user {key}")
        if obsolete.exists():
            raise RuntimeError("Obsolete packaged downloader files survived the update")
        if (case / "downloads/do-not-touch.mp3").read_bytes() != b"existing-user-download":
            raise RuntimeError("Update changed a user download")
        native_exit = verify_native_gui_exit(request, env, evidence)
        new_cli = run([str(cli), "--version"], env=env).strip()
        if versions["new"] not in new_cli:
            raise RuntimeError(f"Replaced CLI reports wrong version: {new_cli}")
        summary = {
            "ok": True, "kind": kind, "baseline": "published" if args.published_baseline else "synthetic",
            "old_version": old_version, "new_version": versions["new"],
            "old_sha256": digest(old), "new_sha256": request["sha256"],
            "old_process_pid": process.pid, "new_process_pid": result["pid"],
            "old_process_exit": code,
            "old_cli": old_cli, "new_cli": new_cli, "relaunch": result,
            "replacement_native_exit": native_exit,
            "preserved_history_backups_paused_work_and_downloads": True,
            "obsolete_package_files_removed": True,
            "integrity_failure_recovery": failure_evidence,
        }
        write_json(evidence / "summary.json", summary)
        completed = True
        return summary
    finally:
        # Keep actionable status and smoke results even on a failure. Never
        # upload the full runner profile or downloaded executable artifacts.
        for file in (case / "before.json", case / "handoff.json", case / "relaunch.json",
                     case / "old-gui.json", status):
            if file.is_file():
                shutil.copy2(file, evidence / file.name)
        # A failed helper may still be recovering. Do not uninstall its target
        # out from under it; the whole failed fixture survives until teardown.
        if completed and kind == "windows-installed" and (target / "unins000.exe").is_file():
            run([str(target / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-dist", type=Path)
    parser.add_argument("--new-dist", required=True, type=Path)
    parser.add_argument("--versions", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--published-baseline", action="store_true")
    args = parser.parse_args()
    if sys.platform not in ("win32", "darwin"):
        parser.error("Real packaged update evidence requires Windows/macOS")
    if not args.published_baseline and args.old_dist is None:
        parser.error("--old-dist is required for synthetic package updates")
    args.new_dist = args.new_dist.resolve()
    args.old_dist = args.old_dist.resolve() if args.old_dist else None
    args.evidence_dir = args.evidence_dir.resolve()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    versions = json.loads(args.versions.read_text(encoding="utf-8"))
    if args.published_baseline:
        from core.update_checker import _is_newer
        if not _is_newer(versions["new"], BASELINE_VERSION):
            raise RuntimeError("Published migration target must be newer than the public baseline")
    with disposable_sandbox() as root:
        (root / MARKER).write_text("disposable packaged updater test", encoding="utf-8")
        kinds = ("windows-portable", "windows-installed") if sys.platform == "win32" else ("macos-app",)
        results = []
        for kind in kinds:
            results.append(exercise(root, kind, args, versions))
            # Complete evidence is external to the sandbox. Free each old,
            # current and retained-backup package before the next large case.
            # Windows bootloader handles can briefly outlive the Python exit.
            for attempt in range(10):
                try:
                    shutil.rmtree(root / kind)
                    break
                except OSError:
                    if attempt == 9:
                        raise
                    time.sleep(1)
        write_json(args.evidence_dir / "summary.json", {"ok": True, "runs": results})


if __name__ == "__main__":
    main()
