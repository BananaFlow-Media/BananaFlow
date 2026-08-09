"""
tests/test_cancel_all_discards_paused.py  –  Cancel All abandons paused work
=============================================================================
The pause/cancel split at the controller level (issue #61).

Cancel All means "throw this batch away". It used to leave ``_paused_requests``
untouched, so a track the user had just cancelled was still offered as
resumable — and after a Global Pause it left the whole paused set behind
completely, because by then no worker is running for cancel_all() to reach.

Global Pause is the opposite and must keep every snapshot exactly as it is.

Headless (QT_QPA_PLATFORM=offscreen); skipped when PySide6 is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


class _FakeCard:
    def __init__(self, title: str = "Track") -> None:
        self.title = title
        self.artist = "Artist"
        self.track_url = "https://www.youtube.com/watch?v=abc"
        self.status = "downloading"
        self.status_calls: list[str] = []

    def set_status(self, status: str) -> None:
        self.status = status
        self.status_calls.append(status)

    def get_status(self) -> str:
        return self.status


class _FakeOrchestrator:
    """Just the two entry points the controller uses for the pause/cancel
    boundary. Every job is treated as live and pausable; the orchestrator's
    own race-closing logic is test_orchestrator.py's job."""

    def __init__(self, jobs) -> None:
        self._jobs = dict(jobs)
        self.cancelled_paused: list[str] = []

    def live_request_snapshot(self, key: str):
        import dataclasses

        req = self._jobs.get(key)
        if req is None:
            return None, None
        return None, dataclasses.replace(req)

    def cancel_paused_job(self, key: str) -> bool:
        self.cancelled_paused.append(key)
        return True


class _FakeWorker:
    def __init__(self, jobs, running: bool = True) -> None:
        self._jobs = jobs
        self._orch = _FakeOrchestrator(jobs)
        self._running = running
        self.cancelled_keys: list[str] = []
        self.cancelled = False

    def isRunning(self) -> bool:  # noqa: N802 — Qt's spelling
        return self._running

    def cancel(self) -> None:
        self.cancelled = True

    def cancel_track(self, key: str) -> None:
        self.cancelled_keys.append(key)


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


def _request(tmp_path, workspace: str):
    from core.downloader import DownloadRequest, MediaType

    return DownloadRequest(
        url="https://www.youtube.com/watch?v=abc",
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
        workspace_dir=workspace,
    )


@pytest.fixture
def removed(monkeypatch):
    """Capture workspace removals instead of touching the filesystem —
    remove_workspace_tree's own path-ownership rules are utils/paths' tests."""
    calls: list[Path] = []
    import utils.paths

    monkeypatch.setattr(utils.paths, "remove_workspace_tree", calls.append)
    return calls


def test_cancel_all_after_a_global_pause_discards_every_snapshot(
    tmp_path, monkeypatch, app, removed,
):
    """The gap this closes: the batch worker is already gone, so cancel_all()
    had nothing to reach and the paused set survived untouched."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))
    workspace = str(tmp_path / ".bananaflow_tmp" / "batch-1" / key)

    card.set_status("paused")
    ctrl._paused_requests[key] = _request(tmp_path, workspace)
    ctrl._key_to_card[key] = card
    ctrl._persist_paused_state()
    assert ctrl._paused_store.load(), "precondition: the pause was persisted"

    ctrl.cancel_all()

    assert ctrl._paused_requests == {}
    assert card.get_status() == "cancelled"
    assert ctrl._paused_store.load() == [], "the persisted record must go too"
    assert removed == [Path(workspace).parent], (
        "nothing else will ever clean up after an abandoned paused job"
    )


def test_cancel_all_while_the_batch_runs_tells_the_orchestrator(
    tmp_path, monkeypatch, app, removed,
):
    """A track paused individually while its siblings kept downloading. The
    orchestrator that owns it is still alive, so the cancellation has to reach
    the batch aggregator — and the workspace is left to _cleanup_cancelled_batch,
    which runs once the worker has actually finished winding down."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))
    workspace = str(tmp_path / ".bananaflow_tmp" / "batch-1" / key)
    req = _request(tmp_path, workspace)

    worker = _FakeWorker(jobs=[(key, req)], running=True)
    ctrl._dl_worker = worker
    card.set_status("paused")
    ctrl._paused_requests[key] = req
    ctrl._key_to_card[key] = card

    ctrl.cancel_all()

    assert worker._orch.cancelled_paused == [key]
    assert ctrl._paused_requests == {}
    assert card.get_status() == "cancelled"
    assert worker.cancelled, "the running worker is still cancelled as before"
    assert removed == [], (
        "a .part file must not be deleted out from under a worker that is "
        "still shutting down"
    )


def test_global_pause_keeps_its_snapshots(tmp_path, monkeypatch, app, removed):
    """The other half of the split: Pause All captures and KEEPS."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))
    workspace = str(tmp_path / ".bananaflow_tmp" / "batch-1" / key)
    req = _request(tmp_path, workspace)

    worker = _FakeWorker(jobs=[(key, req)], running=True)
    ctrl._dl_worker = worker
    ctrl._key_to_card[key] = card

    ctrl.global_pause()

    assert key in ctrl._paused_requests
    assert card.get_status() == "paused"
    assert worker._orch.cancelled_paused == []
    assert removed == []
    assert ctrl._paused_store.load(), "a paused batch survives a restart"


def test_cancel_all_with_nothing_paused_is_unchanged(
    tmp_path, monkeypatch, app, removed,
):
    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))
    worker = _FakeWorker(jobs=[(key, _request(tmp_path, ""))], running=True)
    ctrl._dl_worker = worker

    ctrl.cancel_all()

    assert worker.cancelled
    assert worker._orch.cancelled_paused == []
    assert removed == []


def test_pause_then_cancel_all_leaves_nothing_to_resume(
    tmp_path, monkeypatch, app, removed,
):
    """End to end through the controller's own API: pause a track, cancel the
    batch, and Resume All must have nothing left to start."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard()
    key = str(id(card))
    workspace = str(tmp_path / ".bananaflow_tmp" / "batch-1" / key)
    req = _request(tmp_path, workspace)

    worker = _FakeWorker(jobs=[(key, req)], running=True)
    ctrl._dl_worker = worker
    ctrl._key_to_card[key] = card

    assert ctrl.pause_track(card) is True
    assert card.get_status() == "paused"

    ctrl.cancel_all()

    assert ctrl._paused_requests == {}
    assert card.get_status() == "cancelled"

    started = []
    monkeypatch.setattr(ctrl, "_build_batch_worker", lambda *a, **k: started.append(a))
    ctrl.resume_all()
    assert started == [], "a cancelled batch has nothing to resume"
