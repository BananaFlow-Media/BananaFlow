import hashlib
from io import BytesIO
from pathlib import Path
import zipfile

import pytest

from core import app_update_helper, app_updates, runtime_components, tool_updates
from core.update_checker import ReleaseInfo
from utils import paths


def _api_asset(name, content):
    return {
        "name": name, "size": len(content), "state": "uploaded",
        "url": app_updates.ASSET_API_PREFIX + "123",
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
    }


def test_exact_maintenance_revision_is_prepared_then_disappears(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    helper = install / "bananaflow-updater.exe"
    helper.write_bytes(b"helper")
    support = app_updates.AppUpdateSupport(
        True, kind="windows-portable", target=install,
        executable="bananaflow.exe", helper_path=helper, architecture="x64",
    )
    version = app_updates.FULL_VERSION
    asset_name = app_updates._asset_name(version, support, 2)
    content = b"verified same-version package"
    release = {
        "tag_name": f"maintenance-v{version}", "draft": False, "prerelease": True,
        "assets": [_api_asset(asset_name, content)],
    }
    monkeypatch.setattr(app_updates, "app_update_support", lambda: support)
    monkeypatch.setattr(app_updates, "_update_root", lambda: tmp_path / "updates")
    monkeypatch.setattr(app_updates, "_fetch_tag_release", lambda client, tag, **kwargs: release)
    monkeypatch.setattr(app_updates, "_quarantine", lambda artifact: None)
    monkeypatch.setattr(app_updates, "_download", lambda client, asset, path, *args: path.write_bytes(content))
    outcome = app_updates.check_app_maintenance()
    assert outcome.update_available
    assert (outcome.release.version, outcome.release.maintenance_revision) == (version, 2)

    prepared = app_updates.prepare_app_update(outcome.release)
    assert prepared.artifact_path.name == asset_name
    assert (prepared.version, prepared.maintenance_revision) == (version, 2)
    assert app_updates.get_prepared_app_update() == prepared

    monkeypatch.setattr(app_updates, "MAINTENANCE_REVISION", 2)
    assert app_updates.check_app_maintenance().status == "up_to_date"
    assert app_updates.get_prepared_app_update() is None
    with pytest.raises(app_updates.AppUpdateError):
        app_updates.prepare_app_update(outcome.release)


@pytest.mark.parametrize("change", [{"draft": True}, {"prerelease": False}, {"tag_name": "maintenance-v0.0.1"}])
def test_maintenance_does_not_accept_wrong_channel(change, monkeypatch):
    version = app_updates.FULL_VERSION
    support = app_updates.AppUpdateSupport(True, kind="macos-app")
    release = {
        "tag_name": f"maintenance-v{version}", "draft": False, "prerelease": True,
        "assets": [_api_asset(app_updates._asset_name(version, support, 2), b"data")],
        **change,
    }
    monkeypatch.setattr(app_updates, "app_update_support", lambda: support)
    monkeypatch.setattr(app_updates, "_fetch_tag_release", lambda client, tag, **kwargs: release)
    assert app_updates.check_app_maintenance().status == "error"


def test_changed_maintenance_revision_is_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(app_updates, "app_update_support", lambda: app_updates.AppUpdateSupport(True))
    monkeypatch.setattr(app_updates, "_update_root", lambda: tmp_path / "updates")
    monkeypatch.setattr(app_updates, "_maintenance_asset", lambda client, support: None)
    with pytest.raises(app_updates.AppUpdateError, match="changed"):
        # The candidate withdrawn before the download is rejected.
        app_updates.prepare_app_update(ReleaseInfo(
            app_updates.FULL_VERSION, "", "", "", maintenance_revision=1,
        ))


def _zip_payload(tool, revision):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in tool_updates._executable_names(tool):
            archive.writestr(name, f"{name} revision {revision}")
    return stream.getvalue()


@pytest.fixture
def runtime_channel(monkeypatch, tmp_path):
    assets = {}
    offers = []
    monkeypatch.setattr(tool_updates, "_root", lambda: tmp_path / "runtime")
    monkeypatch.setattr(tool_updates, "_platform_key", lambda: "windows-x64")
    monkeypatch.setattr(tool_updates, "is_frozen", lambda: True)
    monkeypatch.setattr(tool_updates, "_active", {})
    monkeypatch.setattr(tool_updates, "_catalog", lambda client: offers)
    monkeypatch.setattr(tool_updates, "_quarantine", lambda path: None)
    monkeypatch.setattr(tool_updates, "_healthcheck", lambda *args: None)

    def download(client, asset, target, *args):
        target.write_bytes(assets[asset.name])
        # Run real artifact integrity verification before archive handling.
        tool_updates.verify_artifact(target, asset.size, asset.sha256)

    monkeypatch.setattr(tool_updates, "_download", download)

    def publish(tool, revision):
        name = f"bananaflow-{tool}-windows-x64-r{revision}.zip"
        content = _zip_payload(tool, revision)
        assets[name] = content
        asset = app_updates.select_verified_asset({"assets": [_api_asset(name, content)]}, name)
        offers.append(tool_updates.ToolOffer(tool, revision, asset))

    return publish, offers


def test_tools_activate_together_on_next_launch_and_restore_previous(runtime_channel, monkeypatch):
    publish, offers = runtime_channel
    publish("ffmpeg", 1)
    publish("deno", 1)
    for tool in ("ffmpeg", "deno"):
        tool_updates.install_tool_update(tool, "r1")
        assert tool_updates.active_tool_dir(tool) is None
    tool_updates.activate_tool_overlays()
    ffmpeg_v1 = tool_updates.active_tool_dir("ffmpeg")
    deno_v1 = tool_updates.active_tool_dir("deno")
    assert ffmpeg_v1 and deno_v1
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_components, "is_frozen", lambda: True)
    assert Path(paths.get_ffmpeg_executable()).parent == ffmpeg_v1
    assert Path(paths.get_ffprobe_executable()).parent == ffmpeg_v1
    assert runtime_components.find_bundled_runtime() == ("deno", deno_v1 / tool_updates._executable_names("deno")[0])
    assert not any(status.update_available for status in tool_updates.check_tools(1))

    publish("ffmpeg", 2)
    tool_updates.install_tool_update("ffmpeg", "r2")
    assert tool_updates.active_tool_dir("ffmpeg") == ffmpeg_v1
    tool_updates._active.clear()
    tool_updates.activate_tool_overlays()
    ffmpeg_v2 = tool_updates.active_tool_dir("ffmpeg")
    assert ffmpeg_v2 != ffmpeg_v1
    (ffmpeg_v2 / tool_updates._executable_names("ffmpeg")[0]).write_bytes(b"unexpected")
    tool_updates._active.clear()
    tool_updates.activate_tool_overlays()
    assert tool_updates.active_tool_dir("ffmpeg") == ffmpeg_v1
    assert tool_updates.active_tool_dir("deno") == deno_v1

    # An expired authorization record refreshes, and a withdrawn pack falls back.
    offers[:] = [offer for offer in offers if offer.tool == "deno"]
    monkeypatch.setattr(tool_updates, "CONTROL_AGE_SECONDS", -1)
    tool_updates._active.clear()
    tool_updates.activate_tool_overlays()
    assert tool_updates.active_tool_dir("ffmpeg") is None
    assert tool_updates.active_tool_dir("deno") == deno_v1


