"""
tests/test_paused_persistence_integration.py  –  Controller <-> store wiring
==============================================================================
The DownloadController persists paused state to the single authoritative
PausedBatchStore on every pause change, and restores it on startup. These
tests drive the controller directly (offscreen Qt; DownloadWorker and cards
faked) and assert the persisted file reflects each transition, and that a
fresh controller can restore the paused jobs after a simulated restart.
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


class _FakeCard:
    def __init__(self, title: str, status: str = "downloading") -> None:
        self.title = title
        self.artist = "Artist"
        self.track_url = f"https://youtu.be/{title}"
        self.duration = "3:00"
        self.thumbnail_url = "http://thumb"
        self.platform = "youtube"
        self.album = "Album"
        self.parent_artist = ""
        self.release_type = ""
        self.category = ""
        self.album_index = 0
        self.total_tracks = 0
        self._status = status

    def get_status(self):
        return self._status

    def set_status(self, s):
        self._status = s

    def set_progress(self, f):
        pass


class _FakeLiveWorker:
    def __init__(self, jobs):
        self._jobs = jobs

    def isRunning(self):
        return True

    def cancel(self):
        pass


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


def _req(title, ws):
    return DownloadRequest(
        url=f"https://youtu.be/{title}", output_dir="/out",
        media_type=MediaType.AUDIO, forced_title=title, workspace_dir=ws,
    )


def test_global_pause_persists_to_store(tmp_path, monkeypatch, app):
    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard("A")
    k = str(id(card))
    ctrl._key_to_card = {k: card}
    ctrl._dl_worker = _FakeLiveWorker([(k, _req("A", "/ws/batch-1/keyA"))])

    ctrl.global_pause()

    persisted = ctrl._paused_store.load()
    assert len(persisted) == 1
    assert persisted[0].card["title"] == "A"
    assert persisted[0].workspace_dir == "/ws/batch-1/keyA"
    assert persisted[0].request["forced_title"] == "A"


def test_resume_all_clears_the_store(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as dw_mod

    class _FakeWorker:
        def __init__(self, *a, **k):
            for n in ("track_progress", "track_speed", "track_status",
                      "track_finished", "track_preexisting", "overall_progress",
                      "metrics", "batch_snapshot", "job_count_changed", "job_error",
                      "all_finished", "track_thumbnail"):
                setattr(self, n, type("S", (), {"connect": lambda *a, **k: None})())

        def start(self):
            pass

        def isRunning(self):
            return False

    monkeypatch.setattr(dw_mod, "DownloadWorker", _FakeWorker)

    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard("A", status="paused")
    k = str(id(card))
    ctrl._key_to_card = {k: card}
    ctrl._paused_requests = {k: _req("A", "/ws/batch-1/keyA")}
    ctrl._persist_paused_state()
    assert len(ctrl._paused_store.load()) == 1

    ctrl._dl_worker = None
    ctrl.resume_all()

    assert ctrl._paused_store.load() == [], "resuming everything clears the store"


def test_restart_restores_paused_jobs(tmp_path, monkeypatch, app):
    # 1) First controller pauses a job (persists it), with a REAL workspace.
    from utils.paths import make_batch_workspace
    out = tmp_path / "out"
    out.mkdir()
    container = make_batch_workspace(str(out))
    sub = container / "keyA"
    sub.mkdir()
    (sub / "song.part").write_bytes(b"partial")

    ctrl1 = _controller(tmp_path, monkeypatch, app)
    ctrl1._cfg.output_dir = str(out)
    card = _FakeCard("A")
    k = str(id(card))
    ctrl1._key_to_card = {k: card}
    ctrl1._dl_worker = _FakeLiveWorker([(k, _req("A", str(sub)))])
    ctrl1.global_pause()

    # 2) A FRESH controller (simulated restart) restores from the same store.
    ctrl2 = _controller(tmp_path, monkeypatch, app)
    ctrl2._cfg.output_dir = str(out)

    created = []

    def _factory(card_dict):
        c = _FakeCard(card_dict.get("title", "?"), status="queued")
        created.append((c, card_dict))
        return c

    restored = ctrl2.restore_paused_on_startup(_factory)

    assert len(restored) == 1
    assert created[0][1]["title"] == "A"
    # The rebuilt request is in the new controller's paused set, workspace kept.
    new_key = str(id(restored[0]))
    assert new_key in ctrl2._paused_requests
    assert ctrl2._paused_requests[new_key].workspace_dir == str(sub)
    assert sub.exists(), "a valid paused workspace must survive the startup sweep"


def test_restart_skips_jobs_whose_workspace_vanished(tmp_path, monkeypatch, app):
    ctrl = _controller(tmp_path, monkeypatch, app)
    out = tmp_path / "out"
    out.mkdir()
    ctrl._cfg.output_dir = str(out)

    # Persist a job whose workspace does NOT exist on disk.
    from core.paused_batch_store import PausedJob
    ctrl._paused_store.save([PausedJob(
        key="gone", card={"title": "Gone"},
        request={"url": "u", "output_dir": str(out), "workspace_dir": str(out / ".bananaflow_tmp" / "batch-missing" / "k")},
        workspace_dir=str(out / ".bananaflow_tmp" / "batch-missing" / "k"),
    )])

    restored = ctrl.restore_paused_on_startup(lambda cd: _FakeCard(cd.get("title", "?")))

    assert restored == []
    assert ctrl._paused_requests == {}
    # Store is re-persisted to reflect nothing restorable.
    assert ctrl._paused_store.load() == []


def test_startup_sweep_reclaims_abandoned_workspace(tmp_path, monkeypatch, app):
    from utils.paths import make_batch_workspace
    ctrl = _controller(tmp_path, monkeypatch, app)
    out = tmp_path / "out"
    out.mkdir()
    ctrl._cfg.output_dir = str(out)

    abandoned = make_batch_workspace(str(out))  # not referenced by any paused job
    (abandoned / "junk").mkdir()

    ctrl.restore_paused_on_startup(lambda cd: None)

    assert not abandoned.exists(), "an abandoned workspace must be swept on startup"
