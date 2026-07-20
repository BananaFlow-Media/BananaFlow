from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.metadata_validation import DuplicateConfidence, DuplicateEvidence, DuplicateGroup, DuplicateHash, DuplicateScanResult
from ui.dialogs.duplicate_files_dialog import DuplicateFilesDialog
from ui.controllers.metadata_controller import MetadataController
from ui.workers.duplicate_detector_worker import DuplicateDetectorWorker
from core.metadata_models import AudioTrackItem, OriginalTags


def test_structured_duplicate_groups_use_honest_evidence_and_confidence(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, True)
    audio = worker._structured({"hash": [(tmp_path / "one.mp3", DuplicateHash("hash", DuplicateEvidence.AUDIO_PAYLOAD)), (tmp_path / "two.mp3", DuplicateHash("hash", DuplicateEvidence.AUDIO_PAYLOAD))]}, "md5")
    size = worker._structured({42: [tmp_path / "one.wav", tmp_path / "two.wav"]}, "size")
    assert audio.groups[0].evidence is DuplicateEvidence.AUDIO_PAYLOAD
    assert audio.groups[0].confidence is DuplicateConfidence.HIGH
    assert size.groups[0].evidence is DuplicateEvidence.SIZE_ONLY
    assert size.groups[0].confidence is DuplicateConfidence.POSSIBLE
    assert not size.groups[0].safe_for_destructive_resolution


def test_whole_file_fallback_never_claims_audio_payload(tmp_path):
    worker = DuplicateDetectorWorker(tmp_path, True)
    result = worker._structured({"hash": [(tmp_path / "one.opus", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE)), (tmp_path / "two.opus", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE))]}, "md5")
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
                                            (tmp_path / "two.mp3", DuplicateHash("hash", DuplicateEvidence.WHOLE_FILE))]}, "md5")
    assert result.partial
    assert result.warnings == ((str(tmp_path / "locked.mp3"), "hash", "duplicate_read_failed"),)


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
