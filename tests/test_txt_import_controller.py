"""Application-level TXT import sequencing and cancellation contracts."""

from __future__ import annotations

import os
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Qt controller test is Windows-only")


def _setup(tmp_path, monkeypatch, outcomes):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    from config import AppConfig
    from core.playlist_parser import ParseResult, SourcePlatform, TrackMeta, UrlKind
    from ui.controllers.fetch_controller import FetchController
    import ui.workers.fetch_worker as worker_module

    QApplication.instance() or QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    starts = []

    class FakeWorker(QObject):
        track_found = Signal(dict, int, int)
        progress_msg = Signal(str)
        soft_error = Signal(str)
        finished = Signal(object)
        error = Signal(object)

        def __init__(self, url, **_kwargs):
            super().__init__(_kwargs.get("parent"))
            self.url = url
            self.running = False

        def start(self):
            self.running = True
            starts.append(self.url)
            outcome = outcomes[self.url]
            if outcome in ("hold", "late"):
                return
            if outcome == "error":
                self.running = False
                self.error.emit("failed")
                return
            if outcome == "result_error":
                self.running = False
                self.finished.emit(ParseResult(
                    url=self.url, kind=UrlKind.SINGLE_VIDEO,
                    platform=SourcePlatform.YOUTUBE, error="failed parse",
                ))
                return
            track = TrackMeta(
                title=outcome, url=self.url, platform=SourcePlatform.YOUTUBE,
                source_kind=UrlKind.SINGLE_VIDEO.name, source_url=self.url,
            )
            self.track_found.emit({
                "title": outcome, "track_url": self.url,
                "source_kind": track.source_kind, "source_url": track.source_url,
            }, 1, 1)
            self.running = False
            self.finished.emit(ParseResult(
                url=self.url, kind=UrlKind.SINGLE_VIDEO,
                platform=SourcePlatform.YOUTUBE, tracks=[track], total_count=1,
            ))

        def isRunning(self):
            return self.running

        def cancel(self):
            if not self.running:
                return
            self.running = False
            if outcomes[self.url] == "late":
                return
            self.finished.emit(ParseResult(
                url=self.url, kind=UrlKind.SINGLE_VIDEO,
                platform=SourcePlatform.YOUTUBE, cancelled=True,
            ))

        def wait(self, _ms):
            return True

    monkeypatch.setattr(worker_module, "FetchWorker", FakeWorker)
    controller = FetchController(AppConfig())
    return controller, starts


def test_completed_fetch_workers_do_not_accumulate_as_controller_children(
    tmp_path, monkeypatch,
):
    from PySide6.QtCore import QCoreApplication, QEvent

    urls = [f"https://youtu.be/{index:011d}" for index in range(40)]
    controller, starts = _setup(
        tmp_path, monkeypatch, {url: f"Track {index}" for index, url in enumerate(urls)},
    )

    for url in urls:
        controller.fetch(url)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()

    assert starts == urls
    assert controller._fetch_worker is None
    assert controller.children() == []


def test_real_fetch_qthreads_are_released_after_many_consecutive_completions(
    tmp_path, monkeypatch,
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from config import AppConfig
    from core.playlist_parser import ParseResult, SourcePlatform, TrackMeta, UrlKind
    from ui.controllers.fetch_controller import FetchController
    from ui.workers.fetch_worker import FetchWorker

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    def parse(_self, url, on_item=None, **_kwargs):
        track = TrackMeta(
            title=url.rsplit("/", 1)[-1], url=url,
            platform=SourcePlatform.YOUTUBE,
            source_kind=UrlKind.SINGLE_VIDEO.name, source_url=url,
        )
        on_item(track, 1, 1)
        return ParseResult(
            url=url, kind=UrlKind.SINGLE_VIDEO,
            platform=SourcePlatform.YOUTUBE, tracks=[track], total_count=1,
        )

    monkeypatch.setattr("core.playlist_parser.PlaylistParser.parse", parse)
    controller = FetchController(AppConfig())
    urls = [f"https://youtu.be/real{index:07d}" for index in range(30)]

    for url in urls:
        controller.fetch(url)
        deadline = time.monotonic() + 2
        while controller._fetch_worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert controller._fetch_worker is None

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert controller.findChildren(FetchWorker) == []


def test_txt_import_processes_every_url_in_order_and_continues_after_failure(
    tmp_path, monkeypatch,
):
    urls = [
        "https://youtu.be/aaaaaaaaaaa",
        "https://youtu.be/bbbbbbbbbbb",
        "https://youtu.be/ccccccccccc",
    ]
    source = tmp_path / "batch.txt"
    source.write_text("\n".join([urls[0], "not a URL", urls[1], urls[2]]), encoding="utf-8")
    controller, starts = _setup(
        tmp_path, monkeypatch,
        {urls[0]: "First", urls[1]: "result_error", urls[2]: "Third"},
    )
    tracks, summaries, modal_errors, finished = [], [], [], []
    controller.track_fetched.connect(tracks.append)
    controller.temporary_status.connect(summaries.append)
    controller.fetch_error.connect(modal_errors.append)
    controller.fetch_finished.connect(finished.append)

    controller.batch_import(str(source))

    assert starts == urls
    assert [track["title"] for track in tracks] == ["First", "Third"]
    assert [track["source_url"] for track in tracks] == [urls[0], urls[2]]
    assert modal_errors == []
    assert [result.url for result in finished] == [urls[0], urls[2]]
    assert summaries and "2" in summaries[-1] and "1" in summaries[-1]
    assert not controller._batch_active


def test_txt_import_cancellation_does_not_start_remaining_workers(tmp_path, monkeypatch):
    urls = ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"]
    source = tmp_path / "batch.txt"
    source.write_text("\n".join(urls), encoding="utf-8")
    controller, starts = _setup(tmp_path, monkeypatch, {urls[0]: "hold", urls[1]: "Second"})
    summaries = []
    controller.temporary_status.connect(summaries.append)

    controller.batch_import(str(source))
    assert starts == [urls[0]]
    controller.cancel()

    assert starts == [urls[0]]
    assert summaries and "cancel" in summaries[-1].lower()
    assert not controller._batch_active


def test_late_cancelled_worker_cannot_overwrite_new_txt_batch(tmp_path, monkeypatch):
    from core.playlist_parser import ParseResult, SourcePlatform, UrlKind

    old_url = "https://youtu.be/oldoldold01"
    new_url = "https://youtu.be/newnewnew01"
    source = tmp_path / "batch.txt"
    source.write_text(new_url, encoding="utf-8")
    controller, starts = _setup(
        tmp_path, monkeypatch, {old_url: "late", new_url: "New"},
    )
    tracks = []
    controller.track_fetched.connect(tracks.append)

    controller.fetch(old_url)
    stale_worker = controller._fetch_worker
    controller.batch_import(str(source))
    stale_worker.track_found.emit({"title": "STALE", "source_url": old_url}, 1, 1)
    stale_worker.finished.emit(ParseResult(
        url=old_url, kind=UrlKind.SINGLE_VIDEO,
        platform=SourcePlatform.YOUTUBE, tracks=[],
    ))

    assert starts == [old_url, new_url]
    assert [track["title"] for track in tracks] == ["New"]
    assert not controller._batch_active
