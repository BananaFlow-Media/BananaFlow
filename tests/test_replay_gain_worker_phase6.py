"""ReplayGain worker progress, cancellation, partial completion, and staleness."""
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from core.metadata_models import (
    AudioTrackItem,
    OriginalTags,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_TRACK_GAIN,
)
from core.replay_gain import AlbumGroup, ReplayGainAnalysis
from ui.controllers.metadata_controller import MetadataController
from ui.workers.metadata_worker import ReplayGainAnalysisWorker


def _app():
    return QCoreApplication.instance() or QCoreApplication([])


def _item(tmp_path: Path, name: str) -> AudioTrackItem:
    path = tmp_path / name
    path.write_bytes(b"unchanged audio boundary")
    return AudioTrackItem(
        path=path, folder=tmp_path, ext=".mp3", format_id="mp3",
        metadata_editable=True, original=OriginalTags(album="Album", artist="Artist"),
    )


def _result(item: AudioTrackItem, gain: float = -2.0) -> ReplayGainAnalysis:
    return ReplayGainAnalysis(item.path, gain, 0.9, -16.0, 1.0)


def test_track_worker_reports_progress_and_never_mutates_files(tmp_path, monkeypatch):
    _app()
    tracks = [_item(tmp_path, "עברית one.mp3"), _item(tmp_path, "two space.mp3")]
    before = {item.path: item.path.read_bytes() for item in tracks}
    monkeypatch.setattr("core.replay_gain.analyse_track", lambda path, **_kw: _result(next(item for item in tracks if item.path == path)))
    values = []
    progress = []
    summaries = []
    worker = ReplayGainAnalysisWorker(
        tracks, mode="track", operation_id="op", op_generation=1,
        selection_ids=(1, 2),
    )
    worker.result_ready.connect(lambda item, result: values.append((item, result)))
    worker.progress.connect(lambda done, total: progress.append((done, total)))
    worker.finished.connect(summaries.append)
    worker.run()
    assert progress == [(1, 2), (2, 2)]
    assert len(values) == 2 and summaries[0]["failed"] == 0
    assert all(item.proposed.replay_gain_changes == {} for item in tracks)
    assert {path: path.read_bytes() for path in before} == before


def test_worker_cancellation_keeps_completed_results_honest(tmp_path, monkeypatch):
    _app()
    tracks = [_item(tmp_path, "a.mp3"), _item(tmp_path, "b.mp3")]
    monkeypatch.setattr("core.replay_gain.analyse_track", lambda path, **_kw: _result(next(item for item in tracks if item.path == path)))
    values = []
    summaries = []
    worker = ReplayGainAnalysisWorker(
        tracks, mode="track", operation_id="op", op_generation=1,
        selection_ids=(1, 2),
    )
    worker.result_ready.connect(lambda item, result: (values.append((item, result)), worker.cancel()))
    worker.finished.connect(summaries.append)
    worker.run()
    assert len(values) == 1
    assert summaries[0]["cancelled"] is True and summaries[0]["completed"] == 1


def test_album_worker_partial_failure_does_not_invent_album_values(tmp_path, monkeypatch):
    _app()
    first, second = _item(tmp_path, "a.mp3"), _item(tmp_path, "b.mp3")
    def analyze(path, **_kw):
        if path == second.path:
            raise RuntimeError("decoder failed")
        return _result(first)
    monkeypatch.setattr("core.replay_gain.analyse_track", analyze)
    values = []
    summaries = []
    worker = ReplayGainAnalysisWorker(
        [first, second], mode="album",
        groups=(AlbumGroup(("artist", "album", ""), (first, second)),),
        operation_id="op", op_generation=1, selection_ids=(1, 2),
    )
    worker.result_ready.connect(lambda item, result: values.append(result))
    worker.finished.connect(summaries.append)
    worker.run()
    assert len(values) == 1
    assert all("replaygain_album_gain" not in result for result in values)
    assert summaries[0]["failed"] == 1


