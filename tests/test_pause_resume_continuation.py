"""
tests/test_pause_resume_continuation.py  –  True global pause/resume
========================================================================
Global pause used to snapshot nothing, and "Resume All" re-entered
_on_download()/start_batch() — a genuinely NEW batch that re-ran the
duplicate policy and discarded every job's partial download and workspace.

These tests pin the redesigned behaviour:
  * global_pause snapshots every UNFINISHED job (full fidelity, resumable,
    workspace preserved) and leaves completed/duplicate-skip jobs alone.
  * resume_all continues exactly those jobs as one batch, never rebuilding
    from cards and never invoking duplicate detection or the dialog.
  * repeated pause/resume cycles preserve the workspace and job identity.

Headless (offscreen Qt); skipped without PySide6. DownloadWorker is
monkeypatched so no real QThread/network starts.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from core.downloader import DownloadRequest, MediaType


# ── Fakes ──────────────────────────────────────────────────────────────────

class _FakeCard:
    def __init__(self, title: str, status: str = "downloading") -> None:
        self.title = title
        self.artist = "Artist"
        self.track_url = f"https://youtu.be/{title}"
        self._status = status
        self.status_calls: list[str] = []

    def get_status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        self._status = status
        self.status_calls.append(status)

    def set_progress(self, fraction: float) -> None:
        pass


class _FakeLiveWorker:
    """Stands in for a running batch DownloadWorker for global_pause."""

    def __init__(self, jobs) -> None:
        self._jobs = jobs
        self.cancelled = False

    def isRunning(self) -> bool:
        return True

    def cancel(self) -> None:
        self.cancelled = True


class _FakeSignal:
    def connect(self, *_a, **_k) -> None:
        pass


class _FakeDownloadWorker:
    last_instance = None

    def __init__(self, jobs, engine, config, db=None, max_workers=3,
                 preexisting=None, parent=None) -> None:
        self.jobs = jobs
        self.preexisting = preexisting or []
        self.started = False
        for name in (
            "track_progress", "track_speed", "track_status", "track_finished",
            "track_preexisting", "overall_progress", "metrics", "batch_snapshot",
            "job_count_changed", "job_error", "all_finished", "track_thumbnail",
        ):
            setattr(self, name, _FakeSignal())
        _FakeDownloadWorker.last_instance = self

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return self.started


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _controller(tmp_path, monkeypatch, app):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    from config import AppConfig
    from core.downloader import DownloadEngine
    from ui.controllers.download_controller import DownloadController
    return DownloadController(AppConfig(), DownloadEngine())


def _req(title: str, workspace: str = "/ws", **kw) -> DownloadRequest:
    return DownloadRequest(
        url=f"https://youtu.be/{title}", output_dir="/out",
        media_type=MediaType.AUDIO, forced_title=title, workspace_dir=workspace, **kw,
    )


# ── global_pause ─────────────────────────────────────────────────────────────

class TestGlobalPause:
    def test_snapshots_every_unfinished_job(self, tmp_path, monkeypatch, app):
        ctrl = _controller(tmp_path, monkeypatch, app)
        card_a = _FakeCard("A", status="downloading")
        card_b = _FakeCard("B", status="queued")
        ka, kb = str(id(card_a)), str(id(card_b))
        ctrl._key_to_card = {ka: card_a, kb: card_b}
        ctrl._dl_worker = _FakeLiveWorker([(ka, _req("A")), (kb, _req("B"))])

        ctrl.global_pause()

        assert set(ctrl._paused_requests) == {ka, kb}
        assert all(r.resumable for r in ctrl._paused_requests.values())
        assert card_a.get_status() == "paused"
        assert card_b.get_status() == "paused"
        assert ctrl._dl_worker.cancelled is True

    def test_does_not_capture_completed_or_preexisting_jobs(self, tmp_path, monkeypatch, app):
        ctrl = _controller(tmp_path, monkeypatch, app)
        done = _FakeCard("done", status="done")       # completed or duplicate-skip
        live = _FakeCard("live", status="downloading")
        kd, kl = str(id(done)), str(id(live))
        ctrl._key_to_card = {kd: done, kl: live}
        ctrl._dl_worker = _FakeLiveWorker([(kd, _req("done")), (kl, _req("live"))])

        ctrl.global_pause()

        assert kl in ctrl._paused_requests
        assert kd not in ctrl._paused_requests, "a completed job must never be re-run"
        assert done.get_status() == "done"  # untouched

    def test_snapshot_preserves_every_field(self, tmp_path, monkeypatch, app):
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A")
        k = str(id(card))
        live = _req(
            "A", workspace="/ws/batch/x/keyA", thumbnail_url="http://thumb",
            forced_album="My Album", category="stream:hls", is_solo=True,
            cookies_browser="chrome", square_thumbnails=True,
        )
        ctrl._key_to_card = {k: card}
        ctrl._dl_worker = _FakeLiveWorker([(k, live)])

        ctrl.global_pause()

        saved = ctrl._paused_requests[k]
        assert saved.workspace_dir == "/ws/batch/x/keyA"
        assert saved.thumbnail_url == "http://thumb"
        assert saved.forced_album == "My Album"
        assert saved.category == "stream:hls"
        assert saved.is_solo is True
        assert saved.cookies_browser == "chrome"
        assert saved.square_thumbnails is True
        assert saved.resumable is True

    def test_no_running_worker_is_a_noop(self, tmp_path, monkeypatch, app):
        ctrl = _controller(tmp_path, monkeypatch, app)
        ctrl._dl_worker = None
        ctrl.global_pause()  # must not raise
        assert ctrl._paused_requests == {}


# ── resume_all ───────────────────────────────────────────────────────────────

class TestResumeAll:
    def test_resumes_the_snapshotted_jobs_as_one_batch(self, tmp_path, monkeypatch, app):
        import ui.workers.download_worker as dw_mod
        monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeDownloadWorker)

        ctrl = _controller(tmp_path, monkeypatch, app)
        card_a, card_b = _FakeCard("A"), _FakeCard("B")
        ka, kb = str(id(card_a)), str(id(card_b))
        ctrl._key_to_card = {ka: card_a, kb: card_b}
        ctrl._paused_requests = {ka: _req("A"), kb: _req("B")}
        ctrl._dl_worker = None

        ctrl.resume_all()

        worker = _FakeDownloadWorker.last_instance
        assert worker is not None
        assert worker.started is True
        assert {k for k, _ in worker.jobs} == {ka, kb}
        assert worker.preexisting == []  # a resume never has duplicate-skips
        assert ctrl._paused_requests == {}  # consumed
        assert ctrl._dl_worker is worker

    def test_resume_never_calls_duplicate_detection(self, tmp_path, monkeypatch, app):
        import ui.workers.download_worker as dw_mod
        monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeDownloadWorker)

        def _boom(**kwargs):
            raise AssertionError("resume must never run duplicate detection")

        monkeypatch.setattr("core.duplicate_checker.find_duplicate", _boom)

        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A")
        k = str(id(card))
        ctrl._key_to_card = {k: card}
        ctrl._paused_requests = {k: _req("A")}
        ctrl._dl_worker = None

        ctrl.resume_all()  # would raise if it touched find_duplicate

        assert _FakeDownloadWorker.last_instance.started is True

    def test_resume_preserves_workspace_dir(self, tmp_path, monkeypatch, app):
        import ui.workers.download_worker as dw_mod
        monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeDownloadWorker)

        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A")
        k = str(id(card))
        ctrl._key_to_card = {k: card}
        ctrl._paused_requests = {k: _req("A", workspace="/ws/batch-x/keyA")}
        ctrl._dl_worker = None

        ctrl.resume_all()

        job_req = dict(_FakeDownloadWorker.last_instance.jobs)[k]
        assert job_req.workspace_dir == "/ws/batch-x/keyA"

    def test_resume_all_with_nothing_paused_is_noop(self, tmp_path, monkeypatch, app):
        import ui.workers.download_worker as dw_mod
        _FakeDownloadWorker.last_instance = None
        monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeDownloadWorker)

        ctrl = _controller(tmp_path, monkeypatch, app)
        ctrl._paused_requests = {}
        ctrl._dl_worker = None

        ctrl.resume_all()

        assert _FakeDownloadWorker.last_instance is None  # no worker built

    def test_pause_then_resume_cycle_keeps_workspace_and_identity(self, tmp_path, monkeypatch, app):
        import ui.workers.download_worker as dw_mod
        monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeDownloadWorker)

        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="downloading")
        k = str(id(card))
        ctrl._key_to_card = {k: card}
        ctrl._dl_worker = _FakeLiveWorker([(k, _req("A", workspace="/ws/batch-1/keyA"))])

        # Pause → capture → resume.
        ctrl.global_pause()
        ctrl._dl_worker = None  # the paused worker has stopped
        ctrl.resume_all()

        resumed = dict(_FakeDownloadWorker.last_instance.jobs)[k]
        assert resumed.workspace_dir == "/ws/batch-1/keyA"
        assert resumed.resumable is True
        # Same job identity (key) throughout.
        assert list(dict(_FakeDownloadWorker.last_instance.jobs).keys()) == [k]


# ── AppWindow wiring ─────────────────────────────────────────────────────────

def test_resume_button_routes_to_resume_all_not_download(tmp_path, monkeypatch, app):
    """The Resume-All button must continue the batch (resume_all), never
    start a fresh one (_on_download / start_batch)."""
    import ui.controllers.download_controller as ctrl_mod

    calls = []

    class _StubCtrl:
        def global_pause(self):
            calls.append("global_pause")

        def resume_all(self):
            calls.append("resume_all")

    class _StubQueuePanel:
        def set_pause_resume_state(self, _v):
            pass

        def get_all_cards(self):
            return []

    # Drive AppWindow._on_global_pause_resume in isolation via a tiny shim
    # that reproduces its exact body against stubs.
    from ui.app_window import AppWindow

    shim = AppWindow.__new__(AppWindow)
    shim._download_ctrl = _StubCtrl()
    shim._queue_panel = _StubQueuePanel()

    class _StubStatusBar:
        def show_paused(self):
            pass

    shim._status_bar = _StubStatusBar()

    def _on_download_should_not_run():
        calls.append("_on_download")

    shim._on_download = _on_download_should_not_run

    AppWindow._on_global_pause_resume(shim, pause=False)

    assert "resume_all" in calls
    assert "_on_download" not in calls