def test_tools_preserve_working_selection_when_health_check_fails(runtime_channel, monkeypatch):
    publish, _ = runtime_channel
    publish("deno", 1)
    tool_updates.install_tool_update("deno", "r1")
    tool_updates.activate_tool_overlays()
    original = tool_updates.active_tool_dir("deno")
    publish("deno", 2)

    def fail(*args):
        raise app_updates.AppUpdateError("failed")

    monkeypatch.setattr(tool_updates, "_healthcheck", fail)
    with pytest.raises(app_updates.AppUpdateError, match="failed"):
        tool_updates.install_tool_update("deno", "r2")
    tool_updates._active.clear()
    tool_updates.activate_tool_overlays()
    assert tool_updates.active_tool_dir("deno") == original


def test_tool_archive_rejects_additional_paths(tmp_path):
    artifact = tmp_path / "tool.zip"
    members = tool_updates._executable_names("deno")
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(members[0], b"data")
        archive.writestr("../other", b"data")
    with pytest.raises(app_updates.AppUpdateError, match="unexpected"):
        tool_updates._unpack(artifact, tmp_path, members)


def test_catalog_selects_only_current_platform_and_authenticates_assets(monkeypatch):
    monkeypatch.setattr(tool_updates, "_platform_key", lambda: "windows-x64")
    tool = "bananaflow-deno-windows-x64-r2.zip"
    release = {
        "tag_name": tool_updates._channel(), "draft": False, "prerelease": True,
        "assets": [
            _api_asset(tool, b"windows asset"),
            _api_asset("bananaflow-deno-macos-arm64-r9.zip", b"macOS asset"),
        ],
    }
    monkeypatch.setattr(tool_updates, "_fetch_tag_release", lambda client, tag, **kwargs: release)
    offers = tool_updates._catalog(None)
    assert [(offer.tool, offer.revision, offer.asset.name) for offer in offers] == [("deno", 2, tool)]
    release["assets"][0]["url"] = "https://example.org/unofficial"
    with pytest.raises(app_updates.AppUpdateError):
        tool_updates._catalog(None)


def test_gui_verifies_native_tools_off_thread(tmp_path, monkeypatch):
    qt = pytest.importorskip("PySide6.QtCore")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    import main

    app = widgets.QApplication.instance() or widgets.QApplication([])
    observations = []
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(main, "_select_runtime_tools", lambda: observations.append(qt.QThread.currentThread() != app.thread()))
    main._activate_runtime_tools_with_splash()
    assert observations == [True]


def test_portable_maintenance_requires_exact_runtime_directory(tmp_path):
    archive = tmp_path / "maintenance.zip"
    runtime = "_internal-maintenance-1.2.3-r2"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("bananaflow.exe", b"app")
        output.writestr(f"{runtime}/runtime.dll", b"runtime")
    target = tmp_path / "install"
    with pytest.raises(app_update_helper.AppUpdateError, match="portable application"):
        app_update_helper.extract_portable(archive, target)
    with pytest.raises(app_update_helper.AppUpdateError, match="portable application"):
        app_update_helper.extract_portable(archive, target, runtime_dir="_internal-maintenance-1.2.3-r3")
    app_update_helper.extract_portable(archive, target, runtime_dir=runtime)
    assert (target / runtime / "runtime.dll").read_bytes() == b"runtime"