def test_album_worker_emits_track_and_album_proposals_for_complete_group(tmp_path, monkeypatch):
    _app()
    first, second = _item(tmp_path, "a.mp3"), _item(tmp_path, "b.mp3")
    tracks = [first, second]
    monkeypatch.setattr(
        "core.replay_gain.analyse_track",
        lambda path, **_kw: _result(next(item for item in tracks if item.path == path)),
    )
    monkeypatch.setattr("core.replay_gain.analyse_album_program", lambda *_a, **_k: (-15.0, 0.95))
    values = []
    summaries = []
    worker = ReplayGainAnalysisWorker(
        tracks, mode="album",
        groups=(AlbumGroup(("artist", "album", ""), tuple(tracks)),),
        operation_id="op", op_generation=1, selection_ids=(1, 2),
    )
    worker.result_ready.connect(lambda item, result: values.append((item, result)))
    worker.finished.connect(summaries.append)
    worker.run()
    album_values = [result for _item, result in values if REPLAYGAIN_ALBUM_GAIN in result]
    assert len(album_values) == 2
    assert all(REPLAYGAIN_ALBUM_PEAK in result for result in album_values)
    assert summaries[0]["completed"] == 2 and summaries[0]["failed"] == 0


def test_ambiguous_album_group_emits_no_silent_album_values(tmp_path, monkeypatch):
    _app()
    first, second = _item(tmp_path, "a.mp3"), _item(tmp_path, "b.mp3")
    tracks = [first, second]
    monkeypatch.setattr(
        "core.replay_gain.analyse_track",
        lambda path, **_kw: _result(next(item for item in tracks if item.path == path)),
    )
    monkeypatch.setattr(
        "core.replay_gain.analyse_album_program",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ambiguous album must not run")),
    )
    values = []
    worker = ReplayGainAnalysisWorker(
        tracks, mode="album",
        groups=(AlbumGroup(("#1", "greatest hits", ""), (first,), True),
                AlbumGroup(("#2", "greatest hits", ""), (second,), True)),
        operation_id="op", op_generation=1, selection_ids=(1, 2),
    )
    worker.result_ready.connect(lambda item, result: values.append(result))
    worker.run()
    assert len(values) == 2
    assert all(REPLAYGAIN_ALBUM_GAIN not in result for result in values)


def test_cancellation_during_album_program_keeps_only_completed_track_results(tmp_path, monkeypatch):
    _app()
    tracks = [_item(tmp_path, "a.mp3"), _item(tmp_path, "b.mp3")]
    monkeypatch.setattr(
        "core.replay_gain.analyse_track",
        lambda path, **_kw: _result(next(item for item in tracks if item.path == path)),
    )
    def cancel_album(_paths, *, cancel_event):
        cancel_event.set()
        from core.replay_gain import ReplayGainAnalysisCancelled
        raise ReplayGainAnalysisCancelled("cancelled")
    monkeypatch.setattr("core.replay_gain.analyse_album_program", cancel_album)
    values, summaries = [], []
    worker = ReplayGainAnalysisWorker(
        tracks, mode="album",
        groups=(AlbumGroup(("artist", "album", ""), tuple(tracks)),),
        operation_id="op", op_generation=1, selection_ids=(1, 2),
    )
    worker.result_ready.connect(lambda item, result: values.append(result))
    worker.finished.connect(summaries.append)
    worker.run()
    assert len(values) == 2
    assert all(REPLAYGAIN_ALBUM_GAIN not in result for result in values)
    assert summaries[0]["cancelled"] is True


def test_controller_rejects_result_after_selection_changes(tmp_path, monkeypatch):
    _app()
    first, second = _item(tmp_path, "a.mp3"), _item(tmp_path, "b.mp3")
    controller = MetadataController()
    controller.workspace_state.set_tracks([first, second])
    controller.workspace_state.set_selected_items([first])
    monkeypatch.setattr(ReplayGainAnalysisWorker, "start", lambda self: None)
    controller.analyze_replaygain_tracks([first])
    worker = controller._replaygain_worker
    controller.workspace_state.set_selected_items([second])
    worker.result_ready.emit(first, {REPLAYGAIN_TRACK_GAIN: -3.0})
    assert first.proposed.replay_gain_changes == {}
    worker.finished.emit({"operation_id": "op", "mode": "track", "total": 1, "completed": 1, "failed": 0, "cancelled": False})
    controller.deleteLater()


def test_controller_defers_shutdown_and_cancels_running_analysis():
    _app()
    controller = MetadataController()

    class Signal:
        def connect(self, *_args):
            pass

    class Worker:
        finished = Signal()

        def __init__(self):
            self.cancelled = False

        def isRunning(self):
            return True

        def cancel(self):
            self.cancelled = True

    worker = Worker()
    controller._replaygain_worker = worker
    assert controller.request_shutdown() is False
    assert worker.cancelled is True
    controller.deleteLater()
