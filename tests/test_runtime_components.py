"""
tests/test_runtime_components.py  –  Bundled-component detection/activation
============================================================================
All tests build a fake bundle layout under tmp_path and point
utils.paths.get_install_dir at it, so nothing depends on how the test
machine is actually set up and nothing touches a real install.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.runtime_components as rc


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_activation_flag():
    """activate_bundled_components() is idempotent via a module global —
    reset it between tests so each test starts clean."""
    rc._activated = False
    yield
    rc._activated = False


def _point_install_dir(monkeypatch, install: Path):
    monkeypatch.setattr(rc, "get_install_dir", lambda: install)
    # is_frozen is imported into rc's namespace; default False is fine.
    monkeypatch.setattr(rc, "is_frozen", lambda: False)


def _make_provider(plugins_dir: Path, module: str = "getpot_bgutil") -> None:
    ext = plugins_dir / "bgutil-ytdlp-pot-provider" / "yt_dlp_plugins" / "extractor"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / f"{module}.py").write_text("# fake provider plugin\n", encoding="utf-8")


def _make_script_provider(plugins_dir: Path) -> None:
    _make_provider(plugins_dir, "getpot_bgutil")
    _make_provider(plugins_dir, "getpot_bgutil_script")


def _make_runtime(runtime_dir: Path, exe_name: str) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    exe = runtime_dir / exe_name
    exe.write_text("binary", encoding="utf-8")
    return exe


def _make_backend(root: Path, version: str = "1.3.1") -> Path:
    server = root / "pot-provider-backend" / "bgutil-ytdlp-pot-provider" / "server"
    (server / "src").mkdir(parents=True, exist_ok=True)
    (server / "src" / "generate_once.ts").write_text("// fake script\n", encoding="utf-8")
    (server / "node_modules").mkdir(parents=True, exist_ok=True)
    (server / "package.json").write_text(f'{{"version": "{version}"}}\n', encoding="utf-8")
    return server


def _runtime_exe_name(name: str) -> str:
    base = {"deno": "deno", "node": "node", "quickjs": "qjs"}[name]
    return base + (".exe" if os.name == "nt" else "")


# ──────────────────────────────────────────────────────────────────────────────
# Provider detection (filesystem scan)
# ──────────────────────────────────────────────────────────────────────────────

class TestProviderScan:

    def test_no_plugins_dir_means_no_providers(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        info = rc.detect_bundled_components()
        assert info.plugins_dir is None
        assert info.provider_modules == []
        assert not info.has_bundled_provider

    def test_bundled_provider_detected(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_provider(tmp_path / "yt-dlp-plugins")
        info = rc.detect_bundled_components()
        assert info.plugins_dir == tmp_path / "yt-dlp-plugins"
        assert info.provider_modules == ["getpot_bgutil"]
        assert info.has_bundled_provider
        assert not info.has_bundled_script_provider

    def test_non_getpot_modules_ignored(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        ext = tmp_path / "yt-dlp-plugins" / "somepkg" / "yt_dlp_plugins" / "extractor"
        ext.mkdir(parents=True)
        (ext / "some_extractor.py").write_text("# not a provider\n", encoding="utf-8")
        (ext / "_private.py").write_text("# underscore ignored\n", encoding="utf-8")
        info = rc.detect_bundled_components()
        assert info.plugins_dir is not None       # folder exists
        assert info.provider_modules == []         # but no getpot_* provider
        assert not info.has_bundled_provider

    def test_scan_is_filesystem_only_not_import(self, tmp_path, monkeypatch):
        # scan_bundled_provider_modules must never import the module.
        import importlib
        monkeypatch.setattr(
            importlib, "import_module",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not import")),
        )
        plugins = tmp_path / "yt-dlp-plugins"
        _make_provider(plugins)
        assert rc.scan_bundled_provider_modules(plugins) == ["getpot_bgutil"]

    def test_scan_none_dir_returns_empty(self):
        assert rc.scan_bundled_provider_modules(None) == []


# ──────────────────────────────────────────────────────────────────────────────
# JS runtime detection
# ──────────────────────────────────────────────────────────────────────────────

class TestRuntimeScan:

    def test_no_runtime_dir(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        info = rc.detect_bundled_components()
        assert info.runtime_dir is None
        assert not info.has_bundled_js_runtime

    def test_bundled_deno_detected(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        exe = _make_runtime(tmp_path / "runtime", _runtime_exe_name("deno"))
        info = rc.detect_bundled_components()
        assert info.has_bundled_js_runtime
        assert info.js_runtime_name == "deno"
        assert info.js_runtime_path == exe

    def test_deno_preferred_over_node(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("node"))
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("deno"))
        info = rc.detect_bundled_components()
        assert info.js_runtime_name == "deno"

    def test_node_detected_when_only_node(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("node"))
        info = rc.detect_bundled_components()
        assert info.js_runtime_name == "node"


class TestProviderBackendScan:

    def test_no_backend_dir(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        info = rc.detect_bundled_components()
        assert info.provider_backend_dir is None
        assert not info.has_bundled_provider_backend

    def test_deno_script_backend_detected(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        server = _make_backend(tmp_path)
        info = rc.detect_bundled_components()
        assert info.has_bundled_provider_backend
        assert info.provider_backend_dir == server
        assert info.provider_backend_mode == "script-deno"
        assert info.provider_backend_version == "1.3.1"
        assert info.provider_script_path == server / "src" / "generate_once.ts"
        assert info.provider_node_modules_dir == server / "node_modules"

    def test_backend_requires_node_modules(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        server = _make_backend(tmp_path)
        (server / "node_modules").rmdir()
        info = rc.detect_bundled_components()
        assert info.provider_backend_dir is None
        assert not info.has_bundled_provider_backend

    def test_extractor_args_point_to_server_home(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        server = _make_backend(tmp_path)
        args = rc.bundled_pot_provider_extractor_args()
        assert args == {
            "youtubepot-bgutilscript": {
                "server_home": [str(server)],
            },
        }


# ──────────────────────────────────────────────────────────────────────────────
# Activation (side effects: PATH, plugin dir registration)
# ──────────────────────────────────────────────────────────────────────────────

class TestActivation:

    def test_runtime_dir_prepended_to_path(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("deno"))
        monkeypatch.setenv("PATH", "/existing/path")

        info = rc.activate_bundled_components()

        assert info.has_bundled_js_runtime
        parts = os.environ["PATH"].split(os.pathsep)
        assert parts[0] == str(tmp_path / "runtime")
        assert "/existing/path" in parts

    def test_activation_is_idempotent(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("deno"))
        monkeypatch.setenv("PATH", "/existing/path")

        rc.activate_bundled_components()
        rc.activate_bundled_components()   # second call must not double-prepend

        assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path / "runtime")) == 1

    def test_nothing_bundled_leaves_path_untouched(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        monkeypatch.setenv("PATH", "/existing/path")
        info = rc.activate_bundled_components()
        assert not info.has_bundled_provider
        assert not info.has_bundled_js_runtime
        assert os.environ["PATH"] == "/existing/path"

    def test_backend_activation_sets_controlled_cache_env(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_script_provider(tmp_path / "yt-dlp-plugins")
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("deno"))
        _make_backend(tmp_path)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("DENO_NO_PROMPT", raising=False)
        monkeypatch.setattr(rc, "get_app_data_dir", lambda: tmp_path / "appdata")

        info = rc.activate_bundled_components()

        assert info.has_full_po_provider_stack
        assert os.environ["XDG_CACHE_HOME"] == str(tmp_path / "appdata" / "cache")
        assert os.environ["DENO_NO_PROMPT"] == "1"

    def test_full_stack_requires_script_provider_module(self, tmp_path, monkeypatch):
        _point_install_dir(monkeypatch, tmp_path)
        _make_provider(tmp_path / "yt-dlp-plugins", "getpot_bgutil")
        _make_runtime(tmp_path / "runtime", _runtime_exe_name("deno"))
        _make_backend(tmp_path)

        info = rc.detect_bundled_components()

        assert info.has_bundled_provider
        assert not info.has_bundled_script_provider
        assert info.has_bundled_provider_backend
        assert not info.has_full_po_provider_stack

    def test_detection_never_raises_on_bad_state(self, monkeypatch):
        # get_install_dir blowing up must degrade to "nothing bundled",
        # not propagate — startup must never crash on this.
        monkeypatch.setattr(rc, "get_install_dir", lambda: (_ for _ in ()).throw(OSError("boom")))
        info = rc.detect_bundled_components()
        assert not info.has_bundled_provider
        assert not info.has_bundled_js_runtime
        info2 = rc.activate_bundled_components()
        assert not info2.has_bundled_js_runtime
