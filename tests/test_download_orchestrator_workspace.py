"""
tests/test_download_orchestrator_workspace.py  –  Batch workspace lifecycle
================================================================================
DownloadOrchestrator.run_batch() creates one hidden batch workspace per
batch and assigns it to every job's DownloadRequest.workspace_dir before
any engine work starts (core.downloader then writes there and atomically
publishes into the real output_dir — see test_downloader_publish.py).

Covers the workspace's LIFECYCLE rules specifically:
  * every real job gets workspace_dir set, pointed at a real hidden dir
    under its output_dir.
  * a clean (non-cancelled, no per-track pause) batch finish cleans the
    workspace up.
  * a whole-batch cancel (pause or a real cancel — the orchestrator can't
    tell them apart) preserves the workspace.
  * a per-track pause (cancel_track on ONE job while the rest of the batch
    completes normally) ALSO preserves the workspace — this is the subtle
    case: was_cancelled alone is False here (the engine-level cancel event
    is never set), so cleanup must additionally check per-job cancel
    events, or a paused track's .part file gets deleted out from under it
    the moment its siblings finish.
  * a job whose request already carries a workspace_dir (a paused-track
    resume) keeps that exact same value — run_batch() must never hand it
    a fresh, unrelated workspace, or its .part file becomes unreachable.
  * duplicate-skip ("preexisting") jobs never touch workspace machinery at
    all (they never call the engine).

Offline — FakeEngine stands in for DownloadEngine, no yt-dlp, no network.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus, MediaType
from core.download_orchestrator import DownloadOrchestrator


class FakeEngine:
    """Records the DownloadRequest of every job it was asked to download,
    then reports immediate success."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self.seen_requests: list[DownloadRequest] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        self.seen_requests.append(req)
        if self._cancel_event.is_set() or (req.cancel_event and req.cancel_event.is_set()):
            return
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED, url=req.url,
                output_path=f"{req.workspace_dir or req.output_dir}/out.mp3", fraction=1.0,
            ))


class BlockingEngine:
    """Blocks each job on an Event so the test can cancel/pause mid-run."""

    def __init__(self) -> None:
        self._cancel_event = threading.Event()
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen_requests: list[DownloadRequest] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, req: DownloadRequest) -> None:
        self.seen_requests.append(req)
        self.started.set()
        self.release.wait(5.0)
        if self._cancel_event.is_set() or (req.cancel_event and req.cancel_event.is_set()):
            return
        if req.on_finished:
            req.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED, url=req.url,
                output_path=f"{req.workspace_dir or req.output_dir}/out.mp3", fraction=1.0,
            ))


class NullCallbacks:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _job(key: str, url: str, output_dir: str, workspace_dir: str = None):
    return (key, DownloadRequest(
        url=url, output_dir=output_dir, media_type=MediaType.AUDIO,
        workspace_dir=workspace_dir,
    ))


# ── Workspace assignment ─────────────────────────────────────────────────────

class TestWorkspaceAssignment:
    def test_every_job_gets_a_workspace_dir(self, tmp_path):
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=2)

        orch.run_batch([
            _job("a", "http://a", str(tmp_path)),
            _job("b", "http://b", str(tmp_path)),
        ])

        assert len(engine.seen_requests) == 2
        for req in engine.seen_requests:
            assert req.workspace_dir
            assert tmp_path.resolve() in Path(req.workspace_dir).resolve().parents

    def test_all_jobs_in_one_batch_share_the_same_workspace(self, tmp_path):
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=2)

        orch.run_batch([
            _job("a", "http://a", str(tmp_path)),
            _job("b", "http://b", str(tmp_path)),
        ])

        workspaces = {req.workspace_dir for req in engine.seen_requests}
        assert len(workspaces) == 1

    def test_preexisting_jobs_never_get_a_workspace(self, tmp_path):
        """Duplicate-skip jobs never call engine.download() at all — there
        is nothing for them to write, so no workspace should even be
        created when a batch is ENTIRELY duplicate-skips."""
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks())

        orch.run_batch([], preexisting=[("skip1", str(tmp_path / "existing.mp3"))])

        assert engine.seen_requests == []
        assert not (tmp_path / ".bananaflow_tmp").exists()

    def test_preset_workspace_dir_is_not_overridden(self, tmp_path):
        """A resumed job (DownloadController.pause_track carries the
        original workspace_dir forward) must keep using that SAME
        workspace, not get handed a fresh one that has no .part file in
        it."""
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks())

        original_workspace = tmp_path / ".bananaflow_tmp" / "batch-original"
        original_workspace.mkdir(parents=True)
        req = DownloadRequest(
            url="http://a", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
            workspace_dir=str(original_workspace),
        )

        orch.run_batch([("a", req)])

        assert engine.seen_requests[0].workspace_dir == str(original_workspace)
        # No fresh sibling workspace was created alongside it.
        siblings = list((tmp_path / ".bananaflow_tmp").iterdir())
        assert siblings == [original_workspace]


