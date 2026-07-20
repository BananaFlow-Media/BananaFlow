from __future__ import annotations

from pathlib import Path

from core.metadata_models import AudioTrackItem, JournalFileState, OriginalTags
from core.metadata_processor import backup_tags, execute_restore_recovery, read_journal, write_journal


def test_restore_recovery_resumes_only_unverified_records(tmp_path: Path, monkeypatch):
    first = tmp_path / "first.mp3"; first.write_bytes(b"one")
    second = tmp_path / "second.mp3"; second.write_bytes(b"two")
    backup = tmp_path / "bananaflow_tag_backup_restore.json"
    items = [AudioTrackItem(first, tmp_path, ".mp3", original=OriginalTags(title="one")),
             AudioTrackItem(second, tmp_path, ".mp3", original=OriginalTags(title="two"))]
    backup_tags(items, backup, operation_id="apply")
    journal = tmp_path / "bananaflow_tag_restore_restore-1.journal.json"
    write_journal(journal, {
        "schema": 1, "operation_id": "restore-1", "operation_type": "restore",
        "backup_path": str(backup), "batch_state": "metadata_writing",
        "files": {str(first): {"state": JournalFileState.VERIFIED},
                  str(second): {"state": JournalFileState.PLANNED}},
    }, durable=True)
    calls = []
    def fake_restore(records, **_kwargs):
        calls.extend(path for path, _tags in records)
        return []
    monkeypatch.setattr("core.metadata_processor.restore_tags", fake_restore)
    outcomes, all_ok = execute_restore_recovery(journal)
    assert calls == [second]
    assert outcomes == [] and not all_ok
    assert read_journal(journal)["files"][str(first)]["state"] == JournalFileState.VERIFIED
