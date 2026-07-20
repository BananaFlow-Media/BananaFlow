"""Focused B5 lifecycle regressions for ReplayGain-aware application close."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from core.metadata_models import AudioTrackItem, OriginalTags, REPLAYGAIN_TRACK_GAIN
from ui.app_window import AppWindow
from ui.controllers.metadata_controller import MetadataController
from ui.workers.metadata_worker import ReplayGainAnalysisWorker


class _ControlledWorker(QObject):
    finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.running = True
        self.cancel_count = 0

    def isRunning(self):
        return self.running

    def cancel(self):
        self.cancel_count += 1

    def complete(self):
        self.running = False
        self.finished.emit({})


class _CloseEvent:
    def __init__(self):
        self.accepted = 0
        self.ignored = 0

    def accept(self):
        self.accepted += 1

    def ignore(self):
        self.ignored += 1


class _WindowHarness:
    def __init__(self, controller):
        self._cfg = SimpleNamespace(tray_on_close=False)
        self._tray = None
        self._metadata_ctrl = controller
        self._metadata_shutdown_wired = False
        self.close_count = 0

    def close(self):
        self.close_count += 1

    def _on_metadata_shutdown_timeout(self):
        pass


def _app():
    return QApplication.instance() or QApplication([])


def test_appwindow_replaygain_only_close_defers_cancels_and_is_idempotent():
    _app()
    controller = MetadataController()
    worker = _ControlledWorker()
    controller._replaygain_worker = worker
    harness = _WindowHarness(controller)
    first, second = _CloseEvent(), _CloseEvent()
    try:
        AppWindow.closeEvent(harness, first)
        AppWindow.closeEvent(harness, second)
        assert first.ignored == second.ignored == 1
        assert worker.cancel_count == 1
        assert harness._metadata_shutdown_wired is True

        worker.complete()
        assert harness.close_count == 1
        assert controller.has_active_shutdown_work() is False
    finally:
        controller.deleteLater()
        worker.deleteLater()


def test_shutdown_waits_for_apply_and_replaygain_without_double_destroy():
    _app()
    controller = MetadataController()
    apply_worker, replaygain_worker = _ControlledWorker(), _ControlledWorker()
    controller._apply_worker = apply_worker
    controller._replaygain_worker = replaygain_worker
    ready = []
    controller.shutdown_ready.connect(lambda: ready.append(True))
    try:
        assert controller.request_shutdown() is False
        assert (apply_worker.cancel_count, replaygain_worker.cancel_count) == (1, 1)
        apply_worker.complete()
        assert ready == []
        replaygain_worker.complete()
        assert ready == [True]
        assert controller.request_shutdown() is True
    finally:
        controller.deleteLater()
        apply_worker.deleteLater()
        replaygain_worker.deleteLater()


def test_replaygain_stale_result_and_success_are_rejected_after_close(tmp_path, monkeypatch):
    _app()
    path = tmp_path / "shutdown.mp3"
    path.write_bytes(b"fixture")
    item = AudioTrackItem(
        path=path, folder=tmp_path, ext=".mp3", format_id="mp3",
        metadata_editable=True, original=OriginalTags(),
    )
    controller = MetadataController()
    controller.workspace_state.set_tracks([item])
    controller.workspace_state.set_selected_items([item])
    monkeypatch.setattr(ReplayGainAnalysisWorker, "start", lambda self: None)
    controller.analyze_replaygain_tracks([item])
    worker = controller._replaygain_worker
    monkeypatch.setattr(worker, "isRunning", lambda: True)
    proposals, completions = [], []
    controller.replaygain_proposal_ready.connect(lambda *_args: proposals.append(True))
    controller.replaygain_analysis_complete.connect(completions.append)
    try:
        assert controller.request_shutdown() is False
        worker.result_ready.emit(item, {REPLAYGAIN_TRACK_GAIN: -3.0})
        worker.progress.emit(1, 1)
        worker.finished.emit({
            "operation_id": "op", "mode": "track", "total": 1,
            "completed": 1, "failed": 0, "cancelled": True,
        })
        assert item.proposed.replay_gain_changes == {}
        assert proposals == [] and completions == []
    finally:
        controller.deleteLater()
        worker.deleteLater()


def test_appwindow_idle_close_remains_immediate(monkeypatch):
    _app()
    controller = MetadataController()
    harness = _WindowHarness(controller)
    harness._save_state = lambda: None
    harness._save_queue_state = lambda: None
    harness._clipboard_worker = None
    harness._download_ctrl = SimpleNamespace(_dl_worker=None)
    harness._fetch_ctrl = SimpleNamespace(_fetch_worker=None, _scraper_worker=None)
    harness._search_ctrl = SimpleNamespace(_search_worker=None)
    monkeypatch.setitem(sys.modules, "keyboard", SimpleNamespace(unhook_all=lambda: None))
    event = _CloseEvent()
    try:
        AppWindow.closeEvent(harness, event)
        assert event.accepted == 1 and event.ignored == 0
    finally:
        controller.deleteLater()
