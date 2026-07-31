import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.metadata_validation import DuplicateConfidence, DuplicateEvidence, DuplicateGroup, DuplicateHash, DuplicateScanResult
from ui.dialogs.duplicate_files_dialog import DuplicateFilesDialog
from ui.controllers.metadata_controller import MetadataController
from ui.workers.duplicate_detector_worker import DuplicateDetectorWorker
from core.metadata_models import AudioTrackItem, OriginalTags


def test_structured_duplicate_groups_use_honest_evidence_and_confidence(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, True)
    audio = worker._structured({"hash": [(tmp_path / "one.mp3", DuplicateHash("hash", DuplicateEvidence.AUDIO_PAYLOAD)), (tmp_path / "two.mp3", DuplicateHash("hash", DuplicateEvidence.AUDIO_PAYLOAD))]})
    assert audio.groups[0].evidence is DuplicateEvidence.AUDIO_PAYLOAD
    assert audio.groups[0].confidence is DuplicateConfidence.HIGH
    assert audio.groups[0].safe_for_destructive_resolution


def test_a_size_only_group_is_still_refused_for_deletion(tmp_path):
    """No scan produces one any more, but the guard outlives the strategy."""
    group = DuplicateGroup("g", (str(tmp_path / "one.wav"), str(tmp_path / "two.wav")),
                           DuplicateEvidence.SIZE_ONLY, DuplicateConfidence.POSSIBLE, "size", 42)
    assert not group.safe_for_destructive_resolution
    assert group.confidence_key == "duplicates_confidence_possible"


def test_whole_file_fallback_never_claims_audio_payload(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, True)
    result = worker._structured({"hash": [(tmp_path / "one.opus", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE)), (tmp_path / "two.opus", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE))]})
    assert result.groups[0].evidence is DuplicateEvidence.WHOLE_FILE


def test_hash_evidence_comes_from_the_actual_container_path(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, True)
    mp3 = tmp_path / "audio.mp3"; mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00frames")
    flac = tmp_path / "audio.flac"; flac.write_bytes(b"fLaC\x80\x00\x00\x00frames")
    m4a = tmp_path / "audio.m4a"; m4a.write_bytes((12).to_bytes(4, "big") + b"mdat" + b"data")
    mp4 = tmp_path / "audio.mp4"; mp4.write_bytes((12).to_bytes(4, "big") + b"mdat" + b"data")
    broken = tmp_path / "broken.m4a"; broken.write_bytes(b"not-an-mp4")
    broken_flac = tmp_path / "broken.flac"; broken_flac.write_bytes(b"fLaC\x80\x00")
    aac = tmp_path / "raw.aac"; aac.write_bytes(b"raw-aac")
    assert worker._audio_hash(mp3).evidence is DuplicateEvidence.AUDIO_PAYLOAD
    assert worker._audio_hash(flac).evidence is DuplicateEvidence.AUDIO_PAYLOAD
    assert worker._audio_hash(m4a).evidence is DuplicateEvidence.AUDIO_PAYLOAD
    assert worker._audio_hash(mp4).evidence is DuplicateEvidence.AUDIO_PAYLOAD
    assert worker._audio_hash(broken).evidence is DuplicateEvidence.WHOLE_FILE
    assert worker._audio_hash(broken_flac).evidence is DuplicateEvidence.WHOLE_FILE
    assert worker._audio_hash(aac).evidence is DuplicateEvidence.WHOLE_FILE


def test_partial_scan_result_keeps_unreadable_file_evidence_localized_for_the_dialog(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, True)
    worker._warnings.append((str(tmp_path / "locked.mp3"), "hash", "duplicate_read_failed"))
    result = worker._structured({"hash": [(tmp_path / "one.mp3", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE)),
                                            (tmp_path / "two.mp3", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE))]})
    assert result.partial
    assert result.warnings == ((str(tmp_path / "locked.mp3"), "hash", "duplicate_read_failed"),)


# --------------------------------------------------------------------------- #
# The two-pass scan: pass 1 buckets by payload length, pass 2 hashes survivors
# --------------------------------------------------------------------------- #

def _syncsafe(size: int) -> bytes:
    return bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))


def _write_mp3(path, audio: bytes, tag_bytes: int = 64) -> None:
    """An ID3v2 tag of `tag_bytes` -- standing in for cover art -- then audio."""
    path.write_bytes(b"ID3\x04\x00\x00" + _syncsafe(tag_bytes) + b"\x00" * tag_bytes + audio)


def _scan(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, False)
    files = sorted(tmp_path.glob("*.mp3"))
    candidates = worker._same_length_candidates(files, len(files), 0.0)
    result = worker._structured(worker._find_by_md5(candidates, len(candidates), 0.0))
    return worker, candidates, result


