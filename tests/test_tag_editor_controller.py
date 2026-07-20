"""
tests/test_tag_editor_controller.py  –  Real end-to-end Tag Editor coverage
==============================================================================
MetadataController had zero test coverage anywhere, and Phase 3's restore
tests (tests/test_tag_restore.py) exercise the pure core.metadata_processor
functions with fabricated in-memory records, not the real
scan -> apply -> backup -> restore path a user actually drives through
the panel.

This file runs the *real* QThread workers (MetadataScanWorker,
MetadataApplyWorker, MetadataRestoreWorker) against real, minimal audio
files (tests/audio_fixtures.py) inside tmp_path, and waits for their
actual completion signals rather than calling their run() methods
directly — a signal never firing, or firing with the wrong payload, is
exactly the kind of bug a full mock would hide.

Safety is asserted explicitly: restoring from a backup only ever
changes tag content, never file paths (§2 of the Phase 4 brief) — the
same set of file paths exists before and after, nothing is renamed,
moved, or deleted.
"""

from __future__ import annotations

import os

import pytest

from tests.audio_fixtures import make_empty_audio


def _make_controller(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from ui.controllers.metadata_controller import MetadataController
    except ImportError:
        pytest.skip("PySide6 not available in this environment")

    app = QApplication.instance() or QApplication([])
    controller = MetadataController(config=None)
    return app, QTest, controller


def _wait_until(qtest, predicate, timeout_ms=15000) -> None:
    """Pump the Qt event loop until predicate() is true or timeout.

    The default ceiling is generous (15s) on purpose: these conditions gate on a
    real background apply/backup worker completing real disk I/O, and a 5s
    default flaked on loaded CI runners (observed on the windows-latest 3.10
    leg). A fast machine still returns as soon as the predicate is true — only
    the timeout ceiling changed, not the assertion.
    """
    elapsed = 0
    step = 25
    while not predicate() and elapsed < timeout_ms:
        qtest.qWait(step)
        elapsed += step
    assert predicate(), f"condition not met within {timeout_ms}ms"


class TestScanBehavior:

    def test_scan_discovers_real_supported_and_unsupported_files(self, tmp_path, monkeypatch):
        _app, qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            music = tmp_path / "music"
            music.mkdir()
            make_empty_audio(music / "track1.mp3")
            make_empty_audio(music / "track2.flac")
            (music / "notes.txt").write_text("not audio", encoding="utf-8")

            results = {}
            controller.scan_complete.connect(lambda r: results.update(done=r))
            controller.scan(music, recursive=True)

            _wait_until(qtest, lambda: "done" in results)
            result = results["done"]
            assert result.files_count == 2, "notes.txt must not be treated as audio"
            found_names = {t.path.name for t in result.tracks}
            assert found_names == {"track1.mp3", "track2.flac"}
        finally:
            controller.deleteLater()

    def test_scan_emits_started_before_complete(self, tmp_path, monkeypatch):
        _app, qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            music = tmp_path / "music"
            music.mkdir()
            make_empty_audio(music / "a.mp3")

            events = []
            controller.scan_started.connect(lambda: events.append("started"))
            controller.scan_complete.connect(lambda r: events.append("complete"))
            controller.scan(music, recursive=True)

            _wait_until(qtest, lambda: "complete" in events)
            assert events[0] == "started"
        finally:
            controller.deleteLater()


class TestApplyAndBackup:

    def test_apply_changes_writes_real_tags_and_creates_backup(self, tmp_path, monkeypatch):
        from core.metadata_models import AudioTrackItem
        from core.metadata_processor import read_tags, load_tag_backup

        _app, qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            music = tmp_path / "music"
            music.mkdir()
            f = music / "song.mp3"
            make_empty_audio(f)

            item = AudioTrackItem(path=f, folder=music, ext=".mp3", original=read_tags(f))
            item.proposed.title = "New Title"
            item.proposed.year = "2021"

            backups_dir = tmp_path / "backups"
            done = {}
            controller.apply_complete.connect(
                lambda success, fail, skip: done.update(success=success, fail=fail, skip=skip)
            )
            controller.apply_changes(backup_dir=backups_dir, tracks_to_apply=[item])

            _wait_until(qtest, lambda: "success" in done)
            assert done == {"success": 1, "fail": 0, "skip": 0}

            after = read_tags(f)
            assert after.title == "New Title"
            assert after.year == "2021"

            backup_files = list(backups_dir.glob("bananaflow_tag_backup_*.json"))
            assert len(backup_files) == 1, "apply_changes must create exactly one backup file"

            records = load_tag_backup(backup_files[0])
            assert len(records) == 1
            recorded_path, recorded_original = records[0]
            assert recorded_path == f
            assert recorded_original.title == ""  # the pre-change value

        finally:
            controller.deleteLater()

    def test_apply_changes_with_no_real_changes_reports_zero_and_no_backup(
        self, tmp_path, monkeypatch
    ):
        from core.metadata_models import AudioTrackItem
        from core.metadata_processor import read_tags

        _app, qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            music = tmp_path / "music"
            music.mkdir()
            f = music / "song.mp3"
            make_empty_audio(f)
            item = AudioTrackItem(path=f, folder=music, ext=".mp3", original=read_tags(f))
            # No proposed changes at all.

            backups_dir = tmp_path / "backups"
            status = {}
            controller.status_update.connect(lambda msg: status.setdefault("msg", msg))
            controller.apply_changes(backup_dir=backups_dir, tracks_to_apply=[item])

            _wait_until(qtest, lambda: "msg" in status)
            assert not backups_dir.exists() or not list(backups_dir.glob("*.json")), (
                "apply_changes must not create a backup file when nothing has changed"
            )
        finally:
            controller.deleteLater()


class TestFilenameProposals:

    def test_rename_filename_from_title_sets_proposed_filename(
        self, tmp_path, monkeypatch
    ):
        from core.metadata_models import AudioTrackItem, OriginalTags

        _app, _qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            music = tmp_path / "music"
            music.mkdir()
            item = AudioTrackItem(
                path=music / "old name.mp3",
                folder=music,
                ext=".mp3",
                original=OriginalTags(title="Original / Bad: Title?"),
            )
            item.proposed.title = "Clean / New: Title?"

            messages = []
            controller.status_update.connect(messages.append)

            controller.rename_filename_from_title([item])

            assert item.proposed_filename == "Clean New Title.mp3"
            assert messages
            assert "1" in messages[-1]
        finally:
            controller.deleteLater()


class TestRestoreFromBackupEndToEnd:

    def test_restore_only_changes_tags_never_paths(self, tmp_path, monkeypatch):
        """The Phase 3.1 confirmation dialog promises 'only the tags
        change - no file is deleted, moved, or renamed'. Prove it: the
        exact set of file paths on disk is identical before and after
        a real restore, and untouched files (not in the backup) survive
        too."""
        from core.metadata_models import AudioTrackItem, RestoreStatus
        from core.metadata_processor import read_tags, load_tag_backup

        _app, qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            music = tmp_path / "music"
            music.mkdir()
            f1 = music / "song1.mp3"
            f2 = music / "song2.flac"
            untouched = music / "song3.m4a"
            for p in (f1, f2, untouched):
                make_empty_audio(p)

            original1, original2 = read_tags(f1), read_tags(f2)
            item1 = AudioTrackItem(path=f1, folder=music, ext=".mp3", original=original1)
            item1.proposed.title = "Song One"
            item2 = AudioTrackItem(path=f2, folder=music, ext=".flac", original=original2)
            item2.proposed.title = "Song Two"

            backups_dir = tmp_path / "backups"
            apply_done = {}
            controller.apply_complete.connect(lambda *a: apply_done.setdefault("done", True))
            controller.apply_changes(backup_dir=backups_dir, tracks_to_apply=[item1, item2])
            # This two-file safety path performs multiple durable fsyncs. On
            # slower Windows storage it can exceed the generic 5 s single-file
            # worker allowance without being stalled.
            _wait_until(qtest, lambda: "done" in apply_done, timeout_ms=15000)

            assert read_tags(f1).title == "Song One"
            assert read_tags(f2).title == "Song Two"

            paths_before = sorted(p.name for p in music.iterdir())

            backup_file = next(backups_dir.glob("bananaflow_tag_backup_*.json"))
            records = load_tag_backup(backup_file)

            restore_done = {}
            controller.restore_complete.connect(lambda outcomes: restore_done.update(o=outcomes))
            controller.restore_from_backup(records)
            _wait_until(qtest, lambda: "o" in restore_done, timeout_ms=15000)

            paths_after = sorted(p.name for p in music.iterdir())
            assert paths_after == paths_before, "restore must not add/remove/rename any file"

            assert read_tags(f1).title == "", "restore must write the original (empty) title back"
            assert read_tags(f2).title == ""
            assert untouched.exists(), "a file never in the backup must be left alone"

            outcomes = restore_done["o"]
            assert {o.status for o in outcomes} == {RestoreStatus.RESTORED}
        finally:
            controller.deleteLater()

    def test_restore_summary_reports_counts_in_status_message(self, tmp_path, monkeypatch):
        from core.metadata_models import RestoreOutcome, RestoreStatus

        _app, qtest, controller = _make_controller(tmp_path, monkeypatch)
        try:
            summaries = []
            controller.status_update.connect(summaries.append)
            outcomes = [
                RestoreOutcome(path=tmp_path / "a.mp3", status=RestoreStatus.RESTORED),
                RestoreOutcome(path=tmp_path / "b.mp3", status=RestoreStatus.MISSING),
                RestoreOutcome(path=tmp_path / "c.mp3", status=RestoreStatus.FAILED, error="boom"),
            ]
            controller._on_restore_finished(outcomes)

            assert summaries, "restore completion must emit a status_update message"
            msg = summaries[-1]
            # md_restore_done's EN template is "... {restored} restored,
            # {unchanged} already matched, {missing} missing, {fail} failed"
            assert "1" in msg and "restored" in msg.lower()
        finally:
            controller.deleteLater()

    def test_missing_backup_file_raises_cleanly(self, tmp_path, monkeypatch):
        """A backup path that no longer exists (e.g. deleted between the
        file dialog opening and the user confirming) must fail loudly —
        the panel's broad except-clause turns this into the localized
        'invalid backup' dialog — never silently return 'nothing to
        restore'."""
        from core.metadata_processor import load_tag_backup

        missing = tmp_path / "does_not_exist.json"
        with pytest.raises(OSError):
            load_tag_backup(missing)

    def test_corrupt_json_backup_raises_cleanly(self, tmp_path):
        from core.metadata_processor import load_tag_backup
        import json

        bad = tmp_path / "corrupt.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_tag_backup(bad)
