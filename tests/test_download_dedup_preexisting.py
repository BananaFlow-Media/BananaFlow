"""
tests/test_download_dedup_preexisting.py  –  Duplicate-skip accounting
=========================================================================
Root-cause regression test for the "19/19 instead of 59/59" bug: when
duplicate_action == "skip" (or the user declines the "warn" overwrite
prompt), start_batch() used to drop the card from the job list entirely
with a bare ``continue`` — the file was never counted anywhere, so a batch
of 40 already-existing files + 19 real downloads reported 19/19 completed
instead of 59/59.

This test exercises DownloadController.start_batch() directly (not the
full worker thread — DownloadWorker is monkeypatched out so no real QThread
or network activity ever starts) and asserts:
  * a duplicate-skip card is NOT dropped — it is routed to the worker's
    ``preexisting`` list.
  * the card is marked "done" immediately (instant visual feedback).
  * a card with no duplicate still goes through the normal ``jobs`` list.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


# ── Fakes ──────────────────────────────────────────────────────────────────

class _FakeCard:
    """Minimal stand-in for ui.components.track_card.TrackCard."""

    def __init__(self, title: str, artist: str = "Some Artist") -> None:
        self.title = title
        self.artist = artist
        self.album = ""
        self.parent_artist = ""
        self.release_type = ""
        self.platform = "youtube"
        self.category = ""
        self.total_tracks = 0
        self.album_index = 0
        self.queue_index = 0
        self.track_url = f"https://www.youtube.com/watch?v={title}"
        self.thumbnail_url = ""
        self.duration_sec = None
        self._status = "queued"
        self.status_calls: list[str] = []
        self.progress_calls: list[float] = []

    def set_status(self, status: str) -> None:
        self._status = status
        self.status_calls.append(status)

    def get_status(self) -> str:
        return self._status

    def set_progress(self, fraction: float) -> None:
        self.progress_calls.append(fraction)

    def is_selected(self) -> bool:
        return True


class _FakeSignal:
    def connect(self, *_a, **_k) -> None:
        pass


class _FakeDownloadWorker:
    """Records the constructor call instead of starting a real QThread."""

    last_instance: "_FakeDownloadWorker | None" = None

    def __init__(self, jobs, engine, config, db=None, max_workers=3,
                 preexisting=None, batch_id=None, parent=None) -> None:
        self.jobs = jobs
        self.preexisting = preexisting or []
        self.batch_id = batch_id
        self.started = False
        for name in (
            "track_progress", "track_speed", "track_status", "track_phase", "track_finished",
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


def _opts(output_dir) -> dict:
    return {
        "media_type": "audio",
        "quality_label": "audio_mp3_320",
        "audio_format": "mp3",
        "video_format": "mp4",
        "output_dir": str(output_dir),
    }


# ── Tests ──────────────────────────────────────────────────────────────────

def test_skip_policy_routes_card_to_preexisting_not_dropped(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module
    from ui.controllers import download_controller as ctrl_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "skip"

    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"\x00")
    monkeypatch.setattr(
        "core.duplicate_checker.find_duplicate", lambda **kwargs: existing,
    )

    card = _FakeCard("Duplicate Track")
    ctrl.start_batch([card], _opts(tmp_path), None, "")

    worker = _FakeDownloadWorker.last_instance
    assert worker is not None
    assert worker.jobs == []  # no real download job was built
    assert len(worker.preexisting) == 1
    key, path = worker.preexisting[0]
    assert path == str(existing)
    assert key == str(id(card))

    # Instant UI feedback: the card is marked done before the worker even
    # starts, and never gets clobbered back to "queued" by the reset loop.
    assert card.status_calls[-1] == "done"
    assert "queued" not in card.status_calls[card.status_calls.index("done"):]


def test_no_duplicate_goes_through_normal_job_list(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "skip"
    monkeypatch.setattr(
        "core.duplicate_checker.find_duplicate", lambda **kwargs: None,
    )

    card = _FakeCard("Fresh Track")
    ctrl.start_batch([card], _opts(tmp_path), None, "")

    worker = _FakeDownloadWorker.last_instance
    assert worker is not None
    assert worker.preexisting == []
    assert len(worker.jobs) == 1
    assert worker.jobs[0][0] == str(id(card))
    assert card.status_calls[-1] == "queued"


def test_mixed_batch_both_lists_populated(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "skip"

    existing = tmp_path / "dup.mp3"
    existing.write_bytes(b"\x00")

    def _find(**kwargs):
        return existing if kwargs.get("title") == "Dup Track" else None

    monkeypatch.setattr("core.duplicate_checker.find_duplicate", _find)

    dup_card = _FakeCard("Dup Track")
    fresh_card = _FakeCard("Fresh Track")
    ctrl.start_batch([dup_card, fresh_card], _opts(tmp_path), None, "")

    worker = _FakeDownloadWorker.last_instance
    assert worker is not None
    assert len(worker.jobs) == 1
    assert len(worker.preexisting) == 1
    assert worker.jobs[0][0] == str(id(fresh_card))
    assert worker.preexisting[0][0] == str(id(dup_card))
