"""Focused regressions for serialized download-recovery UI decisions."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QDialog
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from core.download_recovery import (
    DownloadFailureIncident,
    FailureIncidentItem,
    RecoveryDecision,
    SystemicRecoveryPolicy,
    UserActionRequest,
)
from ui.app_window import AppWindow
import ui.app_window as app_window_module


class _CallbackSignal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self._callbacks):
            callback(*args)


class _FakeMessageDialog:
    instances: list["_FakeMessageDialog"] = []
    fail_next_open = False

    def __init__(self, *_args, **_kwargs) -> None:
        self.finished = _CallbackSignal()
        self.opened = False
        self.rejected = False
        self.deleted = False
        self.args = _args
        self.kwargs = _kwargs
        self.updates = []
        type(self).instances.append(self)

    def open(self) -> None:
        self.opened = True
        if type(self).fail_next_open:
            type(self).fail_next_open = False
            raise RuntimeError("dialog open failed")

    def reject(self) -> None:
        self.rejected = True
        self.finished.emit(int(QDialog.DialogCode.Rejected))

    def accept(self) -> None:
        self.finished.emit(int(QDialog.DialogCode.Accepted))

    def auxiliary(self) -> None:
        self.finished.emit(2)

    def update_message(self, title, text, details="") -> None:
        self.updates.append((title, text, details))

    def deleteLater(self) -> None:
        self.deleted = True


class _DownloadControllerStub:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.resolutions = []
        self.incident_resolutions = []
        self.cancel_count = 0

    def resolve_user_action(self, request, decision) -> bool:
        resolved = RecoveryDecision(decision)
        self.resolutions.append((request, resolved))
        return request.resolve(resolved)

    def resolve_failure_incident(self, incident_id, decision) -> bool:
        self.incident_resolutions.append((incident_id, RecoveryDecision(decision)))
        return True

    def is_downloading(self) -> bool:
        return self.running

    def cancel_all(self) -> None:
        self.cancel_count += 1


class _CancelStub:
    def __init__(self) -> None:
        self.cancel_count = 0

    def cancel(self) -> None:
        self.cancel_count += 1


class _StatusBarStub:
    def __init__(self) -> None:
        self.cancelling_count = 0
        self.idle_count = 0

    def show_cancelling(self) -> None:
        self.cancelling_count += 1

    def reset_to_idle(self) -> None:
        self.idle_count += 1


class _RecoveryWindowHarness:
    _update_rate_limit_dialog = AppWindow._update_rate_limit_dialog
    _finish_rate_limit_dialog = AppWindow._finish_rate_limit_dialog
    _on_failure_incident_updated_ui = AppWindow._on_failure_incident_updated_ui
    _failure_incident_text = AppWindow._failure_incident_text
    _open_failure_incident_dialog = AppWindow._open_failure_incident_dialog
    _finish_failure_incident_dialog = AppWindow._finish_failure_incident_dialog
    _open_next_failure_incident_dialog = AppWindow._open_next_failure_incident_dialog
    _on_user_action_required_ui = AppWindow._on_user_action_required_ui
    _open_user_action_dialog = AppWindow._open_user_action_dialog
    _finish_user_action_dialog = AppWindow._finish_user_action_dialog
    _open_next_download_error_request = AppWindow._open_next_download_error_request
    _cancel_download_error_requests = AppWindow._cancel_download_error_requests
    _on_cancel = AppWindow._on_cancel

    def __init__(self, *, downloading: bool = False) -> None:
        self._download_ctrl = _DownloadControllerStub(running=downloading)
        self._pending_download_error_requests = []
        self._download_error_request_ids = set()
        self._active_download_error_dialog = None
        self._active_download_error_request = None
        self._download_recovery_workflow_active = False
        self._download_failure_incidents = {}
        self._pending_failure_incident_ids = []
        self._active_failure_incident_dialog = None
        self._active_failure_incident_id = ""
        self._match_prefetcher = _CancelStub()
        self._fetch_ctrl = _CancelStub()
        self._search_ctrl = _CancelStub()
        self._status_bar = _StatusBarStub()

    @staticmethod
    def _localized_error_text(headline: str, detail: str, _raw: str):
        return headline, detail

    @staticmethod
    def _is_browser_cookie_error_text(_text: str) -> bool:
        return False

    @staticmethod
    def _is_auth_error_text(_text: str) -> bool:
        return False

    @staticmethod
    def _run_cookie_wizard_ui() -> bool:
        return False


class _RunningWorker:
    def __init__(self) -> None:
        self.running = True
        self.cancel_count = 0

    def isRunning(self) -> bool:
        return self.running

    def cancel(self) -> None:
        self.cancel_count += 1


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def recovery_ui(monkeypatch, app):
    _FakeMessageDialog.instances.clear()
    _FakeMessageDialog.fail_next_open = False
    monkeypatch.setattr(
        app_window_module, "StyledMessageDialog", _FakeMessageDialog,
    )
    monkeypatch.setattr(
        app_window_module,
        "QTimer",
        SimpleNamespace(singleShot=lambda *_args: _args[-1]()),
    )
    return _RecoveryWindowHarness()


def _request(key: str) -> UserActionRequest:
    error = SimpleNamespace(
        headline=f"Error {key}",
        detail="A download failed",
        raw="provider error",
        message_key="err_generic",
    )
    return UserActionRequest(key=key, error=error)


def _incident(*, auth: bool = False) -> DownloadFailureIncident:
    error = SimpleNamespace(
        headline="Cookies expired" if auth else "No result",
        detail="Repair authentication" if auth else "Search returned no items",
        raw="exact provider error",
        message_key="err_cookies_expired" if auth else "err_no_search_results",
    )
    policy = SystemicRecoveryPolicy("youtube_auth", 3, True) if auth else None
    return DownloadFailureIncident(
        scope="youtube_auth" if auth else "error:err_no_search_results",
        error=error,
        systemic_policy=policy,
    )


def test_one_live_incident_dialog_updates_from_one_to_three_tracks(recovery_ui):
    incident = _incident()
    incident.add(
        FailureIncidentItem("one", "Song One", "https://example/1", "same raw"),
        stopped_all=False,
        streak=0,
    )
    recovery_ui._on_failure_incident_updated_ui(incident)

    assert len(_FakeMessageDialog.instances) == 1
    dialog = _FakeMessageDialog.instances[0]
    assert "Song One" in dialog.kwargs["details"]

    incident.add(
        FailureIncidentItem("two", "Song Two", "https://example/2", "same raw"),
        stopped_all=False,
        streak=0,
    )
    recovery_ui._on_failure_incident_updated_ui(incident)
    incident.add(
        FailureIncidentItem("three", "Song Three", "https://example/3", "same raw"),
        stopped_all=True,
        streak=3,
    )
    recovery_ui._on_failure_incident_updated_ui(incident)

    assert len(_FakeMessageDialog.instances) == 1
    assert len(dialog.updates) == 2
    assert "3" in dialog.updates[-1][0]
    assert "Song Three" in dialog.updates[-1][2]

    dialog.reject()
    assert recovery_ui._download_ctrl.incident_resolutions == [
        (incident.incident_id, RecoveryDecision.SKIP)
    ]


def test_auth_incident_repairs_then_retries_the_whole_group(recovery_ui):
    incident = _incident(auth=True)
    for number in range(1, 4):
        incident.add(
            FailureIncidentItem(
                f"auth-{number}", f"Auth Song {number}", f"https://example/{number}"
            ),
            stopped_all=number == 3,
            streak=number,
        )
    recovery_ui._run_cookie_wizard_ui = lambda: True
    recovery_ui._on_failure_incident_updated_ui(incident)

    dialog = _FakeMessageDialog.instances[0]
    assert dialog.kwargs["accept_text"]
    dialog.accept()

    assert recovery_ui._download_ctrl.incident_resolutions == [
        (incident.incident_id, RecoveryDecision.RETRY)
    ]
    assert incident.incident_id not in recovery_ui._download_failure_incidents


def test_no_result_incident_can_enter_manual_source_selection(recovery_ui):
    incident = _incident()
    incident.add(
        FailureIncidentItem(
            "one", "Song One", "ytsearch1:Artist Song One",
            artist="Artist",
        ),
        stopped_all=False,
        streak=0,
    )
    opened = []
    recovery_ui._begin_failure_source_repair = opened.append
    recovery_ui._on_failure_incident_updated_ui(incident)

    dialog = _FakeMessageDialog.instances[0]
    assert dialog.kwargs["auxiliary_text"]
    dialog.auxiliary()

    assert opened == [incident.incident_id]
    assert recovery_ui._download_ctrl.incident_resolutions == []
    assert incident.incident_id in recovery_ui._download_failure_incidents


def test_search_panel_labels_results_as_sources_during_repair(app):
    from core.playlist_parser import SourcePlatform
    from core.search_engine import ResultKind, SearchResult
    from ui.panels.search_panel import SearchPanel

    config = SimpleNamespace(
        last_search_query="",
        last_search_platform="youtube",
    )
    panel = SearchPanel(config)
    queries = []
    panel.search_requested.connect(queries.append)
    panel.begin_source_repair("Song One", 1, 3)
    panel.run_query("Artist Song One", platform="youtube", force=True)
    card = panel.add_result(SearchResult(
        result_index=1,
        title="Song One",
        artist="Artist",
        url="https://www.youtube.com/watch?v=abcdefghijk",
        platform=SourcePlatform.YOUTUBE,
        kind=ResultKind.TRACK,
        duration_sec=180,
        duration_str="3:00",
    ))

    assert queries == ["Artist Song One"]
    assert panel._source_repair_banner.isHidden() is False
    assert card._action_btn.text() != app_window_module.t("search_card_add_btn")

    panel.end_source_repair()
    assert panel._source_repair_banner.isHidden() is True
    assert card._action_btn.text() == app_window_module.t("search_card_add_btn")
    panel.deleteLater()


def test_distinct_incident_waits_without_stacking_dialogs(recovery_ui):
    first = _incident()
    first.add(FailureIncidentItem("one", "One", ""), stopped_all=False, streak=0)
    second = _incident(auth=True)
    second.add(FailureIncidentItem("two", "Two", ""), stopped_all=False, streak=1)

    recovery_ui._on_failure_incident_updated_ui(first)
    recovery_ui._on_failure_incident_updated_ui(second)
    assert len(_FakeMessageDialog.instances) == 1

    _FakeMessageDialog.instances[0].reject()
    assert len(_FakeMessageDialog.instances) == 2
    assert recovery_ui._active_failure_incident_id == second.incident_id


def test_rate_limit_dialog_keeps_exact_error_and_updates_one_countdown(recovery_ui):
    rate = {
        "epoch": 7,
        "message": "exact upstream 429 message",
        "advertised_seconds": 3600.0,
        "margin_seconds": 60.0,
    }

    recovery_ui._update_rate_limit_dialog(rate, 3660.0, 7)
    recovery_ui._update_rate_limit_dialog(rate, 3659.0, 7)

    assert len(_FakeMessageDialog.instances) == 1
    dialog = _FakeMessageDialog.instances[0]
    assert dialog.kwargs["details"] == "exact upstream 429 message"
    assert len(dialog.updates) == 1
    assert "exact upstream 429 message" == dialog.updates[0][2]


def test_only_one_recovery_dialog_is_active_and_x_opens_the_next(recovery_ui):
    first = _request("first")
    second = _request("second")

    recovery_ui._on_user_action_required_ui(first)
    recovery_ui._on_user_action_required_ui(second)
    recovery_ui._on_user_action_required_ui(second)

    assert len(_FakeMessageDialog.instances) == 1
    first_dialog = _FakeMessageDialog.instances[0]
    assert first_dialog.opened is True
    assert recovery_ui._active_download_error_request is first
    assert recovery_ui._pending_download_error_requests == [second]
    assert first.decision is None and second.decision is None

    # Closing the first question is an explicit per-song SKIP. The next
    # question must then be allowed to open; it must not be throttled away.
    first_dialog.reject()

    assert first.decision == RecoveryDecision.SKIP
    assert len(_FakeMessageDialog.instances) == 2
    second_dialog = _FakeMessageDialog.instances[1]
    assert second_dialog.opened is True
    assert recovery_ui._active_download_error_request is second
    assert recovery_ui._pending_download_error_requests == []
    assert second.decision is None

    second_dialog.reject()
    assert second.decision == RecoveryDecision.SKIP
    assert recovery_ui._active_download_error_dialog is None


def test_cancel_all_resolves_active_and_pending_questions_as_cancel(
    monkeypatch, app,
):
    _FakeMessageDialog.instances.clear()
    monkeypatch.setattr(
        app_window_module, "StyledMessageDialog", _FakeMessageDialog,
    )
    monkeypatch.setattr(
        app_window_module,
        "QTimer",
        SimpleNamespace(singleShot=lambda *_args: _args[-1]()),
    )
    window = _RecoveryWindowHarness(downloading=True)
    active = _request("active")
    pending = _request("pending")
    window._on_user_action_required_ui(active)
    window._on_user_action_required_ui(pending)

    dialog = _FakeMessageDialog.instances[0]
    window._on_cancel()

    assert active.decision == RecoveryDecision.CANCEL
    assert pending.decision == RecoveryDecision.CANCEL
    assert dialog.rejected is True
    assert window._active_download_error_dialog is None
    assert window._active_download_error_request is None
    assert window._pending_download_error_requests == []
    assert window._download_error_request_ids == set()
    assert window._download_recovery_workflow_active is False
    assert window._download_ctrl.cancel_count == 1
    assert window._match_prefetcher.cancel_count == 1
    assert window._fetch_ctrl.cancel_count == 1
    assert window._search_ctrl.cancel_count == 1
    assert window._status_bar.cancelling_count == 1


def test_dialog_open_failure_releases_request_and_does_not_poison_queue(
    recovery_ui,
):
    failed = _request("failed-open")
    following = _request("following")
    _FakeMessageDialog.fail_next_open = True

    recovery_ui._on_user_action_required_ui(failed)

    assert failed.decision == RecoveryDecision.SKIP
    assert failed.wait(0)
    assert _FakeMessageDialog.instances[0].deleted is True
    assert recovery_ui._active_download_error_dialog is None
    assert recovery_ui._active_download_error_request is None

    recovery_ui._on_user_action_required_ui(following)

    assert len(_FakeMessageDialog.instances) == 2
    assert _FakeMessageDialog.instances[1].opened is True
    assert recovery_ui._active_download_error_request is following
    assert following.decision is None

    _FakeMessageDialog.instances[1].reject()
    assert following.decision == RecoveryDecision.SKIP


def test_controller_shutdown_cancels_and_waits_for_resume_worker(
    tmp_path, monkeypatch, app,
):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    from config import AppConfig
    from core.downloader import DownloadEngine
    from ui.controllers.download_controller import DownloadController

    controller = DownloadController(AppConfig(), DownloadEngine())
    resume_worker = _RunningWorker()
    controller._dl_worker = None
    controller._resume_workers = [resume_worker]
    try:
        assert controller.request_shutdown() is False
        assert resume_worker.cancel_count == 1

        resume_worker.running = False
        assert controller.request_shutdown() is True
        assert resume_worker.cancel_count == 1
    finally:
        controller.deleteLater()
