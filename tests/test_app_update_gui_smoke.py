"""Real AppWindow startup + accepted-close updater smoke evidence.

macOS uses Cocoa because native frameless windows dereference NSView handles;
Qt's offscreen pseudo-handles cannot safely exercise that production path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("update_restart", [False, True])
def test_real_gui_smoke_preserves_preferences_and_uses_safe_close(tmp_path, update_restart):
    env = dict(os.environ)
    env.update(
        QT_QPA_PLATFORM="cocoa" if sys.platform == "darwin" else "offscreen",
        PYTHONFAULTHANDLER="1", PYTHONUNBUFFERED="1",
        HOME=str(tmp_path), APPDATA=str(tmp_path),
        XDG_DATA_HOME=str(tmp_path / "data"), XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_STATE_HOME=str(tmp_path / "state"),
    )
    code = f'''
import json
import importlib.abc
import sys
from types import SimpleNamespace

class RequireApplicationBeforeWindow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ui.app_window":
            from PySide6.QtWidgets import QApplication
            assert QApplication.instance() is not None, "AppWindow imported before QApplication"
        return None

sys.meta_path.insert(0, RequireApplicationBeforeWindow())
from config import AppConfig
cfg = AppConfig()
cfg.language = "he"
cfg.theme = "dark"
cfg.check_updates = False
cfg.tray_on_close = True
cfg.save()
from ui.app_update_smoke import run_gui_update_smoke
prepared = SimpleNamespace(version="99.0.0") if {update_restart!r} else None
result = run_gui_update_smoke(prepared)
after = AppConfig()
assert after.language == "he"
assert after.theme == "dark"
assert after.check_updates is False
assert after.tray_on_close is True
print("GUI_SMOKE_RESULT=" + json.dumps(result))
'''
    proc = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-u", "-c", code], env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=45,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(line for line in proc.stdout.splitlines() if line.startswith("GUI_SMOKE_RESULT="))
    result = json.loads(line.split("=", 1)[1])
    assert result["ok"] is True
    assert result["qt_platform"] == ("cocoa" if sys.platform == "darwin" else "offscreen")
    assert proc.stderr.index("QApplication ready") < proc.stderr.index("Importing AppWindow")
    assert result["gui_shown"]
    assert result["navigation_prepared"]
    assert result["shutdown_accepted"]
    assert result["safe_restart_requested"] is update_restart
    assert result["preferences"]["language"] == "he"
    assert result["preferences"]["theme"] == "dark"
    assert "QThread: Destroyed while thread" not in proc.stderr
