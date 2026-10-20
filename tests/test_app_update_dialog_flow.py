"""Deterministic UI update flow, with network/process boundaries mocked."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication, QWidget

from core.component_updates import ComponentStatus
from core.update_checker import ReleaseInfo
from core.update_state import UpdateStateStore
from ui.dialogs.update_prompt_dialog import UpdatePromptDialog
from ui.i18n import set_language, t


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class ControlledAppWorker(QObject):
    progress = Signal(object, object)
    completed = Signal(object, str)
    finished = Signal()
    created = []

    def __init__(self, release=None, parent=None):
        super().__init__(parent)
        self.release = release
        self.update_status = {}
        self.created.append(self)

    def start(self):
        pass


class ControlledComponentWorker(QObject):
    completed = Signal(bool, str)
    finished = Signal()

    def start(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("ui.dialogs.update_prompt_dialog.is_frozen", lambda: True)
    monkeypatch.setattr("ui.dialogs.update_prompt_dialog.can_prepare_app_update", lambda: True)
    monkeypatch.setattr("ui.workers.app_update_worker.AppUpdateWorker", ControlledAppWorker)
    monkeypatch.setattr("ui.workers.component_install_worker.ComponentInstallWorker", ControlledComponentWorker)
    ControlledAppWorker.created.clear()
    parent = QWidget()
    parent.prepared = []
    parent.restarts = []
    parent._on_app_update_prepared = parent.prepared.append
    parent.request_restart = parent.restarts.append
    store = UpdateStateStore(path=tmp_path / "updates.json")
    release = ReleaseInfo("9.9.9", "https://github.com/BananaFlow-Media/BananaFlow/releases/tag/v9.9.9", "Notes", "2026-10-20T00:00:00Z")
    component = ComponentStatus("yt-dlp", "yt-dlp", "2026.1.1", "2026.10.1", True, True)
    yield parent, store, release, component
    parent.deleteLater()


def test_download_verify_then_explicit_restart_only(setup, monkeypatch):
    parent, store, release, component = setup
    dialog = UpdatePromptDialog(store, release, [component], parent=parent)
    restore = ControlledAppWorker.created[-1]
    assert restore.release is None
    assert dialog._installing
    restore.completed.emit(None, "")
    restore.finished.emit()
    assert not dialog._installing
    dialog._on_app_action()
    worker = ControlledAppWorker.created[-1]
    assert worker.release is release
    assert dialog._installing
    worker.progress.emit(5 * 1024**3, 10 * 1024**3)
    assert dialog._progress.value() == 50
    prepared = SimpleNamespace(version="9.9.9")
    worker.completed.emit(prepared, "")
    assert dialog._installing  # completed is NOT QThread.finished
    dialog.reject()
    dialog.accept()
    assert parent.restarts == []
    worker.finished.emit()
    assert not dialog._installing
    assert parent.prepared == [prepared]
    assert dialog._app_action_btn.text() == t("app_update_install_restart_btn")
    assert parent.restarts == []
    assert not dialog._later_btn.isHidden()
    queued = []
    monkeypatch.setattr("ui.dialogs.update_prompt_dialog.QTimer.singleShot", lambda _, callback: queued.append(callback))
    dialog._on_app_action()
    assert parent.restarts == []
    queued[0]()
    assert parent.restarts == [prepared]


def test_component_success_waits_for_thread_then_offers_restart_later(setup):
    parent, store, _, component = setup
    dialog = UpdatePromptDialog(store, None, [component], parent=parent)
    dialog._on_install_components()
    worker = dialog._install_worker
    worker.completed.emit(True, "installed")
    assert dialog._installing
    assert dialog._restart_btn.isHidden()
    worker.finished.emit()
    assert not dialog._installing
    assert not dialog._restart_btn.isHidden()
    assert not dialog._later_btn.isHidden()
    assert dialog._install_components_btn.isHidden()
    assert parent.restarts == []
    dialog._later_btn.click()
    assert parent.restarts == []


def test_failed_download_is_retryable_and_does_not_offer_install(setup):
    parent, store, release, _ = setup
    dialog = UpdatePromptDialog(store, release, [], parent=parent)
    worker = ControlledAppWorker.created[-1]
    worker.completed.emit(None, "")
    worker.finished.emit()
    dialog._on_app_action()
    worker = ControlledAppWorker.created[-1]
    worker.completed.emit(None, "hash mismatch")
    worker.finished.emit()
    assert dialog._prepared_update is None
    assert not dialog._installing
    assert dialog._app_action_btn.isEnabled()
    assert dialog._app_action_btn.text() == t("app_update_download_btn")
    assert dialog._status_lbl.text() == t("app_update_failed_msg")
    assert parent.prepared == parent.restarts == []
    count = len(ControlledAppWorker.created)
    dialog._on_app_action()
    assert len(ControlledAppWorker.created) == count + 1
    ControlledAppWorker.created[-1].finished.emit()


def test_saved_verified_update_can_be_retried_offline(setup):
    parent, store, _, _ = setup
    prepared = SimpleNamespace(version="9.9.9")
    dialog = UpdatePromptDialog(store, parent=parent, prepared_update=prepared, retry_update=True)
    assert ControlledAppWorker.created == []
    assert dialog._app_action_btn.text() == t("app_update_install_restart_btn")
    assert dialog._status_lbl.text() == t("app_update_retry_msg")
    assert not dialog._later_btn.isHidden()
    dialog._later_btn.click()
    assert parent.restarts == []
    assert dialog._prepared_update is prepared


def test_unsupported_build_keeps_website_and_component_update(setup, monkeypatch):
    parent, store, release, component = setup
    monkeypatch.setattr("ui.dialogs.update_prompt_dialog.can_prepare_app_update", lambda: False)
    dialog = UpdatePromptDialog(store, release, [component], parent=parent)
    assert not hasattr(dialog, "_app_action_btn")
    assert hasattr(dialog, "_install_components_btn")
    assert ControlledAppWorker.created == []


def test_mismatched_saved_release_does_not_install_older_target(setup):
    parent, store, release, _ = setup
    dialog = UpdatePromptDialog(store, release, [], parent=parent)
    worker = ControlledAppWorker.created[-1]
    worker.completed.emit(SimpleNamespace(version="9.0.0"), "")
    worker.finished.emit()
    assert dialog._prepared_update is None
    assert dialog._app_action_btn.text() == t("app_update_download_btn")


def test_prepared_hebrew_rtl_action_labels(setup, qapp):
    parent, store, _, _ = setup
    set_language("he")
    qapp.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    try:
        dialog = UpdatePromptDialog(store, parent=parent, prepared_update=SimpleNamespace(version="9.9.9"))
        assert dialog.layoutDirection() == Qt.LayoutDirection.RightToLeft
        assert dialog._app_action_btn.text() == "התקן והפעל מחדש"
        assert dialog._later_btn.text() == "מאוחר יותר"
    finally:
        set_language("en")
        qapp.setLayoutDirection(Qt.LayoutDirection.LeftToRight)


def test_skip_and_remind_hide_matching_badge_but_later_does_not(setup):
    parent, store, release, component = setup
    deferred = []
    parent._on_updates_deferred = deferred.append
    dialog = UpdatePromptDialog(store, release, [component], parent=parent)
    ControlledAppWorker.created[-1].finished.emit()
    dialog._on_skip()
    assert deferred == [["app:9.9.9", "component:yt-dlp:2026.10.1"]]
    dialog2 = UpdatePromptDialog(store, parent=parent, prepared_update=SimpleNamespace(version="9.9.9"))
    dialog2._later_btn.click()
    assert len(deferred) == 1


def test_helper_error_status_reaches_restore_banner_and_startup_badge(setup, tmp_path, monkeypatch):
    """Use the helper's real persisted error contract, not an invented status."""
    import json
    from core import app_updates, app_update_helper
    from ui.app_window import AppWindow

    parent, store, release, _ = setup
    root = tmp_path / "app-updates"
    work = root / ("a" * 32)
    work.mkdir(parents=True)
    (root / "prepared.json").write_text(json.dumps({"schema": 1, "directory": work.name}))
    plan = work / "plan.json"
    plan.write_text(json.dumps({"schema": -1}))
    # Invalid plans fail before running any installer or touching an app tree.
    assert app_update_helper.apply_update_plan(plan) == 1
    monkeypatch.setattr(app_updates, "_update_root", lambda: root)
    status = app_updates.get_app_update_status()
    assert status["status"] == "error"

    dialog = UpdatePromptDialog(store, release, [], parent=parent)
    worker = ControlledAppWorker.created[-1]
    worker.update_status = status
    prepared = SimpleNamespace(version="9.9.9")
    worker.completed.emit(prepared, "")
    worker.finished.emit()
    assert dialog._status_lbl.text() == t("app_update_retry_msg")
    assert dialog._app_action_btn.text() == t("app_update_install_restart_btn")

    window = SimpleNamespace(
        _prepared_update_worker=SimpleNamespace(update_status=status),
        _on_app_update_prepared=lambda value: None,
    )
    AppWindow._on_prepared_update_restored(window, prepared, "")
    assert window._prepared_update_retry is True


def test_reopened_both_ready_updates_keep_component_only_restart(setup, monkeypatch):
    parent, store, release, component = setup
    prepared = SimpleNamespace(version="9.9.9")
    dialog = UpdatePromptDialog(
        store, release, [component], parent=parent,
        prepared_update=prepared, components_ready=True,
    )
    assert not dialog._restart_btn.isHidden()
    assert not dialog._later_btn.isHidden()
    assert dialog._install_components_btn.isHidden()
    assert dialog._app_action_btn.text() == t("app_update_install_restart_btn")
    callbacks = []
    monkeypatch.setattr("ui.dialogs.update_prompt_dialog.QTimer.singleShot", lambda _, callback: callbacks.append(callback))
    dialog._restart_btn.click()
    callbacks[0]()
    assert parent.restarts == [None]  # components alone, not the saved app update
