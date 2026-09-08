"""Regression guards for the fast, visibly responsive startup path."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _run_clean_import(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_core_ui_types_do_not_eagerly_import_provider_clients() -> None:
    result = _run_clean_import(
        "import sys\n"
        "import core.downloader, core.playlist_parser, core.search_engine\n"
        "blocked = {'yt_dlp', 'ytmusicapi', 'httpx', 'bs4'} & set(sys.modules)\n"
        "assert not blocked, sorted(blocked)\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_app_window_import_keeps_deferred_pages_unloaded() -> None:
    result = _run_clean_import(
        "import sys\n"
        "import ui.app_window\n"
        "blocked = {\n"
        " 'ui.panels.metadata_editor.panel',\n"
        " 'ui.panels.settings_panel',\n"
        " 'ui.workers.update_worker',\n"
        " 'yt_dlp',\n"
        "}\n"
        "assert not (blocked & set(sys.modules)), sorted(blocked & set(sys.modules))\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_worker_package_preserves_exports_without_eager_imports() -> None:
    result = _run_clean_import(
        "import sys\n"
        "import ui.workers as workers\n"
        "assert 'ui.workers.update_worker' not in sys.modules\n"
        "assert workers.ThumbnailWorker.__name__ == 'ThumbnailWorker'\n"
        "assert 'ui.workers.thumbnail_worker' in sys.modules\n"
        "assert 'ui.workers.update_worker' not in sys.modules\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_entrypoint_does_not_verify_overlay_before_first_frame() -> None:
    result = _run_clean_import(
        "import sys\n"
        "import main\n"
        "assert 'core.component_overlay' not in sys.modules\n"
        "assert 'yt_dlp' not in sys.modules\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_qfluentwidgets_advertisement_is_suppressed_by_startup_loader() -> None:
    result = _run_clean_import(
        "import main\n"
        "main._load_qfluentwidgets_without_console_ad()\n"
        "print('loaded-cleanly')\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "loaded-cleanly"
    assert "QFluentWidgets Pro" not in result.stdout


def test_production_navigation_preparation_builds_pages_before_handoff() -> None:
    result = _run_clean_import(
        "from ui.app_window import AppWindow\n"
        "class Harness:\n"
        " def __init__(self): self.calls = []\n"
        " def _ensure_metadata_panel(self): self.calls.append('metadata')\n"
        " def _ensure_settings_panel(self): self.calls.append('settings')\n"
        "harness = Harness()\n"
        "AppWindow.prepare_navigation_pages(harness)\n"
        "assert harness.calls == ['metadata', 'settings']\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_main_prepares_navigation_before_showing_interactive_window() -> None:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    main_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = {
        node.func.attr: node.lineno
        for node in ast.walk(main_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "window"
        and node.func.attr in {"prepare_navigation_pages", "show"}
    }
    assert calls["prepare_navigation_pages"] < calls["show"]


def test_cancelled_preflight_worker_suppresses_completion(monkeypatch) -> None:
    import error_handler
    from ui.workers.preflight_worker import PreflightWorker

    callbacks = []

    def cancelled_run(**kwargs):
        callbacks.append(kwargs["should_cancel"])
        return None

    monkeypatch.setattr(error_handler, "run_preflight", cancelled_run)
    worker = PreflightWorker()
    completions = []
    worker.completed.connect(completions.append)
    worker.run()

    assert len(callbacks) == 1
    assert callable(callbacks[0])
    assert completions == []


def test_playwright_availability_probe_exits_without_pending_asyncio_tasks() -> None:
    result = _run_clean_import(
        "from error_handler import check_playwright\n"
        "print(isinstance(check_playwright(), bool))\n"
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "True"
    assert "Task was destroyed" not in result.stderr
    assert "Future exception was never retrieved" not in result.stderr
    assert "TargetClosedError" not in result.stderr


def test_playwright_probe_uses_async_lifecycle() -> None:
    source = (ROOT / "utils" / "playwright_check.py").read_text(encoding="utf-8")
    assert "from playwright.async_api import async_playwright" in source
    assert "from playwright.sync_api import sync_playwright" not in source
