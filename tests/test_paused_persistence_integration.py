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


class _FakeOrchestrator:
    """Exposes just enough of DownloadOrchestrator.live_request_snapshot()
    for global_pause(): treats every job passed to _FakeLiveWorker as live
    and immediately resumable."""

    def __init__(self, jobs) -> None:
        self._jobs = dict(jobs)

    def live_request_snapshot(self, key: str):
        import dataclasses

        req = self._jobs.get(key)
        if req is None:
            return None, None
        return None, dataclasses.replace(req)


class _FakeLiveWorker:
    def __init__(self, jobs):
        self._jobs = jobs
        self._orch = _FakeOrchestrator(jobs)

    def isRunning(self):
        return True

    def cancel(self):
        pass

    def cancel_track(self, key: str) -> None:
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


def test_corrupt_paused_file_skips_the_sweep_entirely(tmp_path, monkeypatch, app):
    """Finding #4: a corrupt paused-state file must not be treated as an
    empty (nothing-paused) one -- that would sweep with an empty keep-set
    and delete every existing, potentially still-resumable workspace."""
    from utils.paths import make_batch_workspace

    ctrl = _controller(tmp_path, monkeypatch, app)
    out = tmp_path / "out"
    out.mkdir()
    ctrl._cfg.output_dir = str(out)

    survivor = make_batch_workspace(str(out))
    (survivor / "keyA").mkdir()
    (survivor / "keyA" / "song.part").write_bytes(b"partial")

    ctrl._paused_store.path.parent.mkdir(parents=True, exist_ok=True)
    ctrl._paused_store.path.write_text("{ not valid json ", encoding="utf-8")

    restored = ctrl.restore_paused_on_startup(lambda cd: _FakeCard(cd.get("title", "?")))

    assert restored == []
    assert survivor.exists(), "sweep must be skipped entirely, not run with an empty keep-set"
    assert (survivor / "keyA" / "song.part").exists()


def test_sweep_covers_previous_output_dir_from_paused_job(tmp_path, monkeypatch, app):
    """Finding #16: the sweep must also cover output dirs referenced by
    currently-loaded paused jobs, not just the current config -- otherwise
    changing the destination folder strands stale workspaces under the OLD
    one forever."""
    from utils.paths import make_batch_workspace
    from core.paused_batch_store import PausedJob

    ctrl = _controller(tmp_path, monkeypatch, app)
    old_out = tmp_path / "old_out"
    new_out = tmp_path / "new_out"
    old_out.mkdir()
    new_out.mkdir()
    ctrl._cfg.output_dir = str(new_out)  # destination changed since the pause

    kept = make_batch_workspace(str(old_out))
    (kept / "keyA").mkdir()
    (kept / "keyA" / "song.part").write_bytes(b"partial")

    abandoned = make_batch_workspace(str(old_out))  # crashed sibling batch, same old dir
    (abandoned / "junk").mkdir()

    ctrl._paused_store.save([PausedJob(
        key="a", card={"title": "A"},
        request={"url": "u", "output_dir": str(old_out), "workspace_dir": str(kept / "keyA")},
    )])

    ctrl.restore_paused_on_startup(lambda cd: _FakeCard(cd.get("title", "?")))

    assert (kept / "keyA" / "song.part").exists(), "the still-paused job's own workspace survives"
    assert not abandoned.exists(), "an abandoned sibling under the OLD output dir must still be swept"


def test_card_dict_uses_track_url_and_duration_keys_and_carries_spotify_metadata(
    tmp_path, monkeypatch, app,
):
    """Finding #13: AppWindow._add_track_to_queue reads "track_url"/
    "duration" (not "url"/"duration_str") when handed a plain dict, which
    is exactly what restore_paused_on_startup passes it -- the mismatched
    spelling silently restored every paused card with an empty URL and
    duration. Also must carry the Spotify two-stage identity fields a
    pending card needs to rebuild its resolver."""
    ctrl = _controller(tmp_path, monkeypatch, app)

    class _SpotifyCard(_FakeCard):
        def __init__(self):
            super().__init__("Track")
            self.spotify_id = "abc123"
            self.spotify_key_kind = "spotify_id"
            self.duration_sec = 200
            self.match_status = "pending"

    card_dict = ctrl._card_to_dict(_SpotifyCard())

    assert card_dict["track_url"] == "https://youtu.be/Track"
    assert card_dict["duration"] == "3:00"
    assert "url" not in card_dict
    assert "duration_str" not in card_dict
    assert card_dict["spotify_id"] == "abc123"
    assert card_dict["spotify_key_kind"] == "spotify_id"
    assert card_dict["duration_sec"] == 200
    assert card_dict["match_status"] == "pending"


