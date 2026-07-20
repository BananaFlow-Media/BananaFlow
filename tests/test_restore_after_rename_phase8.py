from __future__ import annotations

from pathlib import Path

from core.metadata_models import AudioTrackItem, OriginalTags, ProposedTags
from core.metadata_processor import atomic_write_tags, backup_tags, load_tag_backup, read_tags, restore_tags
from core.operation_manifest import finalize_manifest
from core.metadata_models import ApplyOutcome, ApplyStatus
from core.restore_preview import preview_restore
from tests.audio_fixtures import make_empty_audio


def test_restore_preview_finds_final_renamed_file_and_plans_path_restore(tmp_path: Path):
    original = tmp_path / "old.mp3"; original.write_bytes(b"audio")
    item = AudioTrackItem(original, tmp_path, ".mp3", original=OriginalTags(title="old"))
    item.proposed_filename = "new.mp3"
    backup = tmp_path / "bananaflow_tag_backup_test.json"; backup_tags([item], backup, operation_id="undo")
    final = tmp_path / "new.mp3"; original.rename(final)
    finalize_manifest(backup, [ApplyOutcome(original, final, status=ApplyStatus.SUCCESS)], status="completed")
    preview = preview_restore(backup)
    assert preview.found == 1 and preview.needs_path_restore == 1
    assert not preview.rename_plan.blocked


def test_actual_restore_after_rename_writes_original_metadata_at_verified_final_path(tmp_path: Path):
    original = tmp_path / "old.mp3"
    make_empty_audio(original)
    stored = read_tags(original)
    item = AudioTrackItem(original, tmp_path, ".mp3", original=stored)
    item.proposed_filename = "new.mp3"
    backup = tmp_path / "bananaflow_tag_backup_actual.json"
    backup_tags([item], backup, operation_id="apply")
    atomic_write_tags(original, ProposedTags(title="changed"), stored)
    final = tmp_path / "new.mp3"
    original.rename(final)
    finalize_manifest(backup, [ApplyOutcome(original, final, status=ApplyStatus.SUCCESS)],
                      status="completed")
    outcomes = restore_tags(load_tag_backup(backup))
    assert outcomes[0].status == "restored"
    assert read_tags(final).title == stored.title and not original.exists()
