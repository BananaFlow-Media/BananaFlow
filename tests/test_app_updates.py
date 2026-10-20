"""Deterministic trust, preparation, platform and helper failure contracts."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import zipfile

import httpx
import pytest

from core import app_updates as updates
from core import app_update_helper as helper
from core.update_checker import ReleaseInfo, UpdateChecker, _is_newer


def _zip(files=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in (files or {
            "bananaflow.exe": b"new application",
            "_internal/runtime.dll": b"new runtime",
        }).items():
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.fixture
def support(tmp_path, monkeypatch):
    target = tmp_path / "Portable app & spaces"
    target.mkdir()
    (target / "bananaflow.exe").write_bytes(b"old application")
    (target / "bananaflow-updater.exe").write_bytes(b"helper fixture")
    info = updates.AppUpdateSupport(
        True, "", "windows-portable", target, "bananaflow.exe",
        target / "bananaflow-updater.exe", "x64", "",
    )
    monkeypatch.setattr(updates, "app_update_support", lambda: info)
    monkeypatch.setattr(updates, "_update_root", lambda: tmp_path / "appdata" / "app-updates")
    monkeypatch.setattr(updates, "_quarantine", lambda _: None)
    return info


def _release(support, payload, **overrides):
    data = {
        "tag_name": "v99.0.0", "draft": False,
        "assets": [{
            "name": updates._asset_name("99.0.0", support),
            "url": updates.ASSET_API_PREFIX + "123",
            "state": "uploaded", "size": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "browser_download_url": "https://untrusted.invalid/not-used",
        }],
    }
    data.update(overrides)
    return data


def _transport(monkeypatch, metadata, payload, *, handler=None):
    requests = []
    def respond(request):
        requests.append(request)
        if handler:
            answer = handler(request)
            if answer is not None:
                return answer
        if "/releases/tags/" in request.url.path:
            return httpx.Response(200, json=metadata)
        return httpx.Response(200, content=payload)
    original = httpx.Client
    monkeypatch.setattr(
        updates.httpx, "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return requests


def _info(version="99.0.0"):
    return ReleaseInfo(version, "", "", "", asset_url="https://evil.invalid/ignored")


def test_exact_platform_asset_not_first_release_asset(support):
    payload = _zip()
    release = _release(support, payload)
    release["assets"].insert(0, {"name": "SHA256SUMS.txt"})
    assert updates.select_release_asset(release, "99.0.0", support).name.endswith("-portable.zip")
    assert updates._asset_name("99.0.0", replace(support, kind="windows-installed")).endswith("-setup.exe")
    assert updates._asset_name("99.0.0", replace(support, kind="macos-app")).endswith("-macos-arm64.dmg")


@pytest.mark.parametrize("field,value", [
    ("digest", None), ("digest", "sha512:" + "a" * 64),
    ("digest", "sha256:" + "g" * 64),
    ("size", -1), ("size", True), ("size", "100"),
    ("size", helper.MAX_ARTIFACT_BYTES + 1), ("state", "new"),
    ("url", "http://api.github.com/repos/BananaFlow-Media/BananaFlow/releases/assets/123"),
    ("url", updates.ASSET_API_PREFIX + "123/../evil"),
    ("url", updates.ASSET_API_PREFIX + "123?redirect=https://evil.invalid"),
    ("url", "https://api.github.com/repos/other/BananaFlow/releases/assets/123"),
])
def test_asset_metadata_fails_closed(support, field, value):
    release = _release(support, b"asset")
    release["assets"][0][field] = value
    with pytest.raises(helper.AppUpdateError):
        updates.select_release_asset(release, "99.0.0", support)


@pytest.mark.parametrize("changes", [
    {"tag_name": "v98.0.0"}, {"draft": True}, {"draft": None}, {"assets": None},
])
def test_release_identity_fails_closed(support, changes):
    with pytest.raises(helper.AppUpdateError):
        updates.select_release_asset(_release(support, b"asset", **changes), "99.0.0", support)


def test_duplicate_named_assets_rejected(support):
    release = _release(support, b"asset")
    release["assets"].append(dict(release["assets"][0]))
    with pytest.raises(helper.AppUpdateError):
        updates.select_release_asset(release, "99.0.0", support)


def test_prepare_requeries_official_tag_and_persists_verified_download(support, monkeypatch):
    payload = _zip()
    requests = _transport(monkeypatch, _release(support, payload), payload)
    progress = []
    prepared = updates.prepare_app_update(_info(), progress=lambda a, b: progress.append((a, b)))
    assert str(requests[0].url) == updates.API_ROOT + "/releases/tags/v99.0.0"
    assert str(requests[1].url) == updates.ASSET_API_PREFIX + "123"
    assert prepared.artifact_path.read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert (support.target / "bananaflow.exe").read_bytes() == b"old application"
    assert updates.get_prepared_app_update() == prepared
    assert prepared.helper_path.read_bytes() == b"helper fixture"
    assert not (prepared.state_path.parent / "plan.json").exists()


@pytest.mark.parametrize("payload", [b"short", b"bad" * 1000])
def test_failed_download_keeps_existing_prepared_pointer(support, monkeypatch, payload):
    good = _zip()
    _transport(monkeypatch, _release(support, good), good)
    prepared = updates.prepare_app_update(_info())
    # Replace the transport without wrapping the monkeypatched constructor.
    monkeypatch.undo()
    monkeypatch.setattr(updates, "app_update_support", lambda: support)
    monkeypatch.setattr(updates, "_update_root", lambda: prepared.state_path.parent.parent)
    monkeypatch.setattr(updates, "_quarantine", lambda _: None)
    _transport(monkeypatch, _release(support, good), payload)
    with pytest.raises(helper.AppUpdateError):
        updates.prepare_app_update(_info())
    assert updates.get_prepared_app_update() == prepared
    assert len(list(prepared.state_path.parent.parent.iterdir())) == 2


def test_cancelled_download_never_selects_update(support, monkeypatch):
    payload = _zip()
    _transport(monkeypatch, _release(support, payload), payload)
    with pytest.raises(helper.AppUpdateError, match="cancelled"):
        updates.prepare_app_update(_info(), cancelled=lambda: True)
    assert updates.get_prepared_app_update() is None


def test_cached_package_tampering_fails_revalidation(support, monkeypatch):
    payload = _zip()
    _transport(monkeypatch, _release(support, payload), payload)
    prepared = updates.prepare_app_update(_info())
    changed = bytearray(payload)
    changed[10] ^= 1
    prepared.artifact_path.write_bytes(changed)
    assert updates.get_prepared_app_update() is None


@pytest.mark.parametrize("destination", [
    "http://release-assets.githubusercontent.com/asset",
    "https://evil.invalid/asset",
    "https://api.github.com.evil.invalid/asset",
    "https://user:password@api.github.com/asset",
    "https://api.github.com:8443/asset",
])
def test_untrusted_redirect_never_contacted(support, monkeypatch, destination):
    payload = _zip()
    requests = _transport(
        monkeypatch, _release(support, payload), payload,
        handler=lambda request: httpx.Response(302, headers={"location": destination})
        if "/releases/assets/" in request.url.path else None,
    )
    with pytest.raises(helper.AppUpdateError):
        updates.prepare_app_update(_info())
    assert len(requests) == 2


def test_github_asset_redirect_verified(support, monkeypatch):
    payload = _zip()
    requests = _transport(
        monkeypatch, _release(support, payload), payload,
        handler=lambda request: httpx.Response(302, headers={
            "location": "https://release-assets.githubusercontent.com/asset?token=fixture"
        }) if "/releases/assets/" in request.url.path else None,
    )
    assert updates.prepare_app_update(_info()).artifact_path.read_bytes() == payload
    assert len(requests) == 3


def test_source_mode_has_no_self_update(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert updates.app_update_support().reason == "source"


def test_frozen_linux_has_no_self_update(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert updates.app_update_support().reason == "platform"


def test_platform_support_preserves_installed_scope(tmp_path, monkeypatch):
    target = tmp_path / "Installed"
    target.mkdir()
    executable = target / "bananaflow.exe"
    executable.write_bytes(b"exe")
    (target / "bananaflow-updater.exe").write_bytes(b"helper")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(updates.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(updates, "_windows_install_mode", lambda _: "allusers")
    info = updates.app_update_support()
    assert info.supported and info.kind == "windows-installed" and info.install_mode == "allusers"
    monkeypatch.setattr(updates, "_windows_install_mode", lambda _: "")
    (target / "unins000.exe").write_bytes(b"unknown installer")
    assert updates.app_update_support().reason == "unknown-installation"


def test_checker_preserves_assets_for_discovery():
    checker = UpdateChecker()
    release = {
        "tag_name": "v99.0.0", "draft": False,
        "assets": [{"name": "fixture", "url": "https://api.github.com/fixture"}],
    }
    info = checker._release_to_info(release, False)
    assert info.tag_name == "v99.0.0"
    assert info.assets == tuple(release["assets"])


@pytest.mark.parametrize("name", [
    "../escape", "/absolute", "C:/escape", "nested\\escape", "a/../escape",
    "CON", "aux.txt", "folder/NUL.log", "file:stream", "trailing.", "trailing ",
])
def test_portable_archive_paths_cannot_escape(tmp_path, name):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(_zip({
        "bananaflow.exe": b"new", "_internal/a": b"runtime", name: b"bad",
    }))
    destination = tmp_path / "staged"
    destination.mkdir()
    with pytest.raises(helper.AppUpdateError):
        helper.extract_portable(archive, destination)
    assert list(destination.iterdir()) == []


def test_portable_symlinks_and_case_collisions_rejected(tmp_path):
    archive = tmp_path / "asset.zip"
    for symlink in (True, False):
        with zipfile.ZipFile(archive, "w") as out:
            out.writestr("bananaflow.exe", b"new")
            out.writestr("_internal/runtime", b"runtime")
            if symlink:
                entry = zipfile.ZipInfo("link")
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                out.writestr(entry, "../../escape")
            else:
                out.writestr("BANANAFLOW.EXE", b"duplicate")
        with pytest.raises(helper.AppUpdateError):
            helper.extract_portable(archive, tmp_path / "output")


def test_archive_expansion_is_bounded(tmp_path, monkeypatch):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(_zip())
    monkeypatch.setattr(helper, "MAX_EXPANDED_BYTES", 1)
    with pytest.raises(helper.AppUpdateError, match="safe limit"):
        helper.extract_portable(archive, tmp_path / "output")


def _plan(tmp_path, *, kind="windows-portable"):
    work = tmp_path / "appdata" / "app-updates" / ("a" * 32)
    work.mkdir(parents=True)
    target = tmp_path / "Application with spaces"
    target.mkdir()
    (target / "bananaflow.exe").write_bytes(b"old")
    (target / "old-only.txt").write_bytes(b"retained in backup")
    artifact = work / "asset.zip"
    payload = _zip()
    artifact.write_bytes(payload)
    plan = {
        "schema": 1, "version": "99.0.0", "kind": kind,
        "target": str(target), "artifact": str(artifact),
        "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        "executable": "bananaflow.exe", "parent_pid": 123456,
        "transaction": "b" * 32, "relaunch_args": [], "install_mode": "currentuser",
    }
    plan_path = work / "plan.json"
    helper.atomic_json(plan_path, plan)
    return plan_path, plan


@pytest.fixture
def helper_runtime(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(helper, "wait_for_parent", lambda *args: None)
    monkeypatch.setattr(helper, "ensure_no_other_app_processes", lambda: None)
    calls = []
    def relaunch(plan):
        assert not helper.update_lock_is_held(Path(plan["artifact"]).parent.parent / "apply.lock")
        calls.append(plan)
    monkeypatch.setattr(helper, "_relaunch", relaunch)
    return calls


def test_real_portable_apply_swaps_tree_and_retains_recovery(tmp_path, helper_runtime):
    path, plan = _plan(tmp_path)
    assert helper.apply_update_plan(path) == 0
    target, staging, backup = helper._paths(plan)
    assert (target / "bananaflow.exe").read_bytes() == b"new application"
    assert (backup / "bananaflow.exe").read_bytes() == b"old"
    assert (backup / "old-only.txt").exists()
    assert not staging.exists()
    assert helper_runtime[-1]["target"] == str(target)
    assert helper.read_json(path.parent / "status.json")["status"] == "launched"
    assert not helper.update_lock_is_held(path.parent.parent / "apply.lock")


def test_corruption_never_replaces_original(tmp_path, helper_runtime):
    path, plan = _plan(tmp_path)
    Path(plan["artifact"]).write_bytes(b"corruption")
    assert helper.apply_update_plan(path) == 1
    assert (Path(plan["target"]) / "bananaflow.exe").read_bytes() == b"old"
    assert helper.read_json(path.parent / "status.json")["status"] == "error"
    assert len(helper_runtime) == 1  # Reopen original after parent exited.


def test_parent_timeout_never_touches_install_or_relaunches(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path)
    def timeout(*_):
        raise helper.AppUpdateError("parent still active")
    monkeypatch.setattr(helper, "wait_for_parent", timeout)
    assert helper.apply_update_plan(path) == 1
    assert (Path(plan["target"]) / "bananaflow.exe").read_bytes() == b"old"
    assert not helper_runtime


def test_swap_failure_restores_original(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path)
    target, staging, backup = helper._paths(plan)
    original = os.replace
    def replace_fail(source, destination):
        if Path(source) == staging and Path(destination) == target:
            raise OSError("fixture replacement failure")
        return original(source, destination)
    monkeypatch.setattr(helper.os, "replace", replace_fail)
    assert helper.apply_update_plan(path) == 1
    assert (target / "bananaflow.exe").read_bytes() == b"old"
    assert not backup.exists()


def test_relaunch_failure_rolls_back_portable(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path)
    def no_launch(_):
        raise OSError("fixture launch declined")
    monkeypatch.setattr(helper, "_relaunch", no_launch)
    assert helper.apply_update_plan(path) == 1
    target, _, backup = helper._paths(plan)
    assert (target / "bananaflow.exe").read_bytes() == b"old"
    assert not backup.exists()


def test_relaunch_failure_cannot_rollback_under_newly_opened_app(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path)
    def busy():
        raise helper.AppUpdateError("new app was opened independently")
    def failed_launch(_):
        monkeypatch.setattr(helper, "ensure_no_other_app_processes", busy)
        raise OSError("OS launch rejected")
    monkeypatch.setattr(helper, "_relaunch", failed_launch)
    assert helper.apply_update_plan(path) == 1
    target, _, backup = helper._paths(plan)
    assert (target / "bananaflow.exe").read_bytes() == b"new application"
    assert (backup / "bananaflow.exe").read_bytes() == b"old"
    assert not helper.update_lock_is_held(path.parent.parent / "apply.lock")


def test_interrupted_first_rename_recovers_before_retry(tmp_path, helper_runtime):
    path, plan = _plan(tmp_path)
    target, _, backup = helper._paths(plan)
    os.replace(target, backup)
    assert helper.apply_update_plan(path) == 0
    assert (target / "bananaflow.exe").read_bytes() == b"new application"
    assert (backup / "bananaflow.exe").read_bytes() == b"old"


@pytest.mark.parametrize("missing", [True, False])
def test_interrupted_swap_restores_old_app_even_when_package_lost(tmp_path, helper_runtime, missing):
    path, plan = _plan(tmp_path)
    target, _, backup = helper._paths(plan)
    os.replace(target, backup)
    if missing:
        Path(plan["artifact"]).unlink()
    else:
        Path(plan["artifact"]).write_bytes(b"corrupt")
    assert helper.apply_update_plan(path) == 1
    assert (target / "bananaflow.exe").read_bytes() == b"old"
    assert not backup.exists()
    assert helper_runtime[-1]["target"] == str(target)
    assert helper.read_json(path.parent / "status.json")["status"] == "error"


def test_concurrent_helper_rejected_without_touching_target(tmp_path, helper_runtime):
    path, plan = _plan(tmp_path)
    with helper._acquire_lock(path.parent.parent / "apply.lock", "c" * 32):
        assert helper.apply_update_plan(path) == 1
        assert (Path(plan["target"]) / "bananaflow.exe").read_bytes() == b"old"
        assert not helper_runtime
    assert helper.read_json(path.parent.parent / "apply.lock")["pid"] == os.getpid()


@pytest.mark.parametrize("field,value", [
    ("executable", "../other.exe"), ("transaction", "../escape"),
    ("parent_pid", 0), ("parent_pid", True), ("relaunch_args", ["--execute", "bad"]),
    ("target", "/"), ("kind", "other"), ("install_mode", "untrusted"),
])
def test_invalid_plan_rejected(tmp_path, field, value):
    path, plan = _plan(tmp_path, kind="windows-installed")
    plan[field] = value
    helper.atomic_json(path, plan)
    with pytest.raises(helper.AppUpdateError):
        helper.validate_plan(path)


def test_installed_failure_does_not_claim_success_or_launch(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path, kind="windows-installed")
    def failed(_):
        raise helper.AppUpdateError("installer cancelled")
    monkeypatch.setattr(helper, "install_windows", failed)
    assert helper.apply_update_plan(path) == 1
    assert helper.read_json(path.parent / "status.json")["status"] == "error"
    assert not helper_runtime


def test_macos_bundle_identity_and_symlink_boundary(tmp_path):
    app = tmp_path / "BananaFlow.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "bananaflow").write_bytes(b"exe")
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({
            "CFBundleIdentifier": "media.bananaflow.app",
            "CFBundleShortVersionString": "99.0.0",
        }, handle)
    helper._check_macos_tree(app, "99.0.0")
    with pytest.raises(helper.AppUpdateError):
        helper._check_macos_tree(app, "98.0.0")
    (app / "escape").symlink_to(tmp_path)
    with pytest.raises(helper.AppUpdateError, match="escaping"):
        helper._check_macos_tree(app, "99.0.0")


def test_launch_uses_ready_handshake_reset_environment_and_no_shell(support, monkeypatch):
    payload = _zip()
    _transport(monkeypatch, _release(support, payload), payload)
    prepared = updates.prepare_app_update(_info())
    calls = []
    class Process:
        def poll(self):
            return None
    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        plan = helper.read_json(Path(command[2]))
        helper.atomic_json(Path(command[2]).parent / "status.json", {
            "status": "waiting", "transaction": plan["transaction"],
        })
        return Process()
    monkeypatch.setattr(updates.subprocess, "Popen", spawn)
    process = updates.launch_prepared_app_update(prepared)
    assert process.poll() is None
    command, kwargs = calls[0]
    assert command[:2] == [str(prepared.helper_path), "--apply"]
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert not kwargs.get("shell", False)
    assert helper.read_json(Path(command[2]))["relaunch_args"] == []


def test_launch_failure_is_not_success(support, monkeypatch):
    payload = _zip()
    _transport(monkeypatch, _release(support, payload), payload)
    prepared = updates.prepare_app_update(_info())
    class DeadProcess:
        def poll(self):
            return 1
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: DeadProcess())
    with pytest.raises(helper.AppUpdateError, match="exited"):
        updates.launch_prepared_app_update(prepared)
    assert updates.get_prepared_app_update() == prepared


@pytest.mark.parametrize("new,old,expected", [
    ("1.2.0", "1.2.0-beta.1", True), ("1.2.0-beta.1", "1.2.0", False),
    ("1.2.0-rc.10", "1.2.0-rc.9", True), ("1.2.0-beta.2", "1.2.0-beta.11", False),
    ("1.2.0+build.2", "1.2.0+build.1", False), ("1.10.0", "1.9.9", True),
    ("release-99.0.0", "1.2.0", False), ("1.2", "1.2.0", False),
])
def test_app_version_comparison_handles_stable_and_prerelease(new, old, expected):
    assert _is_newer(new, old) is expected


def test_another_application_instance_defers_without_relaunch(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path)
    def busy():
        raise helper.AppUpdateError("another application is active")
    monkeypatch.setattr(helper, "ensure_no_other_app_processes", busy)
    ticks = iter((0, 11))
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    assert helper.apply_update_plan(path) == 1
    assert not helper_runtime
    assert (Path(plan["target"]) / "bananaflow.exe").read_bytes() == b"old"


def test_instance_opened_during_extraction_prevents_swap(tmp_path, helper_runtime, monkeypatch):
    path, plan = _plan(tmp_path)
    checks = []
    def check():
        checks.append(1)
        if len(checks) > 1:
            raise helper.AppUpdateError("another window opened during extraction")
    monkeypatch.setattr(helper, "ensure_no_other_app_processes", check)
    assert helper.apply_update_plan(path) == 1
    target, _, backup = helper._paths(plan)
    assert (target / "bananaflow.exe").read_bytes() == b"old"
    assert not backup.exists() and not helper_runtime


def test_mac_process_check_fails_closed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"123\n", b"")
    monkeypatch.setattr(helper.subprocess, "run", run)
    with pytest.raises(helper.AppUpdateError, match="Another"):
        helper.ensure_no_other_app_processes()
    assert commands[0] == ["/usr/bin/pgrep", "-x", "bananaflow|bananaflow-cli"]


def test_stale_lock_file_is_reused_without_unlink_race(tmp_path):
    lock = tmp_path / "apply.lock"
    helper.atomic_json(lock, {"pid": os.getpid(), "transaction": "old"})
    inode = lock.stat().st_ino
    with helper._acquire_lock(lock, "new"):
        assert helper.update_lock_is_held(lock)
    assert lock.stat().st_ino == inode
    assert not helper.update_lock_is_held(lock)
    assert helper.read_json(lock)["transaction"] == "new"


def test_broken_symlink_lock_is_not_treated_as_inactive(tmp_path):
    lock = tmp_path / "apply.lock"
    lock.symlink_to(tmp_path / "absent")
    with pytest.raises(helper.AppUpdateError, match="unsafe"):
        helper.update_lock_is_held(lock)


def test_launch_timeout_cancels_before_installation(support, monkeypatch):
    payload = _zip()
    _transport(monkeypatch, _release(support, payload), payload)
    prepared = updates.prepare_app_update(_info())
    class UnreadyProcess:
        def poll(self):
            return None
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: UnreadyProcess())
    ticks = iter((0, 31))
    monkeypatch.setattr(updates.time, "monotonic", lambda: next(ticks))
    with pytest.raises(helper.AppUpdateError, match="cancelled"):
        updates.launch_prepared_app_update(prepared)
    assert len(list(prepared.state_path.parent.glob("plan-*.cancel"))) == 1


def test_cancelled_plan_never_replaces_app(tmp_path, helper_runtime):
    path, plan = _plan(tmp_path)
    path.with_suffix(".cancel").touch()
    assert helper.apply_update_plan(path) == 1
    assert (Path(plan["target"]) / "bananaflow.exe").read_bytes() == b"old"
    assert not helper_runtime


def test_standalone_helper_console_flags_match_windows_contract(monkeypatch):
    class StartupInfo:
        dwFlags = 0
        wShowWindow = 1
    monkeypatch.setattr(helper, "_IS_WINDOWS", True)
    monkeypatch.setattr(helper.subprocess, "STARTUPINFO", StartupInfo, raising=False)
    monkeypatch.setattr(helper.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(helper.subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(helper.subprocess, "SW_HIDE", 0, raising=False)
    kwargs = helper._console_process_kwargs()
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["startupinfo"].wShowWindow == 0


def test_standalone_helper_does_not_change_macos_tool_launch_flags(monkeypatch):
    monkeypatch.setattr(helper, "_IS_WINDOWS", False)
    assert helper._console_process_kwargs() == {}
