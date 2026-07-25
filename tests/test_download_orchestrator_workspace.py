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

    def test_each_job_gets_its_own_subdir_under_one_shared_container(self, tmp_path):
        """Per-job isolation: two jobs in one batch get DISTINCT workspace
        subdirs (so identical final filenames can't collide in temporary
        storage) but share ONE batch container."""
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=2)

        orch.run_batch([
            _job("a", "http://a", str(tmp_path)),
            _job("b", "http://b", str(tmp_path)),
        ])

        workspaces = {req.workspace_dir for req in engine.seen_requests}
        assert len(workspaces) == 2  # distinct per-job subdirs
        containers = {Path(w).parent for w in workspaces}
        assert len(containers) == 1  # under one shared batch container

    def test_parallel_identical_filenames_do_not_collide_in_temp_storage(self, tmp_path):
        """The concrete collision case: two jobs with the same title and the
        same playlist_name would write the same relative path — they must
        still land in separate temp subdirs."""
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=2)

        req_a = DownloadRequest(
            url="http://a", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
            forced_title="Same Title", playlist_name="Album",
        )
        req_b = DownloadRequest(
            url="http://b", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
            forced_title="Same Title", playlist_name="Album",
        )
        orch.run_batch([("a", req_a), ("b", req_b)])

        assert req_a.workspace_dir != req_b.workspace_dir

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
        it. The workspace it saw is exactly the preset one."""
        # Use a non-completing engine so the (successful) per-job cleanup
        # doesn't remove the workspace before we can assert on it — here we
        # only care that run_batch did not OVERRIDE the preset value.
        engine = FakeEngine()
        seen_workspace = {}

        original_workspace = tmp_path / ".bananaflow_tmp" / "batch-original" / "job-a"
        original_workspace.mkdir(parents=True)

        original_download = engine.download

        def _capture(req):
            seen_workspace["value"] = req.workspace_dir
            # Do not complete — leave a "cancelled" per-track event so no
            # cleanup runs and the preset dir survives for the assertion.
            if req.cancel_event is not None:
                req.cancel_event.set()
            return original_download(req)

        engine.download = _capture

        req = DownloadRequest(
            url="http://a", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
            workspace_dir=str(original_workspace),
        )
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks())
        orch.run_batch([("a", req)])

        # The engine saw the preset workspace, unchanged.
        assert seen_workspace["value"] == str(original_workspace)
        assert req.workspace_dir == str(original_workspace)
        # No fresh batch container was created for a preset-only batch.
        batches = [
            p for p in (tmp_path / ".bananaflow_tmp").iterdir()
            if p.name != "batch-original"
        ]
        assert batches == []


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

    def test_successful_job_cleans_its_own_subdir_leaving_a_paused_sibling(self, tmp_path):
        """Per-job cleanup on success must not touch a paused sibling's
        subdir. Job A completes (subdir removed); job B is paused mid-run
        (subdir preserved). The batch container survives because B is
        paused."""
        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks(), max_workers=1)

        job_a = _job("a", "http://a", str(tmp_path))
        job_b = _job("b", "http://b", str(tmp_path))

        original = engine.download

        def _run(req):
            # Pause B (the second job) but let A complete normally.
            if req.url == "http://b" and req.cancel_event is not None:
                req.cancel_event.set()
            return original(req)

        engine.download = _run
        orch.run_batch([job_a, job_b])

        ws = {r.url: r.workspace_dir for r in engine.seen_requests}
        assert not Path(ws["http://a"]).exists(), "completed job's subdir must be cleaned"
        assert Path(ws["http://b"]).exists(), "paused sibling's subdir must survive"


class TestWorkspaceIsolationFailure:
    def test_isolation_failure_errors_jobs_instead_of_visible_partials(self, tmp_path, monkeypatch):
        """If NO isolated workspace can be created, jobs must be reported as
        errors — never silently downloaded into the user's visible output
        directory (which would defeat the whole 'no visible partials'
        invariant)."""
        import core.download_orchestrator as orch_mod

        def _boom(_base):
            raise OSError("no workspace anywhere")

        monkeypatch.setattr(orch_mod, "make_batch_workspace", _boom)

        class RecordingCallbacks:
            def __init__(self):
                self.errors = []
                self.statuses = []
            def __getattr__(self, _name):
                return lambda *a, **k: None
            def on_track_error(self, key, err):
                self.errors.append(key)
            def on_track_status(self, key, status):
                self.statuses.append((key, status))

        engine = FakeEngine()
        cb = RecordingCallbacks()
        orch = DownloadOrchestrator(engine=engine, callbacks=cb, max_workers=1)

        result = orch.run_batch([_job("a", "http://a", str(tmp_path))])

        assert engine.seen_requests == []  # engine never ran → no visible writes
        assert "a" in cb.errors
        assert ("a", "error") in cb.statuses
        assert result.failed == 1
        # Nothing was written into the output dir.
        assert list(tmp_path.iterdir()) == []

    def test_resumed_job_with_preset_workspace_still_runs_on_isolation_failure(self, tmp_path, monkeypatch):
        """A preset-workspace resume job needs no fresh workspace, so a
        workspace-creation failure must NOT knock it out — only jobs that
        actually needed a new workspace are failed."""
        import core.download_orchestrator as orch_mod

        monkeypatch.setattr(
            orch_mod, "make_batch_workspace",
            lambda _b: (_ for _ in ()).throw(OSError("no workspace")),
        )

        preset = tmp_path / ".bananaflow_tmp" / "batch-x" / "job-a"
        preset.mkdir(parents=True)
        req = DownloadRequest(
            url="http://a", output_dir=str(tmp_path), media_type=MediaType.AUDIO,
            workspace_dir=str(preset),
        )

        engine = FakeEngine()
        orch = DownloadOrchestrator(engine=engine, callbacks=NullCallbacks())
        orch.run_batch([("a", req)])

        # The preset job ran despite the fresh-workspace failure.
        assert [r.url for r in engine.seen_requests] == ["http://a"]
