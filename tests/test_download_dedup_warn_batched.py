"""
tests/test_download_dedup_warn_batched.py  –  One dialog per batch, not per file
====================================================================================
The "warn" duplicate policy used to call confirm() once per duplicate file
found while building the batch — a 40-track playlist with 40 existing files
meant 40 consecutive pop-ups. start_batch() now defers every "warn"
duplicate into a single batched dialog (ui.dialogs.batch_duplicate_dialog)
shown exactly once per batch, with Skip all / Replace all outcomes.

This exercises DownloadController.start_batch() directly (DownloadWorker
and ask_batch_duplicate_action are both monkeypatched out — no real QThread,
no real Qt dialog) and asserts:
  * the batched-dialog function is called at most once per start_batch call,
    regardless of how many duplicates were found.
  * it receives every duplicate found, and only those.
  * "skip all" routes every duplicate to preexisting; "replace all" routes
    every duplicate to jobs.
  * a batch with no duplicates never calls the dialog at all.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


# ── Fakes (mirrors tests/test_download_dedup_preexisting.py) ────────────────

class _FakeCard:
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

    def set_status(self, status: str) -> None:
        self._status = status
        self.status_calls.append(status)

    def get_status(self) -> str:
        return self._status

    def set_progress(self, fraction: float) -> None:
        pass

    def is_selected(self) -> bool:
        return True


class _FakeSignal:
    def connect(self, *_a, **_k) -> None:
        pass


class _FakeDownloadWorker:
    last_instance: "_FakeDownloadWorker | None" = None

    def __init__(self, jobs, engine, config, db=None, max_workers=3,
                 preexisting=None, batch_id=None, parent=None) -> None:
        self.jobs = jobs
        self.preexisting = preexisting or []
        self.batch_id = batch_id
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


def _opts(output_dir) -> dict:
    return {
        "media_type": "audio",
        "quality_label": "audio_mp3_320",
        "audio_format": "mp3",
        "video_format": "mp4",
        "output_dir": str(output_dir),
    }


def _recording_dialog(return_value: bool):
    """A fake ask_batch_duplicate_action that records every call."""
    calls: list[list[tuple[str, str]]] = []

    def _fake(parent, items):
        calls.append(list(items))
        return return_value

    _fake.calls = calls
    return _fake


# ── Tests ──────────────────────────────────────────────────────────────────

def test_multiple_duplicates_trigger_exactly_one_dialog_call(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module
    import ui.dialogs.batch_duplicate_dialog as dialog_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)
    fake_dialog = _recording_dialog(return_value=True)
    monkeypatch.setattr(dialog_module, "ask_batch_duplicate_action", fake_dialog)

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "warn"

    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"\x00")
    monkeypatch.setattr(
        "core.duplicate_checker.find_duplicate", lambda **kwargs: existing,
    )

    cards = [_FakeCard(f"Dup {i}") for i in range(5)]
    ctrl.start_batch(cards, _opts(tmp_path), None, "")

    # ONE dialog call for the whole batch — never one per duplicate.
    assert len(fake_dialog.calls) == 1
    items = fake_dialog.calls[0]
    assert len(items) == 5
    assert {title for title, _path in items} == {c.title for c in cards}


def test_no_duplicates_never_calls_the_dialog(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module
    import ui.dialogs.batch_duplicate_dialog as dialog_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)
    fake_dialog = _recording_dialog(return_value=True)
    monkeypatch.setattr(dialog_module, "ask_batch_duplicate_action", fake_dialog)

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "warn"
    monkeypatch.setattr(
        "core.duplicate_checker.find_duplicate", lambda **kwargs: None,
    )

    cards = [_FakeCard("Fresh")]
    ctrl.start_batch(cards, _opts(tmp_path), None, "")

    assert fake_dialog.calls == []
    worker = _FakeDownloadWorker.last_instance
    assert len(worker.jobs) == 1
    assert worker.preexisting == []


def test_skip_all_routes_every_duplicate_to_preexisting(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module
    import ui.dialogs.batch_duplicate_dialog as dialog_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)
    monkeypatch.setattr(
        dialog_module, "ask_batch_duplicate_action", _recording_dialog(return_value=True),
    )

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "warn"

    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"\x00")
    monkeypatch.setattr(
        "core.duplicate_checker.find_duplicate", lambda **kwargs: existing,
    )

    cards = [_FakeCard(f"Dup {i}") for i in range(3)]
    ctrl.start_batch(cards, _opts(tmp_path), None, "")

    worker = _FakeDownloadWorker.last_instance
    assert worker.jobs == []
    assert len(worker.preexisting) == 3
    assert {k for k, _p in worker.preexisting} == {str(id(c)) for c in cards}
    for card in cards:
        assert card.status_calls[-1] == "done"


def test_replace_all_routes_every_duplicate_to_jobs(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module
    import ui.dialogs.batch_duplicate_dialog as dialog_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)
    monkeypatch.setattr(
        dialog_module, "ask_batch_duplicate_action", _recording_dialog(return_value=False),
    )

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "warn"

    existing = tmp_path / "existing.mp3"
    existing.write_bytes(b"\x00")
    monkeypatch.setattr(
        "core.duplicate_checker.find_duplicate", lambda **kwargs: existing,
    )

    cards = [_FakeCard(f"Dup {i}") for i in range(3)]
    ctrl.start_batch(cards, _opts(tmp_path), None, "")

    worker = _FakeDownloadWorker.last_instance
    assert worker.preexisting == []
    assert len(worker.jobs) == 3
    assert {k for k, _req in worker.jobs} == {str(id(c)) for c in cards}
    for card in cards:
        # The post-loop reset sets every real job's card to "queued".
        assert card.status_calls[-1] == "queued"


def test_mixed_batch_only_actual_duplicates_go_to_the_dialog(tmp_path, monkeypatch, app):
    import ui.workers.download_worker as download_worker_module
    import ui.dialogs.batch_duplicate_dialog as dialog_module

    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)
    fake_dialog = _recording_dialog(return_value=True)
    monkeypatch.setattr(dialog_module, "ask_batch_duplicate_action", fake_dialog)

    ctrl = _controller(tmp_path, monkeypatch, app)
    ctrl._cfg.duplicate_action = "warn"

    existing = tmp_path / "dup.mp3"
    existing.write_bytes(b"\x00")

    def _find(**kwargs):
        return existing if kwargs.get("title") == "Dup Track" else None

    monkeypatch.setattr("core.duplicate_checker.find_duplicate", _find)

    dup_card = _FakeCard("Dup Track")
    fresh_card = _FakeCard("Fresh Track")
    ctrl.start_batch([dup_card, fresh_card], _opts(tmp_path), None, "")

    assert len(fake_dialog.calls) == 1
    assert [title for title, _p in fake_dialog.calls[0]] == ["Dup Track"]

    worker = _FakeDownloadWorker.last_instance
    assert len(worker.jobs) == 1
    assert worker.jobs[0][0] == str(id(fresh_card))
    assert len(worker.preexisting) == 1
    assert worker.preexisting[0][0] == str(id(dup_card))