def test_restore_rebuilds_spotify_resolver_for_pending_job(tmp_path, monkeypatch, app):
    """Finding #5: a job paused before its Spotify two-stage match ever ran
    has no resolved URL and (per had_pending_resolver) no persisted
    resolver -- the live closure can't survive a restart. Restore must
    rebuild an EQUIVALENT one from the persisted identity fields, not leave
    the job with neither a resolver nor a usable URL."""
    from core.paused_batch_store import PausedJob
    import core.scraper as scraper_mod

    ctrl = _controller(tmp_path, monkeypatch, app)
    out = tmp_path / "out"
    out.mkdir()
    ctrl._cfg.output_dir = str(out)

    ctrl._paused_store.save([PausedJob(
        key="a",
        card={
            "title": "Track", "spotify_id": "abc123",
            "spotify_key_kind": "spotify_id", "duration_sec": 200,
            "match_status": "pending",
        },
        request={
            "url": "pending-spotify-match", "output_dir": str(out),
            "had_pending_resolver": True,
        },
    )])

    restored = ctrl.restore_paused_on_startup(lambda cd: _FakeCard(cd.get("title", "?")))

    assert len(restored) == 1
    key = str(id(restored[0]))
    req = ctrl._paused_requests[key]
    assert req.url_resolver is not None

    monkeypatch.setattr(
        scraper_mod, "resolve_track_to_youtube",
        lambda td, cookies_file=None, cancel_check=None: "https://youtube.com/watch?v=matched",
    )
    resolved = req.url_resolver(None)
    assert "matched" in resolved


def test_resume_track_persists_only_after_the_worker_starts(tmp_path, monkeypatch, app):
    """Finding #14: clearing the on-disk record BEFORE the worker actually
    starts leaves a crash window where a kill between the two makes
    resumable work look like nothing was ever paused -- the next startup's
    sweep would then have no keep-set entry protecting its workspace."""
    import ui.workers.download_worker as dw_mod

    call_order = []

    class _OrderedFakeWorker:
        all_finished = type("S", (), {"connect": lambda *a, **k: None})()

        def __init__(self, *a, **k):
            for n in ("track_progress", "track_speed", "track_status",
                      "track_finished", "job_error", "all_finished", "track_thumbnail"):
                setattr(self, n, type("S", (), {"connect": lambda *a, **k: None})())

        def start(self):
            call_order.append("start")

    monkeypatch.setattr(dw_mod, "DownloadWorker", _OrderedFakeWorker)

    ctrl = _controller(tmp_path, monkeypatch, app)
    card = _FakeCard("A", status="paused")
    k = str(id(card))
    ctrl._key_to_card = {k: card}
    ctrl._paused_requests = {k: _req("A", "/ws/batch-1/keyA")}

    real_persist = ctrl._persist_paused_state
    def _tracked_persist():
        call_order.append("persist")
        real_persist()
    monkeypatch.setattr(ctrl, "_persist_paused_state", _tracked_persist)

    ctrl.resume_track(card)

    assert call_order == ["start", "persist"], (
        "the on-disk record must only be cleared AFTER the worker has "
        "actually started, never before"
    )


def test_queue_state_save_excludes_paused_cards(tmp_path, monkeypatch, app):
    """Paused cards are owned by the PausedBatchStore and restored from there;
    the general queue_state must NOT also save them, or a paused card would
    be restored twice on the next launch."""
    from ui.app_window import AppWindow

    class _Card:
        def __init__(self, title, status):
            self.title = title
            self.artist = ""
            self.track_url = ""
            self.duration = ""
            self.thumbnail_url = ""
            self.platform = "youtube"
            self.album = ""
            self.parent_artist = ""
            self.release_type = ""
            self.category = ""
            self.album_index = 0
            self.total_tracks = 0
            self._status = status

        def get_status(self):
            return self._status

    class _Panel:
        def __init__(self, cards):
            self._cards = cards

        def get_all_cards(self):
            return self._cards

    class _Cfg:
        def __init__(self):
            self.queue_state = None

        def save(self):
            pass

    shim = AppWindow.__new__(AppWindow)
    shim._queue_panel = _Panel([
        _Card("queued", "queued"),
        _Card("downloading", "downloading"),
        _Card("paused", "paused"),
        _Card("done", "done"),
    ])
    shim._cfg = _Cfg()

    AppWindow._save_queue_state(shim)

    saved_titles = {item["title"] for item in shim._cfg.queue_state}
    assert "paused" not in saved_titles, "paused cards are owned by the store"
    assert "done" not in saved_titles
    assert saved_titles == {"queued", "downloading"}
