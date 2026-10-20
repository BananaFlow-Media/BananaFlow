"""CLI work cannot begin while a full-application replacement owns the lock."""
from __future__ import annotations

import sys

import pytest

import cli
from core import app_update_helper as helper
from ui import i18n
from utils import paths


def test_cli_refuses_work_before_logging_or_components(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(i18n, "t", lambda key: f"translated:{key}")
    monkeypatch.setattr(sys, "argv", ["bananaflow-cli", "--doctor"])
    monkeypatch.setattr(cli, "setup_logging", lambda **_: pytest.fail("logging must not start"))
    directory = tmp_path / "app-updates"
    directory.mkdir()
    with helper._acquire_lock(directory / "apply.lock", "fixture"):
        assert cli.main() == 2
    assert capsys.readouterr().err.strip() == "translated:app_update_running_msg"


def test_cli_uncertain_lock_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(i18n, "t", lambda key: f"translated:{key}")
    monkeypatch.setattr(sys, "argv", ["bananaflow-cli", "--doctor"])
    def failure(_):
        raise OSError("fixture inaccessible lock")
    monkeypatch.setattr(helper, "update_lock_is_held", failure)
    monkeypatch.setattr(cli, "setup_logging", lambda **_: pytest.fail("logging must not start"))
    assert cli.main() == 2
    assert capsys.readouterr().err.strip() == "translated:app_update_state_unavailable_msg"


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_cli_static_information_remains_available(flag, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bananaflow-cli", flag])
    monkeypatch.setattr(helper, "update_lock_is_held", lambda _: pytest.fail("static flag must not inspect update"))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
