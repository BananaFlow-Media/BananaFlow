"""A reopened GUI may not race a helper that is replacing its files."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import app_update_helper as helper
from ui.i18n import t


@pytest.fixture
def entrypoint(monkeypatch):
    # The entry point normally configures persistent logging on import.
    # This unit test does not need to write the user's logging directory.
    import importlib
    monkeypatch.setattr("utils.logging_config.setup_logging", lambda **kwargs: None)
    return importlib.import_module("main")


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.paths.get_app_data_dir", lambda: tmp_path)
    path = tmp_path / "app-updates" / "apply.lock"
    path.parent.mkdir()
    return path


def test_live_os_held_lock_blocks_startup_without_loading_services(lock_path, monkeypatch, entrypoint):
    messages = []
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a: messages.append(a[-1]))
    handle = helper._lock_file(lock_path)
    try:
        assert entrypoint._allow_startup_during_app_update() is False
    finally:
        handle.close()
    assert messages == [t("app_update_running_msg")]


def test_stale_file_and_missing_file_do_not_block_startup(lock_path, monkeypatch, entrypoint):
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a: pytest.fail("unexpected startup blocker"))
    assert entrypoint._allow_startup_during_app_update()
    lock_path.write_text("previous crashed updater")
    assert entrypoint._allow_startup_during_app_update()


def test_uncertain_lock_state_fails_closed_with_localized_message(lock_path, monkeypatch, entrypoint):
    messages = []
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a: messages.append(a[-1]))
    def fail(_):
        raise OSError("lock access unavailable")
    monkeypatch.setattr(helper, "update_lock_is_held", fail)
    assert not entrypoint._allow_startup_during_app_update()
    assert messages == [t("app_update_state_unavailable_msg")]


def test_production_guard_precedes_overlay_and_services(entrypoint):
    tree = ast.parse(Path(entrypoint.__file__).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = list(n for n in ast.walk(node) if isinstance(n, ast.Call))
    guarded = next(n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "_allow_startup_during_app_update")
    overlay = next(n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "_activate_selected_component_overlay")
    services = next(n.lineno for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "create_default")
    assert guarded < overlay < services
