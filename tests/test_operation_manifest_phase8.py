from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.metadata_models import AudioTrackItem, OriginalTags
from core.metadata_processor import backup_tags, load_tag_backup
from core.metadata_models import ApplyOutcome, ApplyStatus
from core.operation_manifest import MANIFEST_SCHEMA_VERSION, ManifestError, finalize_manifest, read_manifest


def _item(path: Path) -> AudioTrackItem:
    path.write_bytes(b"audio")
    return AudioTrackItem(path=path, folder=path.parent, ext=".mp3", original=OriginalTags(title="old"))


def test_schema4_manifest_records_apply_intent_and_stays_restore_compatible(tmp_path: Path):
    item = _item(tmp_path / "song.mp3")
    item.proposed.title = "new"
    item.proposed_filename = "renamed.mp3"
    backup = tmp_path / "bananaflow_tag_backup_test.json"
    backup_tags([item], backup, operation_id="op-1", root=tmp_path, app_version="test")
    manifest = read_manifest(backup)
    assert manifest["schema"] == MANIFEST_SCHEMA_VERSION
    assert manifest["operation_type"] == "apply"
    assert manifest["records"][0]["planned_fields"] == ["title"]
    assert manifest["records"][0]["intended_path"].endswith("renamed.mp3")
    assert load_tag_backup(backup)[0][1].title == "old"


def test_manifest_rejects_malformed_schema4(tmp_path: Path):
    path = tmp_path / "bananaflow_tag_backup_bad.json"
    path.write_text(json.dumps({"schema": 4, "operation_id": "", "records": []}), encoding="utf-8")
    with pytest.raises(ManifestError):
        read_manifest(path)


def test_manifest_finalization_persists_verified_outcome(tmp_path: Path):
    item = _item(tmp_path / "song.mp3")
    backup = tmp_path / "bananaflow_tag_backup_test.json"
    backup_tags([item], backup, operation_id="op-2")
    finalize_manifest(backup, [ApplyOutcome(item.path, item.path, status=ApplyStatus.SUCCESS,
                                             fields_written=["title"])], status="completed")
    record = read_manifest(backup)["records"][0]
    assert record["result"]["status"] == ApplyStatus.SUCCESS
    assert record["result"]["fields_written"] == ["title"]
