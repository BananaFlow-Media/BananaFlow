"""
tests/test_cancellation_cleanup.py  –  Cancel is not pause
=============================================================
Cancellation must be fundamentally different from pause:

  * Cancel removes the batch's abandoned partial/intermediate work
    (its hidden workspace); pause preserves it for resume.
  * Already-published final files in the output directory are never
    touched by a cancel.
  * Jobs that never started leave no files.
  * A cancel is scoped to its own batch — a concurrent/unrelated batch's
    workspace is never removed.
  * A cancelled (aborted) download never publishes / never reports done.

Controller-level tests drive DownloadController._on_batch_done via a real
signal so the sender()-based job capture is exercised; orchestrator-level
tests use a fake engine that honours the cancel event.
"""

from __future__ import annotations

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)

from pathlib import Path

from core.batch_outcome import BatchOutcome
from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType


# ── Controller-level: cancel cleans workspace, keeps published files ─────────

class _FakeFinishedWorker(QObject):
    all_finished = Signal(object)

    def __init__(self, jobs) -> None:
        super().__init__()
        self._jobs = jobs

    def isRunning(self) -> bool:
        return False


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


def _batch_with_workspace(tmp_path):
    """Create a real on-disk batch workspace with two per-job subdirs and a
    published final file already in the output dir. Returns (jobs, container,
    published_file)."""
    from utils.paths import make_batch_workspace

    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True)
    container = make_batch_workspace(str(output_dir))  # out/.bananaflow_tmp/batch-<id>

    published = output_dir / "already_done.mp3"
    published.write_bytes(b"COMPLETE-AND-PUBLISHED")

    jobs = []
    for key in ("jobA", "jobB"):
        sub = container / key
        sub.mkdir()
        (sub / "song.part").write_bytes(b"partial")
        req = DownloadRequest(
            url=f"http://{key}", output_dir=str(output_dir),
            media_type=MediaType.AUDIO, workspace_dir=str(sub),
        )
        jobs.append((key, req))
    return jobs, container, published


def test_cancel_removes_workspace_but_keeps_published_file(tmp_path, monkeypatch, app):
    ctrl = _controller(tmp_path, monkeypatch, app)
    jobs, container, published = _batch_with_workspace(tmp_path)

    worker = _FakeFinishedWorker(jobs)
    worker.all_finished.connect(ctrl._on_batch_done)
    ctrl._dl_worker = worker
    ctrl._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)

    worker.all_finished.emit(BatchOutcome.CANCELLED_BY_USER)

    assert not container.exists(), "cancel must remove the abandoned workspace"
    assert published.exists(), "an already-published final file must survive a cancel"
    assert published.read_bytes() == b"COMPLETE-AND-PUBLISHED"


def test_cancel_drops_paused_snapshots_for_the_batch(tmp_path, monkeypatch, app):
    ctrl = _controller(tmp_path, monkeypatch, app)
    jobs, container, _ = _batch_with_workspace(tmp_path)
    # Pretend one job had been per-track paused before the whole-batch cancel.
    ctrl._paused_requests[jobs[0][0]] = jobs[0][1]

    worker = _FakeFinishedWorker(jobs)
    worker.all_finished.connect(ctrl._on_batch_done)
    ctrl._dl_worker = worker
    ctrl._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)

    worker.all_finished.emit(BatchOutcome.CANCELLED_BY_USER)

    assert ctrl._paused_requests == {}, "cancel abandons everything, incl. paused snapshots"


def test_pause_does_NOT_remove_the_workspace(tmp_path, monkeypatch, app):
    """The defining difference: the very same finish flow, but with a PAUSE
    outcome, must leave the workspace intact."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    jobs, container, _ = _batch_with_workspace(tmp_path)

    worker = _FakeFinishedWorker(jobs)
    worker.all_finished.connect(ctrl._on_batch_done)
    ctrl._dl_worker = worker
    ctrl._set_termination_intent(BatchOutcome.PAUSED_BY_USER)

    worker.all_finished.emit(BatchOutcome.PAUSED_BY_USER)

    assert container.exists(), "pause must preserve the workspace for resume"


def test_cancel_is_scoped_to_its_own_batch(tmp_path, monkeypatch, app):
    """Cancelling batch A must not remove an unrelated batch B's workspace."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    jobs_a, container_a, _ = _batch_with_workspace(tmp_path / "a")
    jobs_b, container_b, _ = _batch_with_workspace(tmp_path / "b")

    worker = _FakeFinishedWorker(jobs_a)
    worker.all_finished.connect(ctrl._on_batch_done)
    ctrl._dl_worker = worker
    ctrl._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)

    worker.all_finished.emit(BatchOutcome.CANCELLED_BY_USER)

    assert not container_a.exists()
    assert container_b.exists(), "an unrelated batch's workspace must be untouched"


def test_cancel_before_start_leaves_no_files(tmp_path, monkeypatch, app):
    """Jobs that never started: their per-job subdirs are empty; cancel
    removes the whole container, leaving nothing behind."""
    ctrl = _controller(tmp_path, monkeypatch, app)
    from utils.paths import make_batch_workspace

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    container = make_batch_workspace(str(output_dir))
    jobs = []
    for key in ("j1", "j2"):
        sub = container / key
        sub.mkdir()  # created but empty — download never ran
        jobs.append((key, DownloadRequest(
            url=f"http://{key}", output_dir=str(output_dir),
            media_type=MediaType.AUDIO, workspace_dir=str(sub),
        )))

    worker = _FakeFinishedWorker(jobs)
    worker.all_finished.connect(ctrl._on_batch_done)
    ctrl._dl_worker = worker
    ctrl._set_termination_intent(BatchOutcome.CANCELLED_BY_USER)
    worker.all_finished.emit(BatchOutcome.CANCELLED_BY_USER)

    assert not container.exists()
    assert list(output_dir.iterdir()) == []  # nothing left in the output dir


# ── Orchestrator-level: a cancelled job never publishes ──────────────────────

class _CancellingEngine:
    """Honours the per-request cancel event: a job whose event is set aborts
    without ever calling on_finished (mirrors the yt-dlp abort hook)."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self.published: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        if self._cancel_event.is_set() or (req.cancel_event and req.cancel_event.is_set()):
            # Aborted before any publish — this is the cancelled path.
            return
        self.published.append(req.url)
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED, url=req.url, output_path="/out/x.mp3",
                fraction=1.0,
            ))


class _NullCallbacks:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def test_precancelled_job_never_publishes(tmp_path, app):
    from core.download_orchestrator import DownloadOrchestrator

    engine = _CancellingEngine()
    engine._cancel_event.set()  # cancel before start
    orch = DownloadOrchestrator(engine=engine, callbacks=_NullCallbacks())

    req = DownloadRequest(url="http://a", output_dir=str(tmp_path), media_type=MediaType.AUDIO)
    result = orch.run_batch([("a", req)])

    assert engine.published == []
    assert result.cancelled is True
