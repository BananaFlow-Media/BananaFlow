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


class _FakeOrchestrator:
    """Exposes just enough of DownloadOrchestrator.live_request_snapshot()
    for global_pause(): treats every job passed to _FakeLiveWorker as live
    and immediately resumable. These tests are about global_pause's own
    capture/skip decisions and field preservation, not the orchestrator's
    race-closing locking (that's test_orchestrator.py's job)."""

    def __init__(self, jobs) -> None:
        self._jobs = dict(jobs)

    def live_request_snapshot(self, key: str):
        req = self._jobs.get(key)
        if req is None:
            return None, None
        # snapshot_copy, matching the real orchestrator: dataclasses.replace
        # would drop the init=False output-path tracker.
        return None, req.snapshot_copy()


class _FakeLiveWorker:
    """Stands in for a running batch DownloadWorker for global_pause."""

    def __init__(self, jobs) -> None:
        self._jobs = jobs
        self._orch = _FakeOrchestrator(jobs)
        self.cancelled = False
        self.cancelled_keys: list[str] = []

    def isRunning(self) -> bool:
        return True

    def cancel(self) -> None:
        self.cancelled = True

    def cancel_track(self, key: str) -> None:
        self.cancelled_keys.append(key)


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


# ── Pausing a track that is already running in a resume worker ───────────────

class TestPauseAResumedTrack:
    """A track the user resumed individually runs in its OWN single-job
    worker, not the batch worker. Pause looked only at the batch worker, so
    that track could not be paused again: no snapshot was taken and no
    cancel was delivered, while the card was still labelled "paused" — the
    download simply carried on to completion behind a paused-looking card."""

    def test_pause_track_finds_the_resume_worker(self, tmp_path, monkeypatch, app):
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="downloading")
        key = str(id(card))
        ctrl._key_to_card = {key: card}
        ctrl._dl_worker = None
        resume_worker = _FakeLiveWorker([(key, _req("A", workspace="/ws/batch-1/jobA"))])
        ctrl._resume_workers = [resume_worker]

        ctrl.pause_track(card)

        assert key in ctrl._paused_requests
        assert ctrl._paused_requests[key].workspace_dir == "/ws/batch-1/jobA"
        assert resume_worker.cancelled_keys == [key], (
            "the resume worker must actually be told to stop the track"
        )
        assert card.get_status() == "paused"

    def test_global_pause_captures_a_running_resume_worker_too(
        self, tmp_path, monkeypatch, app,
    ):
        ctrl = _controller(tmp_path, monkeypatch, app)
        batch_card = _FakeCard("B", status="downloading")
        resumed_card = _FakeCard("R", status="downloading")
        kb, kr = str(id(batch_card)), str(id(resumed_card))
        ctrl._key_to_card = {kb: batch_card, kr: resumed_card}
        ctrl._dl_worker = _FakeLiveWorker([(kb, _req("B"))])
        resume_worker = _FakeLiveWorker([(kr, _req("R"))])
        ctrl._resume_workers = [resume_worker]

        ctrl.global_pause()

        assert set(ctrl._paused_requests) == {kb, kr}
        assert resumed_card.get_status() == "paused"
        assert resume_worker.cancelled is True

    def test_pause_track_does_not_relabel_a_card_it_could_not_pause(
        self, tmp_path, monkeypatch, app,
    ):
        """No worker owns the key, so nothing was snapshotted and no cancel
        was delivered. Labelling the card "paused" anyway offered the user a
        Resume with nothing behind it, over a download that is either still
        running or already finished."""
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="downloading")
        ctrl._dl_worker = None
        ctrl._resume_workers = []

        assert ctrl.pause_track(card) is False
        assert ctrl._paused_requests == {}
        assert card.get_status() == "downloading", (
            "the card must keep its real status when the pause did not happen"
        )

    def test_pause_track_does_not_relabel_a_job_that_already_finished(
        self, tmp_path, monkeypatch, app,
    ):
        """The orchestrator refuses a snapshot for a job that reached a
        terminal state. Overwriting a completed card with "paused" is the
        same lie in the other direction."""
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="done")
        key = str(id(card))
        ctrl._key_to_card = {key: card}
        worker = _FakeLiveWorker([(key, _req("A"))])
        # Stand in for a job the orchestrator considers terminal.
        worker._orch.live_request_snapshot = lambda _k: (None, None)
        ctrl._dl_worker = worker

        assert ctrl.pause_track(card) is False
        assert card.get_status() == "done"
        assert ctrl._paused_requests == {}

    def test_pause_track_labels_and_records_when_it_really_paused(
        self, tmp_path, monkeypatch, app,
    ):
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="downloading")
        key = str(id(card))
        ctrl._key_to_card = {key: card}
        worker = _FakeLiveWorker([(key, _req("A", workspace="/ws/batch-1/jobA"))])
        ctrl._dl_worker = worker

        assert ctrl.pause_track(card) is True
        assert card.get_status() == "paused"
        assert key in ctrl._paused_requests
        assert worker.cancelled_keys == [key]