# ── Cleanup rules ─────────────────────────────────────────────────────────────

class TestWorkspaceCleanup:
    def test_clean_batch_finish_removes_the_workspace(self, tmp_path):
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=2)

        orch.run_batch([
            _job("a", "http://a", str(tmp_path)),
            _job("b", "http://b", str(tmp_path)),
        ])

        workspace = engine.seen_requests[0].workspace_dir
        assert not Path(workspace).exists()

    def test_whole_batch_cancel_preserves_the_workspace(self, tmp_path):
        """A mid-run cancel (which might really be a global pause — the
        orchestrator can't tell) must leave the workspace and any .part
        files in it untouched."""
        engine = BlockingEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=1)
        result_box = {}
        done = threading.Event()

        def run():
            try:
                result_box["result"] = orch.run_batch([
                    _job("a", "http://a", str(tmp_path)),
                    _job("b", "http://b", str(tmp_path)),
                ])
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert engine.started.wait(2.0)
        orch.cancel()
        engine.release.set()
        assert done.wait(15.0)
        thread.join(1.0)

        assert result_box["result"].cancelled is True
        workspace = engine.seen_requests[0].workspace_dir
        assert Path(workspace).exists(), (
            "cancelling the whole batch must preserve the workspace — it "
            "might be a pause, not a real cancel"
        )

    def test_per_track_pause_preserves_the_workspace(self, tmp_path):
        """The subtle case: pausing ONE track (cancel_track) while its
        siblings finish normally does NOT set the engine-level cancel
        event, so was_cancelled alone is False for this batch — cleanup
        must still be skipped, or the paused track's .part file gets
        deleted the instant the rest of the batch completes."""
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=1)

        job_a = _job("a", "http://a", str(tmp_path))
        job_b = _job("b", "http://b", str(tmp_path))

        # Pause job "a" the instant it's submitted, before the pool can run
        # it, by hooking into FakeEngine.download via a wrapper that cancels
        # the per-track event on the FIRST call only.
        original_download = engine.download
        calls = {"n": 0}

        def _download_and_maybe_pause(req):
            calls["n"] += 1
            if calls["n"] == 1 and req.cancel_event is not None:
                req.cancel_event.set()  # simulates pause_track()'s cancel_track(key)
            return original_download(req)

        engine.download = _download_and_maybe_pause

        orch.run_batch([job_a, job_b])

        workspace = engine.seen_requests[0].workspace_dir
        assert Path(workspace).exists(), (
            "a per-track pause must preserve the shared batch workspace "
            "even though the rest of the batch completed normally"
        )

    def test_batch_with_only_failures_still_cleans_up(self, tmp_path):
        """A real failure (not a pause) is not resumable — its workspace
        leftovers should NOT block normal cleanup."""
        class FailingEngine(FakeEngine):
            def download(self, req):
                self.seen_requests.append(req)
                if req.on_error:
                    req.on_error(DownloadProgress(
                        status=DownloadStatus.ERROR, url=req.url,
                        error_message="boom",
                    ))

        engine = FailingEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks())

        orch.run_batch([_job("a", "http://a", str(tmp_path))])

        workspace = engine.seen_requests[0].workspace_dir
        assert not Path(workspace).exists()
