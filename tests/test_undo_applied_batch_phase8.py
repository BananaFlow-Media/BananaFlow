from __future__ import annotations

from pathlib import Path
import pytest

from core.metadata_models import AudioTrackItem, OriginalTags
from core.metadata_models import RestoreOutcome, RestoreStatus
from core.metadata_processor import backup_tags
from core.operation_manifest import finalize_manifest
from core.metadata_models import ApplyOutcome, ApplyStatus
from core.undo_applied_batch import undo_applied_batch
from core.operation_manifest import ManifestError


def test_undo_applied_batch_restores_metadata_without_implicit_path_move(tmp_path: Path, monkeypatch):
    path = tmp_path / "old.mp3"; path.write_bytes(b"audio")
    item = AudioTrackItem(path, tmp_path, ".mp3", original=OriginalTags(title="original"))
    item.proposed_filename = "new.mp3"
    manifest = tmp_path / "bananaflow_tag_backup_undo.json"; backup_tags([item], manifest, operation_id="apply")
    final = tmp_path / "new.mp3"; path.rename(final)
    finalize_manifest(manifest, [ApplyOutcome(path, final, status=ApplyStatus.SUCCESS)], status="completed")
    monkeypatch.setattr("core.undo_applied_batch.restore_tags", lambda records, **_: [])
    result = undo_applied_batch(manifest, restore_paths=False)
    assert not result.physical_outcomes and final.exists() and not path.exists()


def test_undo_applied_batch_refuses_externally_changed_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "song.mp3"; path.write_bytes(b"audio")
    item = AudioTrackItem(path, tmp_path, ".mp3", original=OriginalTags(title="original"))
    manifest = tmp_path / "bananaflow_tag_backup_undo.json"; backup_tags([item], manifest, operation_id="apply")
    finalize_manifest(manifest, [ApplyOutcome(path, path, status=ApplyStatus.SUCCESS)], status="completed")
    path.write_bytes(b"changed")
    monkeypatch.setattr("core.undo_applied_batch.restore_tags", lambda records, **_: [])
    result = undo_applied_batch(manifest)
    assert result.partial and result.physical_outcomes[0].error == "file_changed_externally"


def test_undo_applied_batch_restores_approved_rename(tmp_path: Path, monkeypatch):
    path = tmp_path / "old.mp3"; path.write_bytes(b"audio")
    item = AudioTrackItem(path, tmp_path, ".mp3", original=OriginalTags(title="original"))
    item.proposed_filename = "new.mp3"
    manifest = tmp_path / "bananaflow_tag_backup_undo.json"; backup_tags([item], manifest, operation_id="apply")
    final = tmp_path / "new.mp3"; path.rename(final)
    finalize_manifest(manifest, [ApplyOutcome(path, final, status=ApplyStatus.SUCCESS)], status="completed")
    monkeypatch.setattr("core.undo_applied_batch.restore_tags", lambda records, **_: [])
    result = undo_applied_batch(manifest, restore_paths=True)
    assert not result.partial and path.exists() and not final.exists()


def test_undo_applied_batch_refuses_unfinished_manifest(tmp_path: Path):
    path = tmp_path / "song.mp3"; path.write_bytes(b"audio")
    item = AudioTrackItem(path, tmp_path, ".mp3", original=OriginalTags(title="original"))
    manifest = tmp_path / "bananaflow_tag_backup_undo.json"
    backup_tags([item], manifest, operation_id="apply")
    with pytest.raises(ManifestError, match="completed verified"):
        undo_applied_batch(manifest)


def test_metadata_restore_success_plus_path_restore_failure_is_partial(tmp_path: Path, monkeypatch):
    path = tmp_path / "old.mp3"; path.write_bytes(b"audio")
    item = AudioTrackItem(path, tmp_path, ".mp3", original=OriginalTags(title="original"))
    item.proposed_filename = "new.mp3"
    manifest = tmp_path / "bananaflow_tag_backup_partial.json"
    backup_tags([item], manifest, operation_id="apply")
    final = tmp_path / "new.mp3"; path.rename(final)
    finalize_manifest(manifest, [ApplyOutcome(path, final, status=ApplyStatus.SUCCESS)], status="completed")
    monkeypatch.setattr("core.undo_applied_batch.restore_tags",
                        lambda records, **_: [RestoreOutcome(records[0][0], RestoreStatus.UNCHANGED)])
    monkeypatch.setattr("core.undo_applied_batch.execute_rename_component_txn",
                        lambda *_a, **_k: {"status": "rolled_back", "failure": ("oserror", "locked")})
    result = undo_applied_batch(manifest, restore_paths=True)
    assert result.partial and result.metadata_outcomes[0].status == RestoreStatus.UNCHANGED
    assert result.physical_outcomes[0].status == RestoreStatus.FAILED
