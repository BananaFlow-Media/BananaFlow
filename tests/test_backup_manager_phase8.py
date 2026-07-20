from __future__ import annotations

from pathlib import Path
import json

import pytest

from core.backup_manager import BackupManager, BackupManagerError
from core.metadata_models import AudioTrackItem, OriginalTags
from core.metadata_processor import backup_tags


def _backup(root: Path, name: str):
    media = root / f"{name}.mp3"; media.write_bytes(b"audio")
    item = AudioTrackItem(path=media, folder=root, ext=".mp3", original=OriginalTags(title=name))
    target = root / f"bananaflow_tag_backup_{name}.json"
    backup_tags([item], target, operation_id=name)
    return target


def test_manager_lists_valid_backup_and_protects_journal_reference(tmp_path: Path):
    path = _backup(tmp_path, "one")
    manager = BackupManager(tmp_path)
    infos = manager.list_backups(journal_paths={path})
    assert len(infos) == 1 and infos[0].valid and infos[0].interrupted
    with pytest.raises(BackupManagerError):
        manager.delete(path, journal_paths={path})
    assert path.exists()


def test_manager_refuses_delete_outside_backup_root(tmp_path: Path):
    manager = BackupManager(tmp_path)
    outside = tmp_path.parent / "not-a-backup.json"
    with pytest.raises(BackupManagerError):
        manager.delete(outside)


def test_manager_discovers_and_protects_retained_journal_reference(tmp_path: Path):
    path = _backup(tmp_path, "journaled")
    (tmp_path / "bananaflow_tag_apply_op123.journal.json").write_text(
        json.dumps({"operation_id": "op123", "backup_path": str(path), "batch_state": "applying"}),
        encoding="utf-8",
    )
    manager = BackupManager(tmp_path)
    info = manager.list_backups()[0]
    assert info.interrupted is True
    with pytest.raises(BackupManagerError):
        manager.delete(path)
