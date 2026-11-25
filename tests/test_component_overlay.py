"""Offline security and rollback tests for packaged component overlays."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

import core.component_overlay as component_overlay
from core.component_updates import ComponentUpdateChecker
from core.component_overlay import (
    CHANNEL_TAG,
    ComponentManifest,
    ComponentUpdateError,
    _asset_from_release,
    _safe_extract_bundle,
    _tree_sha256,
    activate_component_overlay,
    install_verified_component_update,
    parse_manifest,
    should_activate_component_overlay,
)


def _bundle(*, unsafe_name: str | None = None) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", "from . import version\n")
        archive.writestr("yt_dlp/version.py", "__version__ = '2026.8.19'\n")
        archive.writestr("yt_dlp_ejs/__init__.py", "")
        archive.writestr(
            "yt_dlp-2026.8.19.dist-info/METADATA",
            "Name: yt-dlp\nVersion: 2026.8.19\n",
        )
        if unsafe_name:
            archive.writestr(unsafe_name, "escape")
    return stream.getvalue()


def _manifest_bytes(bundle: bytes, **overrides) -> bytes:
    bundle_id = overrides.pop("bundle_id", "yt-dlp-2026.8.19__ejs-0.4.0")
    data = {
        "schema": 1,
        "channel": CHANNEL_TAG,
        "bundle_id": bundle_id,
        "disabled": False,
        "superseded_by": "",
        "revoked_bundle_ids": [],
        "compatibility": {
            "min_app_version": "1.0.1",
            "max_app_version_exclusive": "2.0.0",
        },
        "packages": [
            {"name": "yt-dlp", "version": "2026.8.19"},
            {"name": "yt-dlp-ejs", "version": "0.4.0"},
        ],
        "bundle": {
            "asset": f"bananaflow-components-{bundle_id}.zip",
            "size": len(bundle),
            "sha256": hashlib.sha256(bundle).hexdigest(),
        },
    }
    data.update(overrides)
    return json.dumps(data).encode()


def _manifest(bundle: bytes, **overrides) -> ComponentManifest:
    return parse_manifest(_manifest_bytes(bundle, **overrides), app_version="1.0.1")


class TestManifest:
    def test_accepts_complete_compatible_manifest(self):
        parsed = _manifest(_bundle())
        assert parsed.bundle_id.startswith("yt-dlp-")
        assert dict(parsed.packages)["yt-dlp"] == "2026.8.19"

    def test_rejects_wrong_channel_and_incompatible_app(self):
        bundle = _bundle()
        with pytest.raises(ComponentUpdateError, match="schema or channel"):
            parse_manifest(_manifest_bytes(bundle, channel="other"), app_version="1.0.1")
        with pytest.raises(ComponentUpdateError, match="not compatible"):
            parse_manifest(_manifest_bytes(bundle), app_version="1.0.0")

    def test_rejects_unsafe_bundle_identifier(self):
        bundle = _bundle()
        with pytest.raises(ComponentUpdateError, match="identifier is unsafe"):
            parse_manifest(_manifest_bytes(bundle, bundle_id="../../escape"), app_version="1.0.1")

    def test_rejects_malformed_or_duplicate_revocation_identifiers(self):
        bundle = _bundle()
        with pytest.raises(ComponentUpdateError, match="unsafe identifier"):
            _manifest(bundle, revoked_bundle_ids=["../active"])
        with pytest.raises(ComponentUpdateError, match="duplicate identifiers"):
            _manifest(bundle, revoked_bundle_ids=["old", "old"])

    def test_release_asset_requires_official_api_url_and_github_digest(self):
        base = {
            "assets": [{
                "name": "bananaflow-components.json",
                "state": "uploaded",
                "size": 20,
                "url": "https://evil.example/releases/assets/1",
                "digest": "sha256:" + "a" * 64,
            }]
        }
        with pytest.raises(ComponentUpdateError, match="official repository API"):
            _asset_from_release(base, "bananaflow-components.json", 1024)
        base["assets"][0]["url"] = (
            "https://api.github.com/repos/BananaFlow-Media/BananaFlow/releases/assets/1"
        )
        base["assets"][0]["digest"] = None
        with pytest.raises(ComponentUpdateError, match="usable SHA-256"):
            _asset_from_release(base, "bananaflow-components.json", 1024)


@pytest.mark.parametrize(
    "case",
    ["valid", "disabled", "revoked", "incompatible", "bad manifest digest", "bad bundle digest", "no bundle"],
)
def test_packaged_checker_offers_only_installable_channel_metadata(monkeypatch, case):
    bundle = _bundle()
    overrides = {
        "disabled": {"disabled": True},
        "revoked": {"revoked_bundle_ids": ["yt-dlp-2026.8.19__ejs-0.4.0"]},
        "incompatible": {"compatibility": {
            "min_app_version": "2.0.0", "max_app_version_exclusive": "3.0.0",
        }},
    }
    manifest = _manifest_bytes(bundle, **overrides.get(case, {}))
    bundle_name = json.loads(manifest)["bundle"]["asset"]
    asset_prefix = component_overlay._ASSET_API_PREFIX
    manifest_url = asset_prefix + "1"
    bundle_url = asset_prefix + "2"
    assets = [
        {
            "name": component_overlay.MANIFEST_ASSET_NAME,
            "state": "uploaded",
            "url": manifest_url,
            "size": len(manifest),
            "digest": "sha256:" + ("0" * 64 if case == "bad manifest digest" else hashlib.sha256(manifest).hexdigest()),
        },
    ]
    if case != "no bundle":
        assets.append({
            "name": bundle_name,
            "state": "uploaded",
            "url": bundle_url,
            "size": len(bundle),
            "digest": "sha256:" + ("0" * 64 if case == "bad bundle digest" else hashlib.sha256(bundle).hexdigest()),
        })

    requests = []

    def respond(request):
        url = str(request.url)
        requests.append(url)
        if url == component_overlay.RELEASE_API_URL:
            return httpx.Response(200, json={"tag_name": CHANNEL_TAG, "assets": assets})
        if url == manifest_url:
            return httpx.Response(200, content=manifest)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(component_overlay.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    report = ComponentUpdateChecker(_installed={"yt-dlp": "2026.8.1", "yt-dlp-ejs": "0.4.0"}.get).check()

    assert requests == [component_overlay.RELEASE_API_URL, manifest_url]
    assert report.all_checks_ok is (case == "valid")
    assert report.has_updates is (case == "valid")


class TestSafeExtraction:
    def test_rejects_path_traversal_without_writing_outside(self, tmp_path):
        destination = tmp_path / "site-packages"
        destination.mkdir()
        with pytest.raises(ComponentUpdateError, match="unsafe path"):
            _safe_extract_bundle(_bundle(unsafe_name="../escaped.py"), destination)
        assert not (tmp_path / "escaped.py").exists()

    def test_requires_both_component_packages(self, tmp_path):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("yt_dlp/__init__.py", "")
        with pytest.raises(ComponentUpdateError, match="yt_dlp_ejs"):
            _safe_extract_bundle(stream.getvalue(), tmp_path)


class TestInstallAndActivation:
    def test_startup_without_an_overlay_skips_the_control_fetch(self, tmp_path):
        def unexpected_fetch():
            pytest.fail("Control metadata is only needed for an installed overlay")

        assert activate_component_overlay(root=tmp_path, control_fetcher=unexpected_fetch) is None

    def test_frozen_healthcheck_does_not_run_normal_overlay_activation(self):
        assert should_activate_component_overlay(argv=["bananaflow.exe"], frozen=True)
        assert not should_activate_component_overlay(
            argv=["bananaflow.exe", "--component-healthcheck", "staging/site-packages"],
            frozen=True,
        )
        assert not should_activate_component_overlay(argv=["main.py"], frozen=False)

    def test_integrity_healthcheck_and_atomic_next_launch_selection(self, tmp_path):
        bundle = _bundle()
        checked = []
        result = install_verified_component_update(
            root=tmp_path,
            fetcher=lambda: (_manifest(bundle), bundle),
            healthcheck=lambda path: checked.append(path),
        )
        assert result.restart_required
        assert checked
        state = json.loads((tmp_path / "active.json").read_text(encoding="utf-8"))
        assert state["active"] == result.bundle_id
        assert (tmp_path / "bundles" / result.bundle_id / "site-packages" / "yt_dlp" / "__init__.py").is_file()

        old_path = list(sys.path)
        try:
            selected = activate_component_overlay(
                root=tmp_path,
                control_fetcher=lambda: _manifest(_bundle()),
            )
            assert selected == tmp_path / "bundles" / result.bundle_id / "site-packages"
            assert sys.path[0] == str(selected)
        finally:
            sys.path[:] = old_path

    def test_newer_bundled_downloader_takes_precedence_over_older_overlay(self, tmp_path, monkeypatch):
        bundle = _bundle()
        result = install_verified_component_update(
            root=tmp_path, fetcher=lambda: (_manifest(bundle), bundle), healthcheck=lambda _path: None,
        )
        packaged = tmp_path / "packaged"
        metadata = packaged / "yt_dlp-2026.11.23.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text("Name: yt-dlp\nVersion: 2026.11.23\n", encoding="utf-8")
        monkeypatch.setattr(sys, "_MEIPASS", str(packaged), raising=False)

        assert activate_component_overlay(root=tmp_path) is None
        state = json.loads((tmp_path / "active.json").read_text(encoding="utf-8"))
        assert state["active"] == result.bundle_id

    def test_hash_mismatch_never_changes_active_pointer(self, tmp_path):
        bundle = _bundle()
        manifest = _manifest(bundle)
        with pytest.raises(ComponentUpdateError, match="integrity"):
            install_verified_component_update(
                root=tmp_path,
                fetcher=lambda: (manifest, bundle + b"tampered"),
                healthcheck=lambda _path: None,
            )
        assert not (tmp_path / "active.json").exists()

    def test_healthcheck_failure_never_changes_active_pointer(self, tmp_path):
        bundle = _bundle()

        def fail(_path):
            raise ComponentUpdateError("health failed")

        with pytest.raises(ComponentUpdateError, match="health failed"):
            install_verified_component_update(
                root=tmp_path, fetcher=lambda: (_manifest(bundle), bundle), healthcheck=fail,
            )
        assert not (tmp_path / "active.json").exists()

    def test_invalid_active_bundle_rolls_back_to_previous(self, tmp_path):
        previous = tmp_path / "bundles" / "previous"
        (previous / "site-packages" / "yt_dlp").mkdir(parents=True)
        (previous / "site-packages" / "yt_dlp_ejs").mkdir()
        (previous / "site-packages" / "yt_dlp" / "__init__.py").write_text("")
        (previous / "site-packages" / "yt_dlp_ejs" / "__init__.py").write_text("")
        (previous / "manifest.json").write_text(json.dumps({
            "schema": 1,
            "bundle_id": "previous",
            "min_app_version": "1.0.1",
            "max_app_version_exclusive": "2.0.0",
            "tree_sha256": _tree_sha256(previous / "site-packages"),
        }))
        (tmp_path / "active.json").write_text(json.dumps({
            "schema": 1, "active": "broken", "previous": "previous",
        }))

        old_path = list(sys.path)
        try:
            selected = activate_component_overlay(
                root=tmp_path,
                control_fetcher=lambda: _manifest(_bundle()),
            )
            assert selected == previous / "site-packages"
            state = json.loads((tmp_path / "active.json").read_text())
            assert state == {"active": "previous", "previous": "", "schema": 1}
        finally:
            sys.path[:] = old_path

    def test_expired_control_record_revokes_active_bundle_and_uses_previous(self, tmp_path):
        old_bundle = _bundle()
        active_bundle = _bundle()
        old_manifest = _manifest(old_bundle, bundle_id="previous")
        active_manifest = _manifest(active_bundle, bundle_id="active")
        install_verified_component_update(
            root=tmp_path,
            fetcher=lambda: (old_manifest, old_bundle),
            healthcheck=lambda _path: None,
        )
        install_verified_component_update(
            root=tmp_path,
            fetcher=lambda: (active_manifest, active_bundle),
            healthcheck=lambda _path: None,
        )

        old_path = list(sys.path)
        try:
            selected = activate_component_overlay(
                root=tmp_path,
                now=2_000_000_000,
                control_fetcher=lambda: _manifest(
                    active_bundle,
                    bundle_id="active",
                    revoked_bundle_ids=["active"],
                ),
            )
            assert selected == tmp_path / "bundles" / "previous" / "site-packages"
            state = json.loads((tmp_path / "active.json").read_text(encoding="utf-8"))
            assert state == {"active": "previous", "previous": "", "schema": 1}
        finally:
            sys.path[:] = old_path

    def test_stale_control_failure_does_not_activate_overlay(self, tmp_path):
        bundle = _bundle()
        manifest = _manifest(bundle)
        install_verified_component_update(
            root=tmp_path,
            fetcher=lambda: (manifest, bundle),
            healthcheck=lambda _path: None,
        )

        def unavailable():
            raise ComponentUpdateError("offline")

        assert activate_component_overlay(
            root=tmp_path,
            now=2_000_000_000,
            control_fetcher=unavailable,
        ) is None

    def test_new_app_version_never_activates_an_old_overlay(self, tmp_path, monkeypatch):
        bundle = _bundle()
        manifest = _manifest(bundle)
        install_verified_component_update(
            root=tmp_path,
            fetcher=lambda: (manifest, bundle),
            healthcheck=lambda _path: None,
        )

        monkeypatch.setattr(component_overlay, "FULL_VERSION", "2.0.0")
        assert activate_component_overlay(root=tmp_path) is None
