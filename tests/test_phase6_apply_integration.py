"""Phase 6 Lyrics/ReplayGain integration with the established Apply worker."""
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from core.metadata_models import (
    AudioTrackItem,
    LYRICS_FIELD,
    OriginalTags,
    ProposedTags,
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
)
from core.metadata_processor import (
    ApplyWriteError,
    atomic_write_tags as real_atomic_write_tags,
    build_track_item,
    load_tag_backup,
    read_tags,
)
from tests.audio_fixtures import make_empty_audio
from ui.workers.metadata_worker import MetadataApplyWorker
from ui.controllers.metadata_controller import MetadataController


def _worker(item: AudioTrackItem, backup: Path) -> MetadataApplyWorker:
    return MetadataApplyWorker(
        [item], backup, operation_id="phase6" + "0" * 26, op_generation=7,
        root=item.folder,
    )


def test_lyrics_replaygain_and_filename_apply_together_with_readback(tmp_path):
    path = tmp_path / "before.mp3"
    make_empty_audio(path)
    item = build_track_item(path)
    item.proposed.set_lyrics("עברית\nEnglish", original=item.original.lyrics, language="heb")
    item.proposed.set_replay_gain(REPLAYGAIN_TRACK_GAIN, -2.25)
    item.proposed.set_replay_gain(REPLAYGAIN_TRACK_PEAK, 0.95)
    item.proposed_filename = "after.mp3"
    results = []
    backup_path = tmp_path / "backup.json"
    worker = _worker(item, backup_path)
    worker.finished.connect(results.append)
    worker.run()

    assert len(results) == 1 and results[0].success_count == 1
    assert len(load_tag_backup(backup_path)) == 1
    assert {"lyrics", REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK}.issubset(
        results[0].outcomes[0].fields_written
    )
    assert item.path.name == "after.mp3" and item.path.exists()
    stored = read_tags(item.path)
    assert stored.lyrics.primary.text == "עברית\nEnglish"
    assert stored.replay_gain.track_gain_db == -2.25
    assert stored.replay_gain.track_peak == 0.95
    assert not item.proposed.has_changes(item.original)


def test_failed_phase6_write_keeps_all_proposals_for_retry(tmp_path, monkeypatch):
    path = tmp_path / "retry.mp3"
    make_empty_audio(path)
    item = build_track_item(path)
    item.proposed.set_lyrics("keep pending", original=item.original.lyrics)
    item.proposed.set_replay_gain(REPLAYGAIN_TRACK_GAIN, 1.5)

    def fail(*_args, **_kwargs):
        raise ApplyWriteError("verify", "verify_failed", "forced mismatch")

    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", fail)
    results = []
    worker = _worker(item, tmp_path / "backup.json")
    worker.finished.connect(results.append)
    worker.run()
    assert results[0].failed_count == 1
    assert item.proposed.lyrics_change.value is not None
    assert REPLAYGAIN_TRACK_GAIN in item.proposed.replay_gain_changes
    assert not read_tags(path).lyrics.has_unsynchronized


def test_read_only_file_can_still_apply_physical_rename_without_metadata_writer(tmp_path, monkeypatch):
    path = tmp_path / "stream.aac"
    path.write_bytes(b"not audio")
    item = build_track_item(path)
    item.proposed_filename = "renamed.aac"
    calls = []
    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", lambda *_args: calls.append(True))
    results = []
    worker = _worker(item, tmp_path / "backup.json")
    worker.finished.connect(results.append)
    worker.run()
    assert calls == []
    assert item.path.name == "renamed.aac"
    assert results[0].success_count == 1


def test_apply_snapshot_does_not_clear_or_adopt_later_replaygain_proposal(tmp_path, monkeypatch):
    path = tmp_path / "race.mp3"
    make_empty_audio(path)
    item = build_track_item(path)
    item.proposed.title = "Persisted title"
    attempted = []

    def blocked_writer(received_path, proposal, original):
        assert proposal is not item.proposed
        assert proposal.changed_fields(original) == {"title"}
        item.proposed.set_replay_gain(REPLAYGAIN_TRACK_GAIN, -3.0)
        attempted.append(set(proposal.changed_fields(original)))
        return real_atomic_write_tags(received_path, proposal, original)

    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", blocked_writer)
    results = []
    worker = _worker(item, tmp_path / "backup.json")
    worker.finished.connect(results.append)
    worker.run()

    stored = read_tags(path)
    assert stored.title == "Persisted title"
    assert stored.replay_gain.track_gain_db is None
    assert item.original.title == "Persisted title"
    assert item.original.replay_gain.track_gain_db is None
    assert item.proposed.title is None
    assert item.proposed.replay_gain_changes[REPLAYGAIN_TRACK_GAIN].value == -3.0
    assert attempted == [{"title"}]
    assert results[0].outcomes[0].fields_written == ["title"]


