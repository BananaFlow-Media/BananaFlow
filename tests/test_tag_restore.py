"""
tests/test_tag_restore.py  –  Phase 3 Tag Editor restore-from-backup
====================================================================
Every Apply writes a JSON backup of the original tags; Phase 3 adds the
missing other half — restoring that backup. These tests pin the core
logic (pure Python, no Qt, no real audio files):

* backup_tags → load_tag_backup round-trips paths and all nine fields,
* malformed files are rejected with ValueError before anything writes,
* restore maps each record to the right outcome: missing / unchanged /
  restored / failed,
* the ProposedTags built for the write carries the backup's values,
  including the None→-1 "clear track number" convention.

File writes are monkeypatched — write correctness for real MP3/FLAC/M4A
files stays a Phase 4 item (the Tag Editor write path itself is still
untested there too).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.metadata_models import (
    AudioTrackItem,
    OriginalTags,
    RestoreStatus,
)
from core.metadata_processor import backup_tags, load_tag_backup, restore_tags


def _item(path: Path, **tags) -> AudioTrackItem:
    return AudioTrackItem(
        path=path, folder=path.parent, ext=path.suffix.lower(),
        original=OriginalTags(**tags),
    )


class TestBackupRoundTrip:

    def test_backup_then_load_roundtrips(self, tmp_path):
        a = _item(tmp_path / "a.mp3", title="Song A", artist="Artist", track_num=3)
        b = _item(tmp_path / "b.flac", album="Album B", year="1999", genre="Rock")
        backup = tmp_path / "backup.json"

        backup_tags([a, b], backup)
        records = load_tag_backup(backup)

        assert [p for p, _ in records] == [a.path, b.path]
        assert records[0][1].title == a.original.title
        assert records[1][1].album == b.original.album
        assert records[0][1].artwork_captured and records[1][1].artwork_captured

    def test_load_rejects_non_list_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"not": "a backup"}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_tag_backup(f)

    def test_load_rejects_bad_record_shape(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text(json.dumps([{"path": 42, "original": {}}]), encoding="utf-8")
        with pytest.raises(ValueError):
            load_tag_backup(f)

    def test_load_rejects_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{ not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_tag_backup(f)

    def test_load_ignores_unknown_tag_fields(self, tmp_path):
        f = tmp_path / "forward_compat.json"
        f.write_text(json.dumps([{
            "path": str(tmp_path / "x.mp3"),
            "original": {"title": "T", "future_field": "?"},
        }]), encoding="utf-8")
        records = load_tag_backup(f)
        assert records[0][1].title == "T"


class TestRestoreOutcomes:

    def test_missing_file_is_reported_not_fatal(self, tmp_path):
        records = [(tmp_path / "gone.mp3", OriginalTags(title="X"))]
        outcomes = restore_tags(records)
        assert [o.status for o in outcomes] == [RestoreStatus.MISSING]

    def test_restores_changed_file_with_backup_values(self, tmp_path, monkeypatch):
        f = tmp_path / "song.mp3"
        f.write_bytes(b"")
        saved = OriginalTags(title="Old Title", artist="Old Artist", track_num=None)
        current = _item(f, title="New Title", artist="New Artist", track_num=7)

        written = {}

        def fake_write(path, proposed, original):
            written["path"] = path
            written["proposed"] = proposed
            written["original"] = original
            return []

        monkeypatch.setattr("core.metadata_processor.build_track_item", lambda p: current)
        monkeypatch.setattr("core.metadata_processor.atomic_write_tags", fake_write)

        outcomes = restore_tags([(f, saved)])

        assert [o.status for o in outcomes] == [RestoreStatus.RESTORED]
        assert written["path"] == f
        assert written["proposed"].title == "Old Title"
        assert written["proposed"].artist == "Old Artist"
        # backup track_num None must clear the current 7 (ProposedTags: -1 = clear)
        assert written["proposed"].track_num == -1
        effective = written["proposed"].effective_tags(written["original"])
        assert effective.title == "Old Title"
        assert effective.track_num is None

    def test_unchanged_file_is_not_written(self, tmp_path, monkeypatch):
        f = tmp_path / "same.mp3"
        f.write_bytes(b"")
        saved = OriginalTags(title="T", artist="A")
        current = _item(f, title="T", artist="A")

        monkeypatch.setattr("core.metadata_processor.build_track_item", lambda p: current)
        monkeypatch.setattr(
            "core.metadata_processor.atomic_write_tags",
            lambda *a: pytest.fail("atomic_write_tags must not run for unchanged files"),
        )

        outcomes = restore_tags([(f, saved)])
        assert [o.status for o in outcomes] == [RestoreStatus.UNCHANGED]

    def test_write_failure_is_reported_per_file(self, tmp_path, monkeypatch):
        f = tmp_path / "locked.mp3"
        f.write_bytes(b"")
        saved = OriginalTags(title="Old")
        current = _item(f, title="New")

        monkeypatch.setattr("core.metadata_processor.build_track_item", lambda p: current)
        from core.metadata_processor import ApplyWriteError
        monkeypatch.setattr("core.metadata_processor.atomic_write_tags", lambda *a: (_ for _ in ()).throw(ApplyWriteError("write", "write_failed", "no")))

        outcomes = restore_tags([(f, saved)])
        assert outcomes[0].status == RestoreStatus.FAILED
        assert outcomes[0].error

    def test_mixed_batch_keeps_per_file_results_in_order(self, tmp_path, monkeypatch):
        ok_file = tmp_path / "ok.mp3"
        ok_file.write_bytes(b"")
        gone = tmp_path / "gone.mp3"

        current = _item(ok_file, title="New")
        monkeypatch.setattr("core.metadata_processor.build_track_item", lambda p: current)
        monkeypatch.setattr("core.metadata_processor.atomic_write_tags", lambda *a: [])

        progress = []
        outcomes = restore_tags(
            [(ok_file, OriginalTags(title="Old")), (gone, OriginalTags())],
            progress_cb=lambda done, total: progress.append((done, total)),
        )
        assert [o.status for o in outcomes] == [RestoreStatus.RESTORED, RestoreStatus.MISSING]
        assert progress == [(1, 2), (2, 2)]

    def test_restore_i18n_keys_exist_in_both_languages(self):
        from ui.i18n import TRANSLATIONS
        for key in (
            "meta_restore_btn", "meta_restore_tooltip",
            "md_restoring_tags", "md_restoring_progress", "md_restore_done",
            "md_restore_summary_title", "md_restore_pick_title",
            "md_restore_invalid_title", "md_restore_invalid_msg",
            "md_restore_empty_msg", "md_restore_all_missing_msg",
            "md_restore_confirm_title", "md_restore_confirm_msg",
            "md_restore_missing_note", "md_restore_more_files",
            "md_restore_confirm_btn",
        ):
            assert key in TRANSLATIONS["en"], f"missing EN {key}"
            assert key in TRANSLATIONS["he"], f"missing HE {key}"
