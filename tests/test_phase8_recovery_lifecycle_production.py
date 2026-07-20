"""Production-path integration coverage for the remaining Phase 8 matrix."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.change_drafts import DraftStore
from core.change_sets import ChangeOrigin
from core.metadata_models import AudioTrackItem, JournalFileState, OriginalTags, ProposedTags
from core.metadata_processor import (
    apply_journal_path, atomic_write_tags, backup_tags, execute_restore_recovery,
    execute_recovery, find_incomplete_journals, inspect_recovery_journal,
    read_journal, read_tags, write_journal,
)
from tests.audio_fixtures import make_empty_audio


def _apply_journal(tmp_path: Path, *, state="backup_started", backup=True):
    media = tmp_path / "song.mp3"
    make_empty_audio(media)
    operation_id = "phase8-op"
    manifest = tmp_path / "bananaflow_tag_backup_phase8.json"
    if backup:
        backup_tags([AudioTrackItem(media, tmp_path, ".mp3", original=read_tags(media))],
                    manifest, operation_id=operation_id)
    journal = apply_journal_path(manifest, operation_id)
    write_journal(journal, {
        "schema": 1, "operation_id": operation_id, "operation_type": "apply",
        "created": "2026-07-14T12:00:00", "backup_path": str(manifest),
        "batch_state": state, "files": {str(media): {
            "original_path": str(media), "state": JournalFileState.PLANNED,
        }},
    }, durable=True)
    return media, manifest, journal


def test_recovery_detects_malformed_journal_without_deleting_it(tmp_path: Path):
    journal = tmp_path / "bananaflow_tag_apply_broken.journal.json"
    journal.write_text("{broken", encoding="utf-8")
    assert find_incomplete_journals(tmp_path) == [journal]
    summary = inspect_recovery_journal(journal)
    assert summary["malformed"] and not summary["discard_allowed"] and journal.exists()


def test_recovery_classifies_backup_started_but_missing_as_inspection_only(tmp_path: Path):
    _media, _manifest, journal = _apply_journal(tmp_path, backup=False)
    summary = inspect_recovery_journal(journal)
    assert summary["backup_status"] == "missing"
    assert summary["recommended_action"] == "inspect"
    assert not summary["discard_allowed"]


def test_recovery_classifies_verified_backup_before_first_write(tmp_path: Path):
    _media, _manifest, journal = _apply_journal(tmp_path, state="applying")
    summary = inspect_recovery_journal(journal)
    assert summary["backup_status"] == "verified"
    assert summary["completed_stages"] == ["backup"]
    assert summary["recommended_action"] == "restore_verified_backup"


def test_restore_recovery_reconciles_completed_write_without_calling_writer(tmp_path: Path, monkeypatch):
    media = tmp_path / "song.mp3"
    make_empty_audio(media)
    saved = read_tags(media)
    manifest = tmp_path / "bananaflow_tag_backup_restore.json"
    backup_tags([AudioTrackItem(media, tmp_path, ".mp3", original=saved)], manifest,
                operation_id="source")
    journal = tmp_path / "bananaflow_tag_restore_resume.journal.json"
    write_journal(journal, {
        "schema": 1, "operation_id": "resume", "operation_type": "restore",
        "source_operation_id": "source", "backup_path": str(manifest),
        "batch_state": "metadata_writing", "files": {str(media): {
            "original_path": str(media), "current_path": str(media),
            "state": JournalFileState.WRITTEN,
        }},
    }, durable=True)
    monkeypatch.setattr("core.metadata_processor.restore_tags",
                        lambda *_a, **_k: pytest.fail("completed write was repeated"))
    outcomes, complete = execute_restore_recovery(journal)
    assert complete and outcomes[0].status == "unchanged"
    assert read_journal(journal)["files"][str(media)]["reconciled_from_disk"] is True


def test_restore_recovery_blocks_changed_file_identity(tmp_path: Path, monkeypatch):
    media = tmp_path / "song.mp3"
    make_empty_audio(media)
    saved = read_tags(media)
    manifest = tmp_path / "bananaflow_tag_backup_restore.json"
    backup_tags([AudioTrackItem(media, tmp_path, ".mp3", original=saved)], manifest,
                operation_id="source")
    atomic_write_tags(media, ProposedTags(title="external"), read_tags(media))
    journal = tmp_path / "bananaflow_tag_restore_conflict.journal.json"
    write_journal(journal, {
        "schema": 1, "operation_id": "resume", "operation_type": "restore",
        "source_operation_id": "source", "backup_path": str(manifest),
        "batch_state": "metadata_writing", "files": {str(media): {
            "original_path": str(media), "current_path": str(media),
            "pre_identity": {"size": 1, "mtime_ns": 1}, "state": JournalFileState.WRITTEN,
        }},
    }, durable=True)
    monkeypatch.setattr("core.metadata_processor.restore_tags",
                        lambda *_a, **_k: pytest.fail("conflicted file was written"))
    outcomes, complete = execute_restore_recovery(journal)
    assert not complete and outcomes[0].error == "file_identity_changed"


def test_recovery_detects_journal_manifest_mismatch(tmp_path: Path):
    _media, manifest, journal = _apply_journal(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["operation_id"] = "different"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    assert inspect_recovery_journal(journal)["backup_status"] == "operation_mismatch"


def test_apply_recovery_refuses_file_that_matches_neither_operation_nor_backup(tmp_path: Path):
    media = tmp_path / "song.mp3"
    make_empty_audio(media)
    original = read_tags(media)
    manifest = tmp_path / "bananaflow_tag_backup_identity.json"
    backup_tags([AudioTrackItem(media, tmp_path, ".mp3", original=original)], manifest,
                operation_id="identity-op")
    atomic_write_tags(media, ProposedTags(title="external"), original)
    journal = apply_journal_path(manifest, "identity-op")
    write_journal(journal, {
        "schema": 1, "operation_id": "identity-op", "operation_type": "apply",
        "backup_path": str(manifest), "batch_state": "applying", "files": {
            str(media): {"original_path": str(media), "intended_path": str(media),
                         "changed_fields": ["title"],
                         "expected_metadata": OriginalTags(title="applied").to_dict(),
                         "baseline_identity": {"size": 1, "mtime_ns": 1},
                         "state": JournalFileState.WRITTEN}},
    }, durable=True)
    outcomes, complete = execute_recovery(journal)
    assert not complete and outcomes[0].error == "file_identity_changed"
    assert read_tags(media).title == "external"


@pytest.fixture
def controller(tmp_path: Path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.controllers.metadata_controller import MetadataController
    QApplication.instance() or QApplication([])
    instance = MetadataController(config=None)
    instance._draft_store = DraftStore(tmp_path / "draft.json")  # noqa: SLF001
    media = tmp_path / "pending.mp3"
    media.write_bytes(b"audio")
    item = AudioTrackItem(media, tmp_path, ".mp3", original=OriginalTags(title="stored"))
    instance.workspace_state.set_tracks([item])
    item.proposed.title = "pending"
    instance.workspace_state.capture_proposals([item], ChangeOrigin.MANUAL)
    return instance, item


def test_lifecycle_cancel_preserves_workspace_and_proposals(controller):
    instance, item = controller
    called = []
    assert not instance.request_lifecycle_action("root", lambda: called.append(True))
    assert instance.resolve_unsaved_changes("cancel")
    assert not called and item.proposed.title == "pending"
    assert instance.workspace_state.change_set.records()


def test_lifecycle_keep_draft_is_verified_before_continuation(controller):
    instance, _item = controller
    called = []
    instance.request_lifecycle_action("root", lambda: called.append(True))
    assert instance.resolve_unsaved_changes("keep_draft")
    metadata, snapshot = instance._draft_store.load()  # noqa: SLF001
    assert called == [True] and metadata["targets"] and snapshot.records


def test_lifecycle_discard_clears_only_current_proposals(controller):
    instance, item = controller
    called = []
    instance.request_lifecycle_action("restore", lambda: called.append(True))
    assert instance.resolve_unsaved_changes("discard")
    assert called == [True] and not instance.workspace_state.change_set.records()
    assert item.proposed.title is None and item.path.exists()


def test_lifecycle_apply_uses_normal_apply_entrypoint(controller, monkeypatch):
    instance, _item = controller
    calls = []
    monkeypatch.setattr(instance, "apply_changes", lambda: calls.append("apply"))
    instance.request_lifecycle_action("close", lambda: calls.append("close"))
    assert instance.resolve_unsaved_changes("apply")
    assert calls == ["apply"]


def test_clean_completed_journal_does_not_trigger_startup_recovery(tmp_path: Path):
    journal = tmp_path / "bananaflow_tag_restore_clean.journal.json"
    write_journal(journal, {"schema": 1, "operation_id": "clean",
                            "operation_type": "restore", "batch_state": "completed",
                            "files": {}}, durable=True)
    assert find_incomplete_journals(tmp_path) == []


def test_shutdown_waits_for_every_active_metadata_worker(controller):
    instance, _item = controller
    class Worker:
        def __init__(self): self.running, self.cancelled = True, False
        def isRunning(self): return self.running
        def cancel(self): self.cancelled = True
        class Finished:
            def connect(self, _slot): pass
        finished = Finished()
    apply, restore = Worker(), Worker()
    instance._apply_worker, instance._restore_worker = apply, restore  # noqa: SLF001
    assert not instance.request_shutdown()
    assert apply.cancelled and restore.cancelled and instance.has_active_shutdown_work()