def test_apply_snapshot_preserves_later_lyrics_and_replacement_field_value(tmp_path, monkeypatch):
    path = tmp_path / "later-edits.mp3"
    make_empty_audio(path)
    item = build_track_item(path)
    item.proposed.title = "Snapshot title"

    def blocked_writer(received_path, proposal, original):
        item.proposed.title = "Newer title"
        item.proposed.set_lyrics("Lyrics added during Apply", original=item.original.lyrics)
        return real_atomic_write_tags(received_path, proposal, original)

    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", blocked_writer)
    results = []
    worker = _worker(item, tmp_path / "backup.json")
    worker.finished.connect(results.append)
    worker.run()

    stored = read_tags(path)
    assert stored.title == "Snapshot title"
    assert not stored.lyrics.has_unsynchronized
    assert item.original.title == "Snapshot title"
    assert item.original.lyrics == stored.lyrics
    assert item.proposed.title == "Newer title"
    assert item.proposed.lyrics_change.value.primary.text == "Lyrics added during Apply"
    assert results[0].outcomes[0].fields_written == ["title"]
    assert LYRICS_FIELD not in results[0].outcomes[0].fields_written


def test_apply_filename_snapshot_keeps_newer_filename_pending(tmp_path, monkeypatch):
    path = tmp_path / "before.mp3"
    make_empty_audio(path)
    item = build_track_item(path)
    item.proposed.title = "Written"
    item.proposed_filename = "snapshot-name.mp3"

    def blocked_writer(received_path, proposal, original):
        item.proposed_filename = "newer-name.mp3"
        return real_atomic_write_tags(received_path, proposal, original)

    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", blocked_writer)
    results = []
    worker = _worker(item, tmp_path / "backup.json")
    worker.finished.connect(results.append)
    worker.run()

    assert item.path.name == "snapshot-name.mp3"
    assert item.path.exists()
    assert item.proposed_filename == "newer-name.mp3"
    assert read_tags(item.path).title == "Written"
    assert results[0].outcomes[0].final_path == item.path


def test_partial_batch_failure_clears_only_successful_snapshot_fields(tmp_path, monkeypatch):
    first_path, second_path = tmp_path / "one.mp3", tmp_path / "two.mp3"
    make_empty_audio(first_path)
    make_empty_audio(second_path)
    first, second = build_track_item(first_path), build_track_item(second_path)
    first.proposed.title = "First"
    second.proposed.title = "Second"

    def one_fails(path, proposal, original):
        if path == second_path:
            raise ApplyWriteError("write", "write_failed", "controlled second failure")
        return real_atomic_write_tags(path, proposal, original)

    monkeypatch.setattr("ui.workers.metadata_worker.atomic_write_tags", one_fails)
    results = []
    worker = MetadataApplyWorker(
        [first, second], tmp_path / "backup.json", operation_id="partial" + "0" * 25,
        op_generation=7, root=tmp_path,
    )
    worker.finished.connect(results.append)
    worker.run()
    assert first.proposed.title is None and first.original.title == "First"
    assert second.proposed.title == "Second" and second.original.title == ""
    assert results[0].success_count == 1 and results[0].failed_count == 1


def test_second_apply_request_is_refused_while_snapshot_worker_is_running(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    path = tmp_path / "pending.mp3"
    item = AudioTrackItem(
        path=path, folder=tmp_path, ext=".mp3", format_id="mp3",
        original=OriginalTags(),
    )
    item.proposed.title = "Pending"
    controller = MetadataController()
    controller.workspace_state.set_tracks([item])

    class RunningApply:
        def isRunning(self):
            return True

    sentinel = RunningApply()
    controller._apply_worker = sentinel
    try:
        controller.apply_changes(backup_dir=tmp_path)
        assert controller._apply_worker is sentinel
        assert item.proposed.title == "Pending"
    finally:
        controller.deleteLater()