# ── Post-download pause (post-processing / publish phase) ────────────────────

class TestPostDownloadResumeCheckpoint:
    """yt-dlp's .part continuation only covers the DOWNLOAD. A job paused
    after every byte arrived is somewhere in post-processing or publishing;
    without a recorded phase the resumed request re-ran yt-dlp against an
    already-complete file, no postprocessor hook fired, _final_output_path
    stayed empty, and the resume died with "output file is missing"."""

    def test_snapshot_preserves_the_phase_and_the_workspace_file_identity(
        self, tmp_path, monkeypatch, app,
    ):
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="downloading")
        key = str(id(card))
        live = _req("A", workspace="/ws/batch-1/jobA")
        live.resume_phase = "postprocess"
        live.resume_final_path = "/ws/batch-1/jobA/A.mp3"
        ctrl._key_to_card = {key: card}
        ctrl._dl_worker = _FakeLiveWorker([(key, live)])

        ctrl.global_pause()

        snap = ctrl._paused_requests[key]
        assert snap.resume_phase == "postprocess"
        assert snap.resume_final_path == "/ws/batch-1/jobA/A.mp3"

    def test_engine_resumes_at_the_postprocess_phase_without_rerunning_ytdlp(
        self, tmp_path, monkeypatch,
    ):
        """The behaviour the checkpoint exists for: the resumed request goes
        straight to post-processing + publish for the file already sitting in
        its workspace, instead of handing an already-complete download back
        to yt-dlp."""
        from core.downloader import DownloadEngine, DownloadStatus

        workspace = tmp_path / "ws"
        output_dir = tmp_path / "out"
        workspace.mkdir()
        output_dir.mkdir()
        done_file = workspace / "A.mp3"
        done_file.write_bytes(b"complete-audio")

        engine = DownloadEngine()
        req = _req("A", workspace=str(workspace))
        req.output_dir = str(output_dir)
        req.resume_phase = "postprocess"
        req.resume_final_path = str(done_file)

        events: list = []
        req.on_progress = lambda p: events.append(p)
        req.on_finished = lambda p: events.append(p)
        req.on_error = lambda p: events.append(p)

        ydl_calls: list = []
        monkeypatch.setattr(
            engine, "_build_ydl_opts",
            lambda r: ydl_calls.append(r) or {},
        )
        pipeline_calls: list = []
        monkeypatch.setattr(
            engine, "_run_final_pipeline",
            lambda r, p: pipeline_calls.append(p) or [],
        )

        engine.download(req)

        assert ydl_calls == [], "a post-download resume must not re-run yt-dlp"
        assert pipeline_calls == [str(done_file)]
        assert (output_dir / "A.mp3").read_bytes() == b"complete-audio"
        assert events[-1].status == DownloadStatus.FINISHED
        # Published: the checkpoint is cleared so a later re-submit of the
        # same request object does not skip its own download.
        assert req.resume_phase is None
        assert req.resume_final_path is None

    def test_publish_phase_resume_skips_post_processing_too(self, tmp_path, monkeypatch):
        """Paused during the publish itself: the pipeline already ran, so
        re-running it would redo artwork/lyrics/ReplayGain work for nothing."""
        from core.downloader import DownloadEngine, DownloadStatus

        workspace = tmp_path / "ws"
        output_dir = tmp_path / "out"
        workspace.mkdir()
        output_dir.mkdir()
        done_file = workspace / "A.mp3"
        done_file.write_bytes(b"ready")

        engine = DownloadEngine()
        req = _req("A", workspace=str(workspace))
        req.output_dir = str(output_dir)
        req.resume_phase = "publish"
        req.resume_final_path = str(done_file)
        events: list = []
        req.on_progress = lambda p: events.append(p)
        req.on_finished = lambda p: events.append(p)
        req.on_error = lambda p: events.append(p)

        pipeline_calls: list = []
        monkeypatch.setattr(
            engine, "_run_final_pipeline",
            lambda r, p: pipeline_calls.append(p) or [],
        )

        engine.download(req)

        assert pipeline_calls == []
        assert (output_dir / "A.mp3").exists()
        assert events[-1].status == DownloadStatus.FINISHED

    def test_stale_checkpoint_whose_file_vanished_downloads_again(self, tmp_path):
        """A checkpoint is only usable while its workspace file still exists.
        If the workspace was swept (or the user deleted it), the job must
        fall back to a real download rather than resuming at a phase whose
        input is gone."""
        from core.downloader import DownloadEngine

        engine = DownloadEngine()
        req = _req("A", workspace=str(tmp_path / "ws"))
        req.resume_phase = "postprocess"
        req.resume_final_path = str(tmp_path / "ws" / "gone.mp3")

        assert engine._resume_checkpoint(req) is None

    def test_no_checkpoint_means_a_normal_download(self, tmp_path):
        from core.downloader import DownloadEngine

        engine = DownloadEngine()
        req = _req("A", workspace=str(tmp_path))
        assert engine._resume_checkpoint(req) is None

    def test_checkpoint_is_recorded_from_what_ytdlp_produced(self):
        """Written as the very next operation after yt-dlp returns, with no
        I/O of its own — the point is that nothing can run in between."""
        from core.downloader import DownloadEngine

        req = _req("A", workspace="/ws/batch-1/jobA")
        req._final_output_path = "/ws/batch-1/jobA/A.mp3"

        DownloadEngine._record_post_download_checkpoint(req)

        assert req.resume_phase == "postprocess"
        assert req.resume_final_path == "/ws/batch-1/jobA/A.mp3"

    def test_no_checkpoint_when_ytdlp_produced_nothing(self):
        """A ytsearch that matched no video must fall through to the normal
        "output file is missing" error, not claim a resume point it has
        not got."""
        from core.downloader import DownloadEngine

        req = _req("A", workspace="/ws/batch-1/jobA")
        DownloadEngine._record_post_download_checkpoint(req)

        assert req.resume_phase is None
        assert req.resume_final_path is None

    def test_checkpoint_exists_before_the_post_download_cancel_check(self, tmp_path):
        """The window the finding named: yt-dlp has finished, but the
        checkpoint used to be assigned only after two context-manager exits
        (real file I/O), a cancellation check and an existence probe. A
        pause landing anywhere in there snapshotted a request with no
        checkpoint AND no output path, and the resume died with "output
        file is missing"."""
        from unittest.mock import MagicMock, patch

        from core.downloader import DownloadEngine

        workspace = tmp_path / "ws"
        workspace.mkdir()
        done = workspace / "A.mp3"
        done.write_bytes(b"complete")

        engine = DownloadEngine()
        req = _req("A", workspace=str(workspace))
        req.output_dir = str(tmp_path / "out")
        req.on_progress = req.on_finished = req.on_error = lambda p: None

        seen: dict = {}

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = lambda s: mock_ydl
        mock_ydl.__exit__ = MagicMock(return_value=False)

        def _fake_download(_urls):
            # Exactly what the postprocessor hook does for a real run.
            req._final_output_path = str(done)

        mock_ydl.download = _fake_download

        def _capture_pipeline(_r, _p):
            # By the time anything downstream of yt-dlp runs, the checkpoint
            # must already be on the request.
            seen["phase"] = req.resume_phase
            seen["path"] = req.resume_final_path
            return []

        with patch("yt_dlp.YoutubeDL") as cls, \
             patch.object(engine, "_run_final_pipeline", side_effect=_capture_pipeline):
            cls.return_value = mock_ydl
            engine.download(req)

        assert seen["phase"] == "postprocess"
        assert seen["path"] == str(done)

    def test_pause_snapshot_keeps_the_output_path_tracker(
        self, tmp_path, monkeypatch, app,
    ):
        """Belt and braces for the residual instant before the checkpoint
        lands: the snapshot keeps _final_output_path, which
        dataclasses.replace resets because the field is init=False."""
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A", status="downloading")
        key = str(id(card))
        live = _req("A", workspace="/ws/batch-1/jobA")
        live._final_output_path = "/ws/batch-1/jobA/A.mp3"
        ctrl._key_to_card = {key: card}
        ctrl._dl_worker = _FakeLiveWorker([(key, live)])

        ctrl.global_pause()

        snap = ctrl._paused_requests[key]
        assert snap._final_output_path == "/ws/batch-1/jobA/A.mp3"

    def test_snapshot_copy_preserves_init_false_trackers(self):
        from core.downloader import DownloadRequest, MediaType

        req = DownloadRequest(url="u", output_dir="/out", media_type=MediaType.AUDIO)
        req._final_output_path = "/ws/x.mp3"
        req._thumb_sent = True

        copy = req.snapshot_copy()

        assert copy is not req
        assert copy._final_output_path == "/ws/x.mp3"
        assert copy._thumb_sent is True


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

    def test_does_not_capture_errored_or_already_cancelled_jobs(self, tmp_path, monkeypatch, app):
        """A card that already reached a terminal error/cancel state must
        never be captured either -- only "done" was excluded before, so an
        errored or already-cancelled job could be snapshotted and Resume
        All would re-run work the user never asked to continue."""
        ctrl = _controller(tmp_path, monkeypatch, app)
        errored = _FakeCard("errored", status="error")
        cancelled = _FakeCard("cancelled", status="cancelled")
        live = _FakeCard("live", status="downloading")
        ke, kc, kl = str(id(errored)), str(id(cancelled)), str(id(live))
        ctrl._key_to_card = {ke: errored, kc: cancelled, kl: live}
        ctrl._dl_worker = _FakeLiveWorker(
            [(ke, _req("errored")), (kc, _req("cancelled")), (kl, _req("live"))]
        )

        ctrl.global_pause()

        assert kl in ctrl._paused_requests
        assert ke not in ctrl._paused_requests, "an errored job must never be re-run"
        assert kc not in ctrl._paused_requests, "an already-cancelled job must never be re-run"

    def test_snapshot_preserves_every_field(self, tmp_path, monkeypatch, app):
        ctrl = _controller(tmp_path, monkeypatch, app)
        card = _FakeCard("A")
        k = str(id(card))
        live = _req(
            "A", workspace="/ws/batch/x/jobA", thumbnail_url="http://thumb",
            forced_album="My Album", category="stream:hls", is_solo=True,
            cookies_browser="chrome", square_thumbnails=True,
        )
        ctrl._key_to_card = {k: card}
        ctrl._dl_worker = _FakeLiveWorker([(k, live)])

        ctrl.global_pause()

        saved = ctrl._paused_requests[k]
        assert saved.workspace_dir == "/ws/batch/x/jobA"
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
        ctrl._paused_requests = {k: _req("A", workspace="/ws/batch-x/jobA")}
        ctrl._dl_worker = None

        ctrl.resume_all()

        job_req = dict(_FakeDownloadWorker.last_instance.jobs)[k]
        assert job_req.workspace_dir == "/ws/batch-x/jobA"

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
        ctrl._dl_worker = _FakeLiveWorker([(k, _req("A", workspace="/ws/batch-1/jobA"))])

        # Pause → capture → resume.
        ctrl.global_pause()
        ctrl._dl_worker = None  # the paused worker has stopped
        ctrl.resume_all()

        resumed = dict(_FakeDownloadWorker.last_instance.jobs)[k]
        assert resumed.workspace_dir == "/ws/batch-1/jobA"
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