def test_same_audio_with_different_cover_art_survives_the_length_pass(tmp_path):
    """Bucketing on file size would lose exactly this pair; payload length must not."""
    audio = b"frames" * 500
    _write_mp3(tmp_path / "a.mp3", audio, tag_bytes=64)
    _write_mp3(tmp_path / "b.mp3", audio, tag_bytes=40_000)   # same song, big cover
    assert (tmp_path / "a.mp3").stat().st_size != (tmp_path / "b.mp3").stat().st_size

    _worker, candidates, result = _scan(tmp_path)
    assert len(candidates) == 2
    assert len(result.groups) == 1
    assert result.groups[0].paths == (str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3"))
    assert result.groups[0].confidence is DuplicateConfidence.HIGH


def test_the_length_pass_drops_files_no_hash_could_have_matched(tmp_path):
    _write_mp3(tmp_path / "a.mp3", b"frames" * 500)
    _write_mp3(tmp_path / "b.mp3", b"frames" * 500)
    _write_mp3(tmp_path / "lonely.mp3", b"other" * 700)       # unique length

    _worker, candidates, result = _scan(tmp_path)
    assert tmp_path / "lonely.mp3" not in candidates
    assert len(candidates) == 2
    assert len(result.groups) == 1


def test_a_shared_length_between_different_songs_is_not_a_group(tmp_path):
    """Same byte length, different audio: pass 2 still has to agree."""
    _write_mp3(tmp_path / "a.mp3", b"A" * 3000)
    _write_mp3(tmp_path / "b.mp3", b"B" * 3000)

    _worker, candidates, result = _scan(tmp_path)
    assert len(candidates) == 2          # both survived pass 1 ...
    assert result.groups == ()           # ... and neither was called a duplicate


def test_an_unreadable_header_is_carried_into_the_hash_pass(tmp_path, monkeypatch):
    """Dropping it in pass 1 is the one way this design could lose a duplicate."""
    _write_mp3(tmp_path / "a.mp3", b"frames" * 500)
    worker = DuplicateDetectorWorker(tmp_path, False)
    monkeypatch.setattr(worker, "_payload_length",
                        lambda path: (_ for _ in ()).throw(OSError("locked")))

    files = sorted(tmp_path.glob("*.mp3"))
    candidates = worker._same_length_candidates(files, len(files), 0.0)
    assert candidates == files
    assert worker._warnings == [(str(tmp_path / "a.mp3"), "prefilter", "duplicate_read_failed")]


def test_progress_is_throttled_rather_than_emitted_per_file(tmp_path):
    """Each signal costs the panel a relayout, so they must not track files.

    Unthrottled, a 66-candidate scan spent ~95s repainting and ~3s hashing.
    """
    from ui.workers.duplicate_detector_worker import _PROGRESS_INTERVAL

    worker = DuplicateDetectorWorker(tmp_path, False)
    emitted = []
    worker.progress.connect(lambda *a: emitted.append(a))

    t0 = time.perf_counter()
    for i in range(500):
        worker._emit_progress(i + 1, 500, t0, final=(i == 499))

    # A burst that takes no real time may only announce itself once, plus the
    # closing one that must always land so the bar reaches its end.
    assert len(emitted) <= 2, f"{len(emitted)} signals for an instant loop"
    assert emitted[-1][0] == 500
    assert _PROGRESS_INTERVAL >= 0.1


def test_a_trailing_id3v1_tag_is_excluded_even_in_one_chunk(tmp_path):
    """The read chunk is now larger than most payloads; the end bound must hold."""
    audio = b"frames" * 100
    tagged = tmp_path / "tagged.mp3"
    clean = tmp_path / "clean.mp3"
    _write_mp3(tagged, audio + b"TAG" + b"x" * 125)
    _write_mp3(clean, audio)

    worker = DuplicateDetectorWorker(tmp_path, False)
    assert tagged.stat().st_size != clean.stat().st_size
    assert worker._audio_hash(tagged).digest == worker._audio_hash(clean).digest
    assert worker._payload_length(tagged) == worker._payload_length(clean)


def test_scanning_is_not_capped_at_ten_thousand_files(tmp_path):
    """The size-only fallback is gone; every group is hash-backed at any scale."""
    import inspect

    source = inspect.getsource(DuplicateDetectorWorker)
    assert "10_000" not in source and "_find_by_size" not in source
    assert not hasattr(DuplicateDetectorWorker, "_find_by_size")


def test_controller_attaches_only_current_workspace_ids_to_an_accepted_result(tmp_path):
    QApplication.instance() or QApplication([])
    first, second = tmp_path / "one.mp3", tmp_path / "two.mp3"
    controller = MetadataController()
    controller.workspace_state.set_tracks([
        AudioTrackItem(first, tmp_path, ".mp3", original=OriginalTags()),
        AudioTrackItem(second, tmp_path, ".mp3", original=OriginalTags()),
    ])
    group = DuplicateGroup("group", (str(first), str(second), str(tmp_path / "gone.mp3")),
                           DuplicateEvidence.WHOLE_FILE, DuplicateConfidence.HIGH, "md5")
    attached = controller._attach_duplicate_workspace_ids(DuplicateScanResult((group,)))
    assert attached.groups[0].workspace_ids == tuple(sorted(
        controller.workspace_state.item_id(item) for item in controller.workspace_state.tracks))


def test_possible_duplicate_dialog_keeps_every_file_by_default(tmp_path):
    QApplication.instance() or QApplication([])
    first, second = tmp_path / "one.mp3", tmp_path / "two.mp3"
    first.write_bytes(b"one"); second.write_bytes(b"two")
    group = DuplicateGroup("group", (str(first), str(second)), DuplicateEvidence.SIZE_ONLY,
                           DuplicateConfidence.POSSIBLE, "size", size=3)
    dialog = DuplicateFilesDialog(DuplicateScanResult((group,)), 0.1, "size", tmp_path)
    assert dialog.files_to_delete == []
    assert all(box.isChecked() for boxes in dialog._group_cbs for box in boxes)
