"""Updater restart authorization must survive Qt close vetoes and thread races."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication, QWidget

from ui.app_window import AppWindow
from ui.i18n import TRANSLATIONS, request_language_restart, t
from ui.services.restart import RestartRequest, launch_after_shutdown, restart_block_reason


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class Event:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class WindowHarness:
    request_restart = AppWindow.request_restart

    def __init__(self):
        self._restart_request = None
        self._cfg = SimpleNamespace(tray_on_close=False)
        self._tray = None
        self._metadata_ctrl = SimpleNamespace(
            has_active_shutdown_work=lambda: False,
            guard_lifecycle_action=lambda *a: True,
            cancel_scan=lambda: None, cancel_apply=lambda: None,
        )
        self._converter_panel = SimpleNamespace(_worker=None)
        self._download_ctrl = SimpleNamespace(is_downloading=lambda: False)
        self._fetch_ctrl = SimpleNamespace(_fetch_worker=None, _scraper_worker=None)
        self._search_ctrl = SimpleNamespace(_search_worker=None)
        self._clipboard_worker = None
        self._save_state = lambda: None
        self._save_queue_state = lambda: None
        self._svc = SimpleNamespace(close=lambda: None)
        self._close_calls = 0
        self._enabled = True
        self._shutdown_accepted = False
        self._owned = []

    def setEnabled(self, enabled):
        self._enabled = enabled

    def findChildren(self, cls):
        assert cls is QThread
        return self._owned

    def close(self):
        self._close_calls += 1


@pytest.mark.parametrize("operation, key", [
    ("downloads", "restart_busy_downloads"),
    ("conversion", "restart_busy_conversion"),
    ("metadata", "restart_busy_metadata"),
])
def test_restart_preserves_active_work(operation, key, monkeypatch):
    window = WindowHarness()
    if operation == "downloads":
        window._download_ctrl.is_downloading = lambda: True
    if operation == "conversion":
        window._converter_panel._worker = SimpleNamespace(isRunning=lambda: True)
    if operation == "metadata":
        window._metadata_ctrl.has_active_shutdown_work = lambda: True
    messages = []
    monkeypatch.setattr("ui.app_window.show_info", lambda *args: messages.append(args[-1]))
    assert restart_block_reason(window) == key
    assert window.request_restart() is False
    assert window._restart_request is None
    assert window._close_calls == 0
    assert window._enabled
    assert messages == [t(key)]


def test_tag_editor_cancel_never_leaves_latent_restart(monkeypatch):
    window = WindowHarness()
    continuations = []
    window._metadata_ctrl.guard_lifecycle_action = lambda _, continuation: continuations.append(continuation) or False
    assert window.request_restart() is False
    assert window._restart_request is None
    window._shutdown_accepted = True  # a later ordinary close is not a restart
    monkeypatch.setattr("ui.services.restart.subprocess.Popen", lambda *a, **k: pytest.fail("spawned after veto"))
    assert launch_after_shutdown(window) is False
    # Only the approved existing Tag Editor continuation can re-enter.
    window._metadata_ctrl.guard_lifecycle_action = lambda *a: True
    continuations[0]()
    assert isinstance(window._restart_request, RestartRequest)
    assert window._close_calls == 1


def test_restart_bypasses_tray_only_after_explicit_request(monkeypatch):
    window = WindowHarness()
    window._cfg.tray_on_close = True
    window._tray = SimpleNamespace(isVisible=lambda: True, hide=lambda: None)
    window._restart_request = RestartRequest()
    window._restart_lifecycle_approved = True
    window._metadata_ctrl.guard_lifecycle_action = lambda *a: pytest.fail("double lifecycle approval")
    monkeypatch.setitem(sys.modules, "keyboard", SimpleNamespace(unhook_all=lambda: None))
    scheduled = []
    monkeypatch.setattr("ui.app_window.QTimer.singleShot", lambda _, cb: scheduled.append(cb))
    event = Event()
    AppWindow.closeEvent(window, event)
    assert event.accepted and not event.ignored
    assert window._shutdown_accepted
    assert len(scheduled) == 1


def test_owned_worker_defers_close_without_destroying_services(monkeypatch):
    window = WindowHarness()
    interruptions, closed = [], []
    worker = SimpleNamespace(isRunning=lambda: True, requestInterruption=lambda: interruptions.append(True))
    window._owned = [worker]
    window._svc.close = lambda: closed.append(True)
    scheduled = []
    monkeypatch.setattr("ui.app_window.QTimer.singleShot", lambda _, cb: scheduled.append(cb))
    event = Event()
    AppWindow.closeEvent(window, event)
    assert event.ignored and not event.accepted
    assert not window._shutdown_accepted
    assert interruptions == [True]
    assert closed == []
    assert len(scheduled) == 1


def test_normal_close_does_not_kill_conversion(monkeypatch):
    window = WindowHarness()
    window._converter_panel._worker = SimpleNamespace(isRunning=lambda: True)
    monkeypatch.setattr("ui.app_window.show_info", lambda *args: None)
    event = Event()
    AppWindow.closeEvent(window, event)
    assert event.ignored and not event.accepted


def test_launch_is_after_accepted_shutdown_and_exactly_once(monkeypatch):
    window = WindowHarness()
    window._restart_request = RestartRequest()
    launches = []
    monkeypatch.setattr("ui.services.restart.subprocess.Popen", lambda *a, **k: launches.append((a, k)))
    assert launch_after_shutdown(window) is False
    window._shutdown_accepted = True
    assert launch_after_shutdown(window) is True
    assert launch_after_shutdown(window) is False
    assert len(launches) == 1


def test_app_update_handoff_failure_preserves_retry_intent(monkeypatch):
    window = WindowHarness()
    prepared = object()
    window._restart_request = RestartRequest(prepared)
    window._shutdown_accepted = True
    calls = []

    def launch(value):
        calls.append(value)
        if len(calls) == 1:
            raise OSError("simulated spawn failure")

    monkeypatch.setitem(sys.modules, "core.app_updates", SimpleNamespace(launch_prepared_app_update=launch))
    with pytest.raises(OSError):
        launch_after_shutdown(window)
    assert window._restart_request.prepared_update is prepared
    assert launch_after_shutdown(window)
    assert calls == [prepared, prepared]


def test_language_restart_uses_safe_window_gate_without_quit():
    calls = []
    app = SimpleNamespace(topLevelWidgets=lambda: [SimpleNamespace(request_restart=lambda: calls.append(True) or False)])
    assert request_language_restart(app, "he") is False
    assert calls == [True]


def test_restart_and_app_update_strings_have_hebrew_parity():
    keys = [key for key in TRANSLATIONS["en"] if key.startswith(("restart_", "app_update_", "update_available", "update_ready"))]
    for key in keys:
        assert key in TRANSLATIONS["he"], key


def test_update_deferral_clears_available_items_not_prepared_update():
    refreshed = []
    prepared = object()
    window = SimpleNamespace(
        _last_update_release=SimpleNamespace(version="9.9.9"),
        _last_component_updates=[SimpleNamespace(key="yt-dlp", latest_version="2026.10.1")],
        _prepared_app_update=prepared,
        _refresh_update_affordance=lambda: refreshed.append(True),
    )
    AppWindow._on_updates_deferred(window, ["app:9.9.9", "component:yt-dlp:2026.10.1"])
    assert window._last_update_release is None
    assert window._last_component_updates == []
    assert window._prepared_app_update is prepared
    assert refreshed == [True]


def test_artwork_close_veto_reenables_window_and_allows_restart_retry(monkeypatch):
    window = WindowHarness()
    waiting = [True]
    checks = []

    def shutdown(*, timeout_ms):
        checks.append(timeout_ms)
        return not waiting[0]

    window._metadata_panel = SimpleNamespace(shutdown_artwork_workers=shutdown)
    monkeypatch.setattr("ui.app_window.show_info", lambda *args: None)
    monkeypatch.setitem(sys.modules, "keyboard", SimpleNamespace(unhook_all=lambda: None))
    monkeypatch.setattr("ui.app_window.QTimer.singleShot", lambda *args: None)
    assert window.request_restart()
    assert not window._enabled
    first = Event()
    AppWindow.closeEvent(window, first)
    assert first.ignored
    assert window._enabled
    assert window._restart_request is None
    assert not window._shutdown_accepted
    assert checks == [0]  # no GUI-thread decoder wait
    waiting[0] = False
    assert window.request_restart()
    second = Event()
    AppWindow.closeEvent(window, second)
    assert second.accepted
    assert window._shutdown_accepted


def test_metadata_timeout_disarms_disabled_restart_and_late_close(monkeypatch):
    window = WindowHarness()
    window._restart_request = RestartRequest()
    window._enabled = False
    disconnected = []
    window._on_metadata_shutdown_timeout = lambda: None
    window._metadata_ctrl.shutdown_ready = SimpleNamespace(disconnect=lambda slot: disconnected.append("ready"))
    window._metadata_ctrl.shutdown_timed_out = SimpleNamespace(disconnect=lambda slot: disconnected.append("timeout"))
    monkeypatch.setattr("qfluentwidgets.InfoBar.warning", lambda **kwargs: None)
    AppWindow._on_metadata_shutdown_timeout(window)
    assert window._restart_request is None
    assert window._enabled
    assert disconnected == ["ready", "timeout"]
