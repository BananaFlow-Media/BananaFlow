"""Offline tests of the real-platform runner's safety and evidence contract."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from core import app_update_smoke
from scripts import build_update_test_packages, run_packaged_update_smoke


ROOT = Path(__file__).resolve().parent.parent


def marked_request(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "bf-upd-fixture"
    case = root / "windows-portable"
    case.mkdir(parents=True)
    (root / app_update_smoke.MARKER).write_text("test", encoding="utf-8")
    request = {
        "root": str(root), "profile": str(case / "profile"),
        "home": str(case / "home"), "appdata": str(case / "appdata"),
        "result": str(case / "relaunch.json"),
    }
    path = case / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return path, request


def test_smoke_requires_marked_sandbox(tmp_path):
    path, request = marked_request(tmp_path)
    assert app_update_smoke.fixture_request(path) == request
    (Path(request["root"]) / app_update_smoke.MARKER).unlink()
    with pytest.raises(ValueError, match="marked disposable"):
        app_update_smoke.fixture_request(path)


@pytest.mark.parametrize("key", ["home", "appdata", "profile", "result"])
def test_smoke_rejects_profile_and_result_escape(tmp_path, key):
    path, request = marked_request(tmp_path)
    request[key] = str(tmp_path / "outside")
    path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        app_update_smoke.fixture_request(path)


@pytest.mark.parametrize("platform,qt_platform", [("darwin", "cocoa"), ("win32", "offscreen"), ("linux", "offscreen")])
def test_smoke_restores_launchservices_environment_only_after_validation(tmp_path, monkeypatch, platform, qt_platform):
    monkeypatch.setattr(app_update_smoke.sys, "platform", platform)
    path, request = marked_request(tmp_path)
    for name in ("HOME", "USERPROFILE", "APPDATA", "QT_QPA_PLATFORM"):
        monkeypatch.setenv(name, "untouched")
    with pytest.raises(ValueError, match="Unexpected smoke result"):
        app_update_smoke.configure_smoke_environment(path.parent / "wrong.json")
    assert os.environ["HOME"] == "untouched"
    app_update_smoke.configure_smoke_environment(Path(request["result"]))
    assert os.environ["HOME"] == request["home"]
    assert os.environ["APPDATA"] == request["appdata"]
    assert os.environ["QT_QPA_PLATFORM"] == qt_platform


def test_build_failure_restores_canonical_version(tmp_path, monkeypatch):
    version = tmp_path / "version.py"
    original = b'FULL_VERSION = "1.2.0"\n'
    version.write_bytes(original)
    monkeypatch.setattr(build_update_test_packages, "ROOT", tmp_path)

    def fail():
        assert 'FULL_VERSION = __version__' in version.read_text(encoding="utf-8")
        raise RuntimeError("fixture build failure")

    monkeypatch.setattr(build_update_test_packages, "build", fail)
    with pytest.raises(RuntimeError, match="fixture build failure"):
        build_update_test_packages.main()
    assert version.read_bytes() == original


def test_fixture_versions_are_consistent_and_target_is_newer():
    original = (ROOT / "version.py").read_bytes()
    namespace = {}
    exec(build_update_test_packages.fixture_source(original, "1.2.1"), namespace)
    assert namespace["FULL_VERSION"] == "1.2.1"
    assert namespace["PEP440_VERSION"] == "1.2.1"
    assert namespace["VERSION_INFO"] == (1, 2, 1)
    assert namespace["PRERELEASE"] is None


def test_baseline_download_enforces_pinned_bytes(tmp_path, monkeypatch):
    content = b"known release"
    path = tmp_path / "known"
    path.write_bytes(content)
    monkeypatch.setitem(run_packaged_update_smoke.BASELINE, "test",
                        ("fixture.zip", len(content), run_packaged_update_smoke.digest(path)))
    monkeypatch.setattr(run_packaged_update_smoke.urllib.request, "urlopen",
                        lambda *a, **kw: io.BytesIO(content))
    result = run_packaged_update_smoke.fetch_baseline(tmp_path, "test")
    assert result.read_bytes() == content
    monkeypatch.setattr(run_packaged_update_smoke.urllib.request, "urlopen",
                        lambda *a, **kw: io.BytesIO(b"wrong release"))
    with pytest.raises(RuntimeError, match="checksum/size"):
        run_packaged_update_smoke.fetch_baseline(tmp_path, "test")


def test_baseline_download_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setitem(run_packaged_update_smoke.BASELINE, "test", ("fixture.zip", 3, "0" * 64))
    monkeypatch.setattr(run_packaged_update_smoke.urllib.request, "urlopen",
                        lambda *a, **kw: io.BytesIO(b"oversize"))
    with pytest.raises(RuntimeError, match="exceeds"):
        run_packaged_update_smoke.fetch_baseline(tmp_path, "test")


def test_artifact_selection_rejects_ambiguity(tmp_path):
    for version in ("1.0.0", "2.0.0"):
        (tmp_path / f"BananaFlow-v{version}-windows-x64-portable.zip").touch()
    with pytest.raises(RuntimeError, match="exactly one"):
        run_packaged_update_smoke.find_artifact(tmp_path, "windows-portable")


def test_result_wait_surfaces_helper_failure(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"status": "error", "message": "refused"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Updater failed"):
        run_packaged_update_smoke.wait_for_result(tmp_path / "absent.json", status)


def test_packaged_update_requires_clean_exit_including_gui_teardown():
    source = (ROOT / "scripts/run_packaged_update_smoke.py").read_text(encoding="utf-8")
    assert "if old_gui_code != 0:" in source
    assert "if old_code != 0:" in source
    assert "if code != 0:" in source
    for forbidden in ("-1073740791", "3221226505", "accepted_old_gui_exit",
                      "reviewed_windows_qt_teardown"):
        assert forbidden not in source

def test_pr_workflow_is_read_only_and_covers_both_platforms():
    yaml = pytest.importorskip("yaml")
    text = (ROOT / ".github/workflows/packaged-updater.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["update"]["strategy"]["matrix"]["os"] == ["windows-latest", "macos-15"]
    for forbidden in ("pull_request_target", "secrets.", "id-token:", "attestations:", "contents: write",
                      "action-gh-release", "continue-on-error"):
        assert forbidden not in text
    release = yaml.safe_load((ROOT / ".github/workflows/release-macos.yml").read_text(encoding="utf-8"))
    assert release["jobs"]["build"]["runs-on"] == "macos-15"
    assert "persist-credentials: false" in text
    assert "--published-baseline" in text
    assert "run_packaged_update_smoke.py" in text
    assert "if: always()" in text


def test_helper_is_built_independently_in_each_release_path():
    helper = (ROOT / "scripts/build_app_updater.py").read_text(encoding="utf-8")
    assert '"--onefile"' in helper
    assert '"app_update_helper.py"' in helper
    for name in ("scripts/build_windows.ps1", "scripts/build_macos.sh",
                 ".github/workflows/release-macos.yml"):
        assert "build_app_updater.py" in (ROOT / name).read_text(encoding="utf-8")


def test_smoke_has_real_preservation_and_process_evidence():
    text = (ROOT / "scripts/run_packaged_update_smoke.py").read_text(encoding="utf-8")
    for requirement in ("old_process_pid", "new_process_pid", "preserved_history_backups",
                        "obsolete_package_files_removed", '("preferences", "sentinels")',
                        'result["pid"] == process.pid'):
        assert requirement in text
    assert "exercise_rejected_artifact" in text
    assert '"old_application_unchanged": True' in text
    assert text.index("code = process.wait(timeout=120)") < text.index(
        'result = wait_for_result(Path(request["result"]), status)')
    hook = (ROOT / "core/app_update_smoke.py").read_text(encoding="utf-8")
    assert "run_gui_update_smoke(prepared)" in hook
    assert 'result["gui"] = run_gui_update_smoke()' in hook
    assert 'and result["gui"].get("ok", False)' in hook


def test_replacement_gui_native_crash_is_not_accepted(tmp_path, monkeypatch):
    import subprocess

    request = {"result": str(tmp_path / "relaunch.json"),
               "target": str(tmp_path / "app"), "executable": "bananaflow.exe"}
    def crash(*args, **kwargs):
        raise subprocess.CalledProcessError(3221226505, args[0])
    monkeypatch.setattr(run_packaged_update_smoke, "run", crash)
    with pytest.raises(subprocess.CalledProcessError):
        run_packaged_update_smoke.verify_native_gui_exit(request, {}, tmp_path)


def test_replacement_gui_native_exit_records_separate_supervised_evidence(tmp_path, monkeypatch):
    request = {"result": str(tmp_path / "relaunch.json"),
               "target": str(tmp_path / "app"), "executable": "bananaflow.exe"}
    def clean_exit(args, **kwargs):
        assert args[1] == "--app-update-smoke-result"
        Path(args[2]).write_text(json.dumps({"ok": True, "gui": {"shutdown_accepted": True, "qt_platform": app_update_smoke.smoke_qt_platform()}}))
        return ""
    monkeypatch.setattr(run_packaged_update_smoke, "run", clean_exit)
    result = run_packaged_update_smoke.verify_native_gui_exit(request, {}, tmp_path)
    assert result["supervised_process_exit_code"] == 0
    assert (tmp_path / "replacement-native-exit.json").is_file()


def test_native_command_failure_keeps_diagnostics_and_fails(monkeypatch, capsys):
    import subprocess

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="tool output", stderr="native failure")
    monkeypatch.setattr(run_packaged_update_smoke.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        run_packaged_update_smoke.run(["fixture-tool"])
    assert "native failure" in capsys.readouterr().err
