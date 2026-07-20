"""
tests/test_apply_safety.py  –  Phase 1 Tag Editor safety hardening
====================================================================
Binding coverage for TE-SAFE-01 … TE-SAFE-13. Uses the real, minimal
audio containers from tests/audio_fixtures.py (mutagen genuinely opens,
tags, saves and re-reads them) plus targeted monkeypatch fault injection.
No mocking of the read/write path itself — a write-safety bug is caught
for real.

Worker-level tests drive the *real* MetadataApplyWorker QThread and wait
on an idle Qt event loop (not a busy poll), reflecting production
scheduling. Nothing is written outside pytest's tmp_path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.audio_fixtures import make_empty_audio

from core.metadata_models import (
    ApplyErrorCode,
    ApplyStatus,
    AudioTrackItem,
    JournalBatchState,
    JournalFileState,
    OriginalTags,
    ProposedTags,
)
import core.metadata_processor as mp
# Import the worker at module load (before any test patches metadata_processor)
# so its `from core.metadata_processor import ...` names bind to the real
# functions, not to a test's monkeypatch.
try:
    import ui.workers.metadata_worker as ww
except Exception:  # pragma: no cover - PySide6 absent
    ww = None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _item(path: Path, **proposed) -> AudioTrackItem:
    make_empty_audio(path)
    it = AudioTrackItem(
        path=path, folder=path.parent, ext=path.suffix.lower(),
        original=mp.read_tags(path),
    )
    for k, v in proposed.items():
        setattr(it.proposed, k, v)
    return it


def _run_worker(worker, timeout_ms=15000):
    """Run a QThread worker on an idle Qt event loop; return its batch result."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])

    captured = {}
    outcomes = []
    loop = QEventLoop()
    worker.file_outcome.connect(lambda o: outcomes.append(o))
    worker.finished.connect(lambda r: (captured.setdefault("result", r), loop.quit()))
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    worker.start()
    loop.exec()
    worker.wait(2000)
    assert "result" in captured, "worker never emitted finished()"
    captured["outcomes"] = outcomes
    return captured


def _make_worker(tracks, backup_path, **kw):
    if ww is None:
        pytest.skip("PySide6 not available")
    kw.setdefault("operation_id", "op" + "0" * 30)
    return ww.MetadataApplyWorker(tracks, backup_path, **kw)


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-01/02 — Backup preflight + atomic backup; abort with 0 media modified
# ══════════════════════════════════════════════════════════════════════════════

class TestBackupPreflight:

    def test_validate_backup_target_creates_and_accepts_good_dir(self, tmp_path):
        target = tmp_path / "backups"
        mp.validate_backup_target(target)
        assert target.is_dir()

    def test_validate_backup_target_rejects_path_under_a_file(self, tmp_path):
        afile = tmp_path / "afile"
        afile.write_text("x", encoding="utf-8")
        with pytest.raises(mp.BackupTargetError):
            mp.validate_backup_target(afile / "sub")

    def test_backup_write_atomic_and_readback_validates(self, tmp_path):
        it = _item(tmp_path / "a.mp3", title="X")
        bp = tmp_path / "b" / "backup.json"
        op = mp.backup_tags([it], bp, operation_id="deadbeefcafef00d", root=tmp_path)
        assert op == "deadbeefcafef00d"
        data = json.loads(bp.read_text(encoding="utf-8"))
        from core.metadata_processor import BACKUP_SCHEMA_VERSION
        assert data["schema"] == BACKUP_SCHEMA_VERSION
        assert data["operation_id"] == "deadbeefcafef00d"
        assert data["records"][0]["original_path"] == str(it.path)

    def test_interrupted_backup_leaves_no_partial_dest(self, tmp_path, monkeypatch):
        it = _item(tmp_path / "a.mp3", title="X")
        bp = tmp_path / "backup.json"

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(mp.json, "dump", boom)
        with pytest.raises(OSError):
            mp.backup_tags([it], bp)
        assert not bp.exists(), "no partial backup file must remain"
        # no leftover temp json files either
        assert not list(tmp_path.glob(".bananaflow_json_*"))


class TestBackupFaultInjectionZeroMediaModified:
    """TE-SAFE-01: a failed backup aborts the whole batch, 0 files modified."""

    def _fixture(self, tmp_path):
        it = _item(tmp_path / "song.mp3", title="New Title")
        before = it.path.read_bytes()
        return it, before

    def test_backup_target_unusable_aborts_untouched(self, tmp_path, monkeypatch):
        it, before = self._fixture(tmp_path)

        def fail_target(_dir):
            raise mp.BackupTargetError("unwritable", "nope")

        monkeypatch.setattr(ww, "validate_backup_target", fail_target)
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        result = res["result"]
        assert result.aborted and not result.preflight_ok
        assert result.global_error_key == "meta_backup_target_failed"
        assert it.path.read_bytes() == before, "no media may be modified"
        assert result.success_count == 0

    def test_backup_write_failure_aborts_untouched(self, tmp_path, monkeypatch):
        it, before = self._fixture(tmp_path)

        def fail_backup(*a, **k):
            raise OSError("cannot write backup")

        monkeypatch.setattr("ui.workers.metadata_worker.backup_tags", fail_backup)
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        result = res["result"]
        assert result.aborted and not result.backup_ok
        assert result.global_error_key == "meta_backup_write_failed"
        assert it.path.read_bytes() == before
        # the batch abort is surfaced distinctly, NOT as a fake per-file failure
        assert result.outcomes == []


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-03 — Versioned backup schema; legacy + new loader
# ══════════════════════════════════════════════════════════════════════════════

class TestVersionedBackupLoader:

    def test_loads_new_schema2_object(self, tmp_path):
        it = _item(tmp_path / "a.mp3", title="X")
        bp = tmp_path / "b.json"
        mp.backup_tags([it], bp, root=tmp_path)
        records = mp.load_tag_backup(bp)
        assert records[0][0] == it.path

    def test_loads_legacy_schema1_list(self, tmp_path):
        bp = tmp_path / "legacy.json"
        bp.write_text(json.dumps([
            {"path": str(tmp_path / "x.mp3"), "original": {"title": "T"}},
        ]), encoding="utf-8")
        records = mp.load_tag_backup(bp)
        assert records[0][0] == tmp_path / "x.mp3"
        assert records[0][1].title == "T"

    def test_schema2_final_path_maps_completed_rename_back_to_origin(self, tmp_path):
        # A renamed file is restored to where it now lives (final_path).
        bp = tmp_path / "b.json"
        bp.write_text(json.dumps({
            "schema": 2, "operation_id": "x", "records": [
                {"original_path": str(tmp_path / "old.mp3"),
                 "intended_path": str(tmp_path / "new.mp3"),
                 "final_path": str(tmp_path / "new.mp3"),
                 "original": {"title": "Orig"}},
            ],
        }), encoding="utf-8")
        records = mp.load_tag_backup(bp)
        assert records[0][0] == tmp_path / "new.mp3"
        assert records[0][1].title == "Orig"

    def test_object_without_schema_is_rejected(self, tmp_path):
        bp = tmp_path / "bad.json"
        bp.write_text(json.dumps({"not": "a backup"}), encoding="utf-8")
        with pytest.raises(ValueError):
            mp.load_tag_backup(bp)


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-07 — Write only proposed fields; preserve everything else
# ══════════════════════════════════════════════════════════════════════════════

class TestPreservation:

    def test_changed_fields_reports_only_real_changes(self):
        orig = OriginalTags(title="A", artist="B", year="2000")
        prop = ProposedTags(title="A2", artist="B")  # artist unchanged
        assert prop.changed_fields(orig) == {"title"}

    def test_mp3_title_edit_preserves_comment_artwork_and_custom(self, tmp_path):
        from mutagen.id3 import ID3, TIT2, COMM, APIC, TXXX, Encoding
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        tags = ID3()
        tags.add(TIT2(encoding=Encoding.UTF8, text="Old"))
        tags.add(COMM(encoding=Encoding.UTF8, lang="eng", desc="", text="my note"))
        tags.add(COMM(encoding=Encoding.UTF8, lang="eng", desc="iTunNORM", text="RG"))
        tags.add(APIC(encoding=Encoding.UTF8, mime="image/png", type=3,
                      desc="cover", data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32))
        tags.add(TXXX(encoding=Encoding.UTF8, desc="MUSICBRAINZ_TRACKID", text="mbid-123"))
        tags.save(str(p))

        original = mp.read_tags(p)
        mp.atomic_write_tags(p, ProposedTags(title="New"), original)

        after = ID3(str(p))
        assert after["TIT2"].text[0] == "New"
        # the main comment survives, and so does the ReplayGain/custom COMM
        comms = {getattr(f, "desc", ""): f.text[0] for f in after.getall("COMM")}
        assert comms.get("") == "my note"
        assert comms.get("iTunNORM") == "RG"
        assert after.getall("APIC"), "embedded artwork must be preserved"
        assert after["TXXX:MUSICBRAINZ_TRACKID"].text[0] == "mbid-123"

    def test_mp3_editing_comment_preserves_other_comm_frames(self, tmp_path):
        from mutagen.id3 import ID3, COMM, Encoding
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        tags = ID3()
        tags.add(COMM(encoding=Encoding.UTF8, lang="eng", desc="", text="old note"))
        tags.add(COMM(encoding=Encoding.UTF8, lang="eng", desc="iTunNORM", text="RG"))
        tags.save(str(p))

        mp.atomic_write_tags(p, ProposedTags(comment="new note"), mp.read_tags(p))
        after = ID3(str(p))
        comms = {getattr(f, "desc", ""): f.text[0] for f in after.getall("COMM")}
        assert comms.get("") == "new note"
        assert comms.get("iTunNORM") == "RG", "the descriptive COMM must survive"

    def test_flac_title_edit_preserves_multi_value_artist(self, tmp_path):
        from mutagen.flac import FLAC
        p = tmp_path / "song.flac"
        make_empty_audio(p)
        audio = FLAC(str(p))
        audio["artist"] = ["Alice", "Bob"]
        audio["title"] = "Old"
        audio.save()

        mp.atomic_write_tags(p, ProposedTags(title="New"), mp.read_tags(p))
        after = FLAC(str(p))
        assert after["title"][0] == "New"
        assert list(after["artist"]) == ["Alice", "Bob"], "multi-value artist preserved"


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-08/12 — Atomic ordering: verify BEFORE replace
# ══════════════════════════════════════════════════════════════════════════════

class TestAtomicWrite:

    def test_write_failure_leaves_original_byte_identical_temp_deleted(self, tmp_path, monkeypatch):
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        mp.atomic_write_tags(p, ProposedTags(title="First"), mp.read_tags(p))
        before = p.read_bytes()

        def boom(*a, **k):
            raise RuntimeError("write blew up")

        monkeypatch.setattr(mp, "_dispatch_write", boom)
        with pytest.raises(mp.ApplyWriteError) as ei:
            mp.atomic_write_tags(p, ProposedTags(title="Second"), mp.read_tags(p))
        assert ei.value.stage == "write"
        assert p.read_bytes() == before, "original must be untouched on write failure"
        assert not list(tmp_path.glob(".bananaflow_tmp_*")), "temp copy must be deleted"

    def test_verify_failure_leaves_original_untouched(self, tmp_path, monkeypatch):
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        mp.atomic_write_tags(p, ProposedTags(title="Kept"), mp.read_tags(p))
        before = p.read_bytes()

        # A writer that does nothing: the changed field will not verify.
        monkeypatch.setattr(mp, "_dispatch_write", lambda *a, **k: None)
        with pytest.raises(mp.ApplyWriteError) as ei:
            mp.atomic_write_tags(p, ProposedTags(title="Changed"), mp.read_tags(p))
        assert ei.value.stage == "verify"
        assert p.read_bytes() == before, "verify failure must leave the original intact"
        assert mp.read_tags(p).title == "Kept"
        assert not list(tmp_path.glob(".bananaflow_tmp_*"))

    def test_successful_write_replaces_and_verifies(self, tmp_path):
        p = tmp_path / "song.flac"
        make_empty_audio(p)
        fields = mp.atomic_write_tags(p, ProposedTags(title="Final"), mp.read_tags(p))
        assert fields == ["title"]
        assert mp.read_tags(p).title == "Final"


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-10 — Rename graph preflight (collision/case/cycle/reserved/escape)
# ══════════════════════════════════════════════════════════════════════════════

class TestRenamePlanning:

    def _it(self, folder, name, target):
        it = AudioTrackItem(path=folder / name, folder=folder, ext=".mp3",
                            original=OriginalTags())
        it.proposed_filename = target
        return it

    def test_reserved_name_blocked(self, tmp_path):
        plan = mp.plan_renames([self._it(tmp_path, "a.mp3", "CON.mp3")])
        assert plan.blocked[str(tmp_path / "a.mp3")] == ApplyErrorCode.RENAME_RESERVED

    def test_invalid_chars_blocked(self, tmp_path):
        plan = mp.plan_renames([self._it(tmp_path, "a.mp3", 'bad:name?.mp3')])
        assert plan.blocked[str(tmp_path / "a.mp3")] == ApplyErrorCode.RENAME_INVALID

    def test_trailing_dot_blocked(self, tmp_path):
        plan = mp.plan_renames([self._it(tmp_path, "a.mp3", "name.mp3.")])
        assert plan.blocked[str(tmp_path / "a.mp3")] == ApplyErrorCode.RENAME_INVALID

    def test_path_escape_blocked(self, tmp_path):
        plan = mp.plan_renames([self._it(tmp_path, "a.mp3", "../evil.mp3")])
        assert plan.blocked[str(tmp_path / "a.mp3")] == ApplyErrorCode.RENAME_ESCAPE

    def test_external_collision_blocked(self, tmp_path):
        (tmp_path / "taken.mp3").write_bytes(b"")
        plan = mp.plan_renames([self._it(tmp_path, "a.mp3", "taken.mp3")])
        assert plan.blocked[str(tmp_path / "a.mp3")] == ApplyErrorCode.RENAME_COLLISION

    def test_duplicate_targets_block_both(self, tmp_path):
        plan = mp.plan_renames([
            self._it(tmp_path, "a.mp3", "same.mp3"),
            self._it(tmp_path, "b.mp3", "same.mp3"),
        ])
        assert plan.blocked[str(tmp_path / "a.mp3")] == ApplyErrorCode.RENAME_COLLISION
        assert plan.blocked[str(tmp_path / "b.mp3")] == ApplyErrorCode.RENAME_COLLISION

    def test_swap_cycle_sequenced_with_temp_hops(self, tmp_path):
        plan = mp.plan_renames([
            self._it(tmp_path, "A.mp3", "B.mp3"),
            self._it(tmp_path, "B.mp3", "A.mp3"),
        ])
        assert not plan.blocked, "a valid swap must not be blocked"
        # more steps than pairs ⇒ a temp hop was inserted
        assert len(plan.steps) == 3
        assert plan.final[str(tmp_path / "A.mp3")] == tmp_path / "B.mp3"
        assert plan.final[str(tmp_path / "B.mp3")] == tmp_path / "A.mp3"

    def test_swap_cycle_executes_correctly_on_disk(self, tmp_path):
        a = tmp_path / "A.mp3"; b = tmp_path / "B.mp3"
        a.write_bytes(b"AAA"); b.write_bytes(b"BBB")
        plan = mp.plan_renames([
            self._it(tmp_path, "A.mp3", "B.mp3"),
            self._it(tmp_path, "B.mp3", "A.mp3"),
        ])
        for src, dst in plan.steps:
            os.replace(str(src), str(dst))
        assert a.read_bytes() == b"BBB"
        assert b.read_bytes() == b"AAA"


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-04/05 — Rename accounting: failed/blocked rename ⇒ PARTIAL, preserved
# ══════════════════════════════════════════════════════════════════════════════

class TestRenameAccounting:

    def test_successful_rename_reports_final_path(self, tmp_path):
        it = _item(tmp_path / "old.mp3", title="T")
        it.proposed_filename = "new.mp3"
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        oc = res["outcomes"][0]
        assert oc.status == ApplyStatus.SUCCESS
        assert oc.final_path == tmp_path / "new.mp3"
        assert (tmp_path / "new.mp3").exists()
        assert not (tmp_path / "old.mp3").exists()

    def test_blocked_rename_aborts_batch_and_preserves_proposal(self, tmp_path, monkeypatch):
        (tmp_path / "taken.mp3").write_bytes(b"")
        it = _item(tmp_path / "old.mp3", title="T")
        it.proposed_filename = "taken.mp3"          # external collision
        before = it.path.read_bytes()
        calls = {"metadata": 0, "rename": 0}
        real_write = ww.atomic_write_tags
        def counted_write(*args, **kwargs):
            calls["metadata"] += 1
            return real_write(*args, **kwargs)
        monkeypatch.setattr(ww, "atomic_write_tags", counted_write)
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        result = res["result"]
        assert result.aborted and not result.preflight_ok
        assert result.global_error_key == "meta_apply_blocked_title"
        assert result.outcomes == []
        assert calls == {"metadata": 0, "rename": 0}
        assert it.path.read_bytes() == before
        # No reconciliation: both proposal tokens are still pending.
        assert it.proposed.title == "T"
        assert it.proposed_filename == "taken.mp3"
        assert (tmp_path / "old.mp3").exists(), "the source was not renamed"

    def test_rename_never_overwrites_an_existing_destination(self, tmp_path, monkeypatch):
        """A cycle swap where one member's tag write fails must never let a
        later rename clobber a real file (no data loss)."""
        a = _item(tmp_path / "A.mp3", title="TA")
        b = _item(tmp_path / "B.mp3", title="TB")
        a.proposed_filename = "B.mp3"
        b.proposed_filename = "A.mp3"
        a_before = a.path.read_bytes()

        real = mp._dispatch_write
        calls = {"n": 0}

        def fail_first(path, *a_, **k):
            calls["n"] += 1
            if calls["n"] == 1:      # A.mp3 write fails
                raise RuntimeError("A write fails")
            return real(path, *a_, **k)

        monkeypatch.setattr(mp, "_dispatch_write", fail_first)
        res = _run_worker(_make_worker([a, b], tmp_path / "bk" / "b.json"))
        # A stays an untouched original; nothing was overwritten.
        assert (tmp_path / "A.mp3").exists()
        assert (tmp_path / "A.mp3").read_bytes() == a_before
        # No outcome is a plain SUCCESS-with-rename that lost data.
        statuses = {str(o.original_path): o.status for o in res["outcomes"]}
        assert statuses[str(tmp_path / "A.mp3")] == ApplyStatus.FAILED

    def test_metadata_write_failure_never_counts_as_success(self, tmp_path, monkeypatch):
        it = _item(tmp_path / "song.mp3", title="New")
        before = it.path.read_bytes()
        monkeypatch.setattr("core.metadata_processor._dispatch_write",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        result = res["result"]
        assert result.success_count == 0
        assert result.failed_count == 1
        assert it.path.read_bytes() == before
        assert res["outcomes"][0].status == ApplyStatus.FAILED
        assert res["outcomes"][0].retryable is True


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-11 — Durable journal + startup recovery
# ══════════════════════════════════════════════════════════════════════════════

class TestJournal:

    def test_incomplete_journal_retained_on_failure_and_summarised(self, tmp_path, monkeypatch):
        good = _item(tmp_path / "good.mp3", title="G")
        bad = _item(tmp_path / "bad.mp3", title="B")

        bp = tmp_path / "bk" / "b.json"
        # Fail the second file's write so the batch is partial and the journal
        # is retained for recovery.
        real = mp._dispatch_write
        calls = {"n": 0}

        def failing(path, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second write fails")
            return real(path, *a, **k)

        monkeypatch.setattr(mp, "_dispatch_write", failing)
        res = _run_worker(_make_worker([good, bad], bp))
        result = res["result"]
        assert result.failed_count == 1 and result.success_count == 1

        journals = mp.find_incomplete_journals(bp.parent)
        assert journals, "an incomplete journal must be retained on partial failure"
        summary = mp.summarize_recovery(mp.read_journal(journals[0]))
        assert summary["incomplete"] >= 1
        assert summary["backup_path"] == str(bp)

    def test_clean_batch_removes_journal(self, tmp_path):
        it = _item(tmp_path / "song.mp3", title="Clean")
        bp = tmp_path / "bk" / "b.json"
        _run_worker(_make_worker([it], bp))
        assert not mp.find_incomplete_journals(bp.parent), (
            "a fully-successful batch must not leave a recovery journal"
        )

    def test_journal_write_read_roundtrip_is_atomic(self, tmp_path):
        jp = tmp_path / "j.journal.json"
        data = {"schema": 1, "batch_state": JournalBatchState.APPLYING,
                "operation_id": "x", "files": {}}
        mp.write_journal(jp, data)
        assert mp.read_journal(jp)["batch_state"] == JournalBatchState.APPLYING
        assert not list(tmp_path.glob(".bananaflow_json_*")), "no temp left behind"


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-09 — Operation coordination: stale results dropped
# ══════════════════════════════════════════════════════════════════════════════

class TestStaleOperation:

    def _controller(self, tmp_path, monkeypatch):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication
        from ui.controllers.metadata_controller import MetadataController
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("APPDATA", str(tmp_path))
        QApplication.instance() or QApplication([])
        return MetadataController(config=None)

    def test_finish_from_superseded_generation_is_dropped(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            from ui.workers.metadata_worker import MetadataApplyWorker
            it = _item(tmp_path / "song.mp3", title="X")
            worker = MetadataApplyWorker(
                [it], tmp_path / "bk" / "b.json",
                operation_id="op1", op_generation=0,
            )
            c._apply_worker = worker  # noqa: SLF001
            worker.finished.connect(c._on_apply_finished)  # noqa: SLF001

            # A newer scan/workspace bumps the generation.
            c._op_generation = 5  # noqa: SLF001

            fired = []
            c.apply_batch_complete.connect(lambda r: fired.append(r))
            c.apply_complete.connect(lambda *a: fired.append(a))

            from core.metadata_models import ApplyBatchResult
            # Simulate the stale worker finishing.
            worker.finished.emit(ApplyBatchResult(operation_id="op1"))
            assert fired == [], "a stale-generation finish must be dropped"
        finally:
            c.deleteLater()

    def test_current_generation_finish_is_forwarded(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            from ui.workers.metadata_worker import MetadataApplyWorker
            from core.metadata_models import ApplyBatchResult
            it = _item(tmp_path / "song.mp3", title="X")
            worker = MetadataApplyWorker(
                [it], tmp_path / "bk" / "b.json",
                operation_id="op1", op_generation=0,
            )
            c._apply_worker = worker  # noqa: SLF001
            worker.finished.connect(c._on_apply_finished)  # noqa: SLF001
            fired = []
            c.apply_batch_complete.connect(lambda r: fired.append(r))
            worker.finished.emit(ApplyBatchResult(operation_id="op1"))
            assert len(fired) == 1
        finally:
            c.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-13 — Bounded, event-loop-safe shutdown
# ══════════════════════════════════════════════════════════════════════════════

class TestBoundedShutdown:

    def _controller(self, tmp_path, monkeypatch):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication
        from ui.controllers.metadata_controller import MetadataController
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication.instance() or QApplication([])
        return MetadataController(config=None)

    def test_request_shutdown_true_when_idle(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            assert c.is_disk_op_active() is False
            assert c.request_shutdown() is True
        finally:
            c.deleteLater()

    def test_request_shutdown_defers_and_cancels_running_op(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            class FakeWorker:
                def __init__(self):
                    self.cancelled = False
                    self.finished = _Sig()
                def isRunning(self):
                    return True
                def cancel(self):
                    self.cancelled = True

            class _Sig:
                def connect(self, *a, **k):
                    pass

            fake = FakeWorker()
            c._apply_worker = fake  # noqa: SLF001
            assert c.is_disk_op_active() is True
            assert c.request_shutdown() is False, "must defer, not close, mid-op"
            assert fake.cancelled is True, "cancellation requested at safe boundary"
        finally:
            c.deleteLater()

    def test_apply_and_restore_are_mutually_exclusive(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            class Running:
                def isRunning(self):
                    return True
            c._apply_worker = Running()  # noqa: SLF001
            msgs = []
            c.status_update.connect(msgs.append)
            c.restore_from_backup([(tmp_path / "x.mp3", OriginalTags())])
            assert c._restore_worker is None, "restore must not start during apply"  # noqa: SLF001
        finally:
            c.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════
# TE-SAFE-06 — Structured results + i18n
# ══════════════════════════════════════════════════════════════════════════════

class TestStructuredResultsAndI18n:

    def test_batch_result_carries_counts_and_backup_path(self, tmp_path):
        it = _item(tmp_path / "song.mp3", title="X")
        bp = tmp_path / "bk" / "b.json"
        res = _run_worker(_make_worker([it], bp))
        result = res["result"]
        assert result.success_count == 1
        assert result.backup_path == bp
        assert result.journal_path is not None
        assert result.operation_id

    def test_outcome_carries_stage_status_fields(self, tmp_path):
        it = _item(tmp_path / "song.mp3", title="X", year="2020")
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        oc = res["outcomes"][0]
        assert oc.status == ApplyStatus.SUCCESS
        assert set(oc.fields_written) == {"title", "year"}
        assert oc.original_path == it.path

    def test_new_i18n_keys_exist_in_both_languages(self):
        from ui.i18n import TRANSLATIONS
        for key in (
            "meta_apply_blocked_title", "meta_backup_target_failed",
            "meta_backup_write_failed", "meta_apply_cancelled",
            "meta_apply_write_failed", "meta_rename_blocked", "meta_rename_failed",
            "meta_done_partial_suffix",
            "md_busy_disk_op", "md_apply_backup_aborted",
            "md_recovery_prompt_title", "md_recovery_prompt_msg",
            "md_recovery_restore_btn", "md_recovery_no_backup", "md_recovery_failed",
        ):
            assert key in TRANSLATIONS["en"], f"missing EN {key}"
            assert key in TRANSLATIONS["he"], f"missing HE {key}"


def _fail_nth_dispatch(monkeypatch, n: int):
    """Patch _dispatch_write to raise on its n-th call (1-based)."""
    real = mp._dispatch_write
    state = {"n": 0}

    def wrapper(path, *a, **k):
        state["n"] += 1
        if state["n"] == n:
            raise RuntimeError(f"dispatch #{n} fails")
        return real(path, *a, **k)

    monkeypatch.setattr(mp, "_dispatch_write", wrapper)


# ══════════════════════════════════════════════════════════════════════════════
# Defect 1 — Journal persistence is a hard contract
# ══════════════════════════════════════════════════════════════════════════════

class TestJournalIsHardPrecondition:

    def test_initial_journal_failure_aborts_with_zero_media_modified(self, tmp_path, monkeypatch):
        it = _item(tmp_path / "song.mp3", title="New Title")
        before = it.path.read_bytes()

        def boom(*a, **k):
            raise OSError("journal disk full")

        # Fail every journal write → the initial (durable) persist fails.
        monkeypatch.setattr(ww, "write_journal", boom)
        res = _run_worker(_make_worker([it], tmp_path / "bk" / "b.json"))
        result = res["result"]
        assert result.aborted and not result.preflight_ok
        assert result.global_error_key == "meta_journal_init_failed"
        assert it.path.read_bytes() == before, "no media may be modified"
        assert result.success_count == 0
        assert not mp.find_incomplete_journals(tmp_path / "bk")

    def test_transition_failure_stops_and_requires_recovery(self, tmp_path, monkeypatch):
        a = _item(tmp_path / "a.mp3", title="AA")
        b = _item(tmp_path / "b.mp3", title="BB")
        b_before = b.path.read_bytes()

        real = ww.write_journal
        calls = {"n": 0}

        def failing(path, data, **k):
            calls["n"] += 1
            # 1=init, 2=APPLYING, 3=file A's VERIFIED transition → fail here,
            # after A's media write already happened.
            if calls["n"] >= 3:
                raise OSError("journal write failed mid-batch")
            return real(path, data, **k)

        monkeypatch.setattr(ww, "write_journal", failing)
        res = _run_worker(_make_worker([a, b], tmp_path / "bk" / "b.json"))
        result = res["result"]
        assert result.recovery_required is True
        assert result.global_error_key == "meta_journal_transition_failed"
        # A's media write already happened; B was never written (stopped).
        assert mp.read_tags(tmp_path / "a.mp3").title == "AA"
        assert b.path.read_bytes() == b_before
        # The journal is NOT silently discarded.
        assert mp.find_incomplete_journals(tmp_path / "bk")


# ══════════════════════════════════════════════════════════════════════════════
# Defect 2 — Successful renames are restorable (recovery renames back)
# ══════════════════════════════════════════════════════════════════════════════

class TestRestorableRenames:

    def test_worker_records_final_path_in_retained_journal(self, tmp_path, monkeypatch):
        # A renames old→new (success); B's write fails → journal retained.
        a = _item(tmp_path / "old.mp3", title="A")
        a.proposed_filename = "new.mp3"
        b = _item(tmp_path / "b.mp3", title="B")
        _fail_nth_dispatch(monkeypatch, 2)   # B is the 2nd write
        bp = tmp_path / "bk" / "b.json"
        _run_worker(_make_worker([a, b], bp))

        journals = mp.find_incomplete_journals(bp.parent)
        assert journals
        journal = mp.read_journal(journals[0])
        rec = journal["files"][str(tmp_path / "old.mp3")]
        assert rec["final_path"] == str(tmp_path / "new.mp3")
        assert (tmp_path / "new.mp3").exists()

    def test_end_to_end_recovery_renames_back_and_restores_tags(self, tmp_path):
        """old.mp3 → new.mp3 → simulated incomplete op → recovery →
        old.mp3 exists, new.mp3 does not, original tags restored."""
        new = tmp_path / "new.mp3"
        make_empty_audio(new)
        mp.atomic_write_tags(new, ProposedTags(title="Changed Title"), mp.read_tags(new))
        assert mp.read_tags(new).title == "Changed Title"

        bp = tmp_path / "bk" / "backup.json"
        bp.parent.mkdir(parents=True)
        bp.write_text(json.dumps({
            "schema": 2, "operation_id": "op", "records": [{
                "original_path": str(tmp_path / "old.mp3"),
                "intended_path": str(new),
                "final_path": str(new),
                "original": {"title": ""},
            }],
        }), encoding="utf-8")
        jp = mp.apply_journal_path(bp, "op")
        jp.write_text(json.dumps({
            "schema": 1, "operation_id": "op", "backup_path": str(bp),
            "batch_state": JournalBatchState.APPLYING,
            "files": {str(tmp_path / "old.mp3"): {
                "original_path": str(tmp_path / "old.mp3"),
                "final_path": str(new),
                "state": JournalFileState.COMPLETE,
            }},
        }), encoding="utf-8")

        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok is True
        assert (tmp_path / "old.mp3").exists(), "file renamed back to original"
        assert not new.exists(), "the renamed file no longer exists at final path"
        assert mp.read_tags(tmp_path / "old.mp3").title == "", "original tags restored"

    def test_recovery_never_overwrites_an_existing_original_path(self, tmp_path):
        new = tmp_path / "new.mp3"
        make_empty_audio(new)
        occupied = tmp_path / "old.mp3"
        occupied.write_bytes(b"UNRELATED")

        bp = tmp_path / "bk" / "backup.json"
        bp.parent.mkdir(parents=True)
        bp.write_text(json.dumps({
            "schema": 2, "operation_id": "op", "records": [{
                "original_path": str(occupied), "final_path": str(new),
                "original": {"title": ""}}]}), encoding="utf-8")
        jp = mp.apply_journal_path(bp, "op")
        jp.write_text(json.dumps({
            "schema": 1, "operation_id": "op", "backup_path": str(bp),
            "batch_state": JournalBatchState.APPLYING,
            "files": {str(occupied): {
                "original_path": str(occupied), "final_path": str(new),
                "state": JournalFileState.COMPLETE}}}), encoding="utf-8")

        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok is False
        assert occupied.read_bytes() == b"UNRELATED", "existing original not overwritten"
        assert new.exists(), "the renamed file is left in place, not lost"


# ══════════════════════════════════════════════════════════════════════════════
# Defect 3 — Rename components are transactional (no stranded temp files)
# ══════════════════════════════════════════════════════════════════════════════

def _no_temp_files(folder: Path) -> bool:
    return not list(folder.glob(".bananaflow_rn_*")) and not list(folder.glob(".bananaflow_tmp_*"))


class TestTransactionalRenameComponents:

    def _swap(self, tmp_path):
        a = _item(tmp_path / "A.mp3", title="TA")
        b = _item(tmp_path / "B.mp3", title="TB")
        a.proposed_filename = "B.mp3"
        b.proposed_filename = "A.mp3"
        return a, b

    def test_A_write_fails_B_succeeds_no_rename_no_temp(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        _fail_nth_dispatch(monkeypatch, 1)      # A is the 1st write
        res = _run_worker(_make_worker([a, b], tmp_path / "bk" / "b.json"))
        statuses = {str(o.original_path): o.status for o in res["outcomes"]}
        assert (tmp_path / "A.mp3").exists() and (tmp_path / "B.mp3").exists()
        assert a.proposed_filename == "B.mp3" and b.proposed_filename == "A.mp3"
        assert statuses[str(tmp_path / "A.mp3")] == ApplyStatus.FAILED
        assert statuses[str(tmp_path / "B.mp3")] == ApplyStatus.PARTIAL
        assert _no_temp_files(tmp_path)

    def test_A_succeeds_B_write_fails_no_rename_no_temp(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        _fail_nth_dispatch(monkeypatch, 2)      # B is the 2nd write
        res = _run_worker(_make_worker([a, b], tmp_path / "bk" / "b.json"))
        statuses = {str(o.original_path): o.status for o in res["outcomes"]}
        assert (tmp_path / "A.mp3").exists() and (tmp_path / "B.mp3").exists()
        assert a.proposed_filename == "B.mp3" and b.proposed_filename == "A.mp3"
        assert statuses[str(tmp_path / "A.mp3")] == ApplyStatus.PARTIAL
        assert statuses[str(tmp_path / "B.mp3")] == ApplyStatus.FAILED
        assert _no_temp_files(tmp_path)

    def test_runtime_failure_after_first_temp_hop_rolls_back(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        A, B = tmp_path / "A.mp3", tmp_path / "B.mp3"
        real_replace = os.replace

        def replace(src, dst, *a_, **k):
            if str(src) == str(B) and str(dst) == str(A):   # the B→A step
                raise OSError("simulated rename failure")
            return real_replace(src, dst, *a_, **k)

        monkeypatch.setattr(os, "replace", replace)
        res = _run_worker(_make_worker([a, b], tmp_path / "bk" / "b.json"))
        assert A.exists() and B.exists()
        assert _no_temp_files(tmp_path)
        statuses = {str(o.original_path): o.status for o in res["outcomes"]}
        assert statuses[str(A)] == ApplyStatus.PARTIAL
        assert statuses[str(B)] == ApplyStatus.PARTIAL

    def test_runtime_failure_after_second_cycle_step_rolls_back(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        A, B = tmp_path / "A.mp3", tmp_path / "B.mp3"
        real_replace = os.replace

        def replace(src, dst, *a_, **k):
            if ".bananaflow_rn_" in str(src) and str(dst) == str(B):   # temp→B step
                raise OSError("simulated rename failure late")
            return real_replace(src, dst, *a_, **k)

        monkeypatch.setattr(os, "replace", replace)
        _run_worker(_make_worker([a, b], tmp_path / "bk" / "b.json"))
        assert A.exists() and B.exists(), "both originals restored after rollback"
        assert _no_temp_files(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# Defect 4 — Preserve original file permissions
# ══════════════════════════════════════════════════════════════════════════════

class TestPermissionPreservation:

    def test_read_only_file_stays_read_only_after_tagging(self, tmp_path):
        import stat as _stat
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        mp.atomic_write_tags(p, ProposedTags(title="Seed"), mp.read_tags(p))
        os.chmod(str(p), _stat.S_IREAD)
        assert not (os.stat(str(p)).st_mode & _stat.S_IWRITE), "precondition: read-only"

        mp.atomic_write_tags(p, ProposedTags(title="Changed"), mp.read_tags(p))
        assert mp.read_tags(p).title == "Changed"
        assert not (os.stat(str(p)).st_mode & _stat.S_IWRITE), (
            "the file must remain read-only after the atomic replace"
        )
        os.chmod(str(p), _stat.S_IWRITE | _stat.S_IREAD)   # allow tmp cleanup

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_writable_0644_stays_0644_after_apply(self, tmp_path):
        import stat as _stat
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        mp.atomic_write_tags(p, ProposedTags(title="Seed"), mp.read_tags(p))
        os.chmod(str(p), 0o644)
        mp.atomic_write_tags(p, ProposedTags(title="Changed"), mp.read_tags(p))
        assert _stat.S_IMODE(os.stat(str(p)).st_mode) == 0o644, (
            "a writable 0644 original must not inherit the mkstemp mode"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Defect 5 — Recovery dismissal semantics
# ══════════════════════════════════════════════════════════════════════════════

class TestRecoveryDismissal:

    def _controller(self, tmp_path, monkeypatch):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication
        from ui.controllers.metadata_controller import MetadataController
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication.instance() or QApplication([])
        return MetadataController(config=None)

    def _journal(self, tmp_path):
        jp = tmp_path / "j.journal.json"
        jp.write_text(json.dumps({"schema": 1, "operation_id": "x",
                                  "batch_state": "applying", "files": {}}),
                      encoding="utf-8")
        return jp

    def test_not_now_keeps_the_journal(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            jp = self._journal(tmp_path)
            c.keep_recovery_for_later({"journal_path": str(jp)})
            assert jp.exists(), "'Not now' must keep the journal"
        finally:
            c.deleteLater()

    def test_forget_deletes_the_journal(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            jp = self._journal(tmp_path)
            c.forget_recovery({"journal_path": str(jp)})
            assert not jp.exists(), "'Forget' must delete the journal"
        finally:
            c.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════
# Defect 6 — Bounded shutdown timeout
# ══════════════════════════════════════════════════════════════════════════════

class TestBoundedShutdownTimeout:

    def _controller(self, tmp_path, monkeypatch):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication
        from ui.controllers.metadata_controller import MetadataController
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication.instance() or QApplication([])
        return MetadataController(config=None)

    def test_timeout_keeps_app_open_and_signals(self, tmp_path, monkeypatch):
        from PySide6.QtCore import QEventLoop, QTimer
        c = self._controller(tmp_path, monkeypatch)
        try:
            monkeypatch.setattr(type(c), "_SHUTDOWN_TIMEOUT_MS", 40)

            class _F:
                def connect(self, *a, **k):
                    pass

            class Running:
                finished = _F()
                def isRunning(self):
                    return True
                def cancel(self):
                    pass

            c._apply_worker = Running()   # noqa: SLF001
            fired = []
            c.shutdown_timed_out.connect(lambda: fired.append(True))
            assert c.request_shutdown() is False    # deferred, not closed

            loop = QEventLoop()
            c.shutdown_timed_out.connect(loop.quit)
            QTimer.singleShot(2000, loop.quit)
            loop.exec()
            assert fired == [True], "bounded timeout must fire and keep app open"
            assert c._shutting_down is False        # noqa: SLF001 - re-armable
        finally:
            c.deleteLater()

    def test_second_request_does_not_duplicate(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            connects = {"n": 0}

            class _F:
                def connect(self, *a, **k):
                    connects["n"] += 1

            class Running:
                finished = _F()
                def isRunning(self):
                    return True
                def cancel(self):
                    pass

            c._apply_worker = Running()   # noqa: SLF001
            assert c.request_shutdown() is False
            assert c.request_shutdown() is False    # second call
            assert connects["n"] == 1, "must not connect the finish callback twice"
        finally:
            c.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════
# Defect 7 — Durable journal for critical transitions
# ══════════════════════════════════════════════════════════════════════════════

class TestDurableJournal:

    def test_durable_write_calls_fsync_plain_does_not(self, tmp_path, monkeypatch):
        calls = {"n": 0}
        real_fsync = os.fsync

        def counting_fsync(fd):
            calls["n"] += 1
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", counting_fsync)
        jp = tmp_path / "j.journal.json"
        mp.write_journal(jp, {"a": 1}, durable=False)
        assert calls["n"] == 0, "non-critical journal write must not fsync"
        mp.write_journal(jp, {"a": 2}, durable=True)
        assert calls["n"] >= 1, "critical journal transition must fsync"


# ══════════════════════════════════════════════════════════════════════════════
# Recovery round — seed helper for a completed-but-journalled Apply
# ══════════════════════════════════════════════════════════════════════════════

def _seed_recovery(tmp_path, moves, *, changed=True, op="op0000",
                   backup_op=None, backup=True, backup_records=None):
    """Seed the on-disk state of a completed-but-journalled Apply.

    moves: list of (original_name, final_name). Creates each `final` file (the
    post-rename current file) carrying a CHANGED title, plus a schema-2 backup
    (original tags) and a journal recording the completed rename mapping.
    """
    bk = tmp_path / "bk"
    bk.mkdir(exist_ok=True)
    records = []
    files = {}
    for orig_name, final_name in moves:
        orig = tmp_path / orig_name
        final = tmp_path / final_name
        make_empty_audio(final)
        if changed:
            mp.atomic_write_tags(final, ProposedTags(title="CHANGED"), mp.read_tags(final))
        records.append({
            "original_path": str(orig), "intended_path": str(final),
            "final_path": str(final), "original": {"title": "orig_" + orig_name},
        })
        files[str(orig)] = {
            "original_path": str(orig), "final_path": str(final),
            "state": "complete", "changed_fields": ["title"] if changed else [],
        }
    bp = bk / "backup.json"
    if backup:
        bp.write_text(json.dumps({
            "schema": 2, "operation_id": backup_op or op,
            "records": backup_records if backup_records is not None else records,
        }), encoding="utf-8")
    jp = mp.apply_journal_path(bp, op)
    jp.write_text(json.dumps({
        "schema": 1, "operation_id": op, "backup_path": str(bp),
        "batch_state": "applying", "files": files,
    }), encoding="utf-8")
    return jp, bp


# ══════════════════════════════════════════════════════════════════════════════
# Blocker 2 — Recovery rename-back supports components/chains/cycles/case-only
# ══════════════════════════════════════════════════════════════════════════════

class TestRecoveryRenameGraph:

    def _names(self, folder):
        return sorted(p.name for p in folder.iterdir() if p.suffix == ".mp3")

    def test_single_rename(self, tmp_path):
        jp, _ = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok
        assert self._names(tmp_path) == ["old.mp3"]
        assert mp.read_tags(tmp_path / "old.mp3").title == "orig_old.mp3"

    def test_two_file_swap(self, tmp_path):
        jp, _ = _seed_recovery(tmp_path, [("A.mp3", "B.mp3"), ("B.mp3", "A.mp3")])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert self._names(tmp_path) == ["A.mp3", "B.mp3"]
        assert mp.read_tags(tmp_path / "A.mp3").title == "orig_A.mp3"
        assert mp.read_tags(tmp_path / "B.mp3").title == "orig_B.mp3"
        assert _no_temp_files(tmp_path)

    def test_three_file_cycle(self, tmp_path):
        jp, _ = _seed_recovery(tmp_path, [
            ("A.mp3", "B.mp3"), ("B.mp3", "C.mp3"), ("C.mp3", "A.mp3")])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert self._names(tmp_path) == ["A.mp3", "B.mp3", "C.mp3"]
        for n in ("A.mp3", "B.mp3", "C.mp3"):
            assert mp.read_tags(tmp_path / n).title == "orig_" + n
        assert _no_temp_files(tmp_path)

    def test_rename_chain(self, tmp_path):
        # A→B and B→C : reverse to A and B, C is vacated.
        jp, _ = _seed_recovery(tmp_path, [("A.mp3", "B.mp3"), ("B.mp3", "C.mp3")])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert self._names(tmp_path) == ["A.mp3", "B.mp3"]
        assert _no_temp_files(tmp_path)

    def test_case_only_rename_restores_exact_casing(self, tmp_path):
        jp, _ = _seed_recovery(tmp_path, [("song.mp3", "Song.mp3")])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok
        assert "song.mp3" in self._names(tmp_path), "exact original casing restored"
        assert "Song.mp3" not in [p.name for p in tmp_path.iterdir()
                                  if p.name == "Song.mp3"]
        assert _no_temp_files(tmp_path)

    def test_external_collision_blocks_and_never_overwrites(self, tmp_path):
        jp, _ = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        # An unrelated file now occupies the original path.
        (tmp_path / "old.mp3").write_bytes(b"UNRELATED")
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok is False
        assert (tmp_path / "old.mp3").read_bytes() == b"UNRELATED"
        assert (tmp_path / "new.mp3").exists()
        assert _no_temp_files(tmp_path)

    def test_runtime_failure_after_temp_hop_rolls_back(self, tmp_path, monkeypatch):
        jp, _ = _seed_recovery(tmp_path, [("A.mp3", "B.mp3"), ("B.mp3", "A.mp3")])
        A, B = tmp_path / "A.mp3", tmp_path / "B.mp3"
        real_replace = os.replace

        def replace(src, dst, *a, **k):
            # Fail a non-temp step that runs after the initial temp hop.
            if ".bananaflow_rn_" not in str(src) and ".bananaflow_rn_" not in str(dst) \
                    and Path(src).exists():
                # only fail once, on a real→real move
                if getattr(replace, "_armed", True):
                    replace._armed = False
                    raise OSError("simulated recovery rename failure")
            return real_replace(src, dst, *a, **k)

        replace._armed = True
        monkeypatch.setattr(os, "replace", replace)
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok is False
        # Both files remain discoverable; no temp stranded.
        assert A.exists() and B.exists()
        assert _no_temp_files(tmp_path)
        # Journal is preserved (caller retires only on all_ok).
        assert jp.exists()


# ══════════════════════════════════════════════════════════════════════════════
# Blocker 4 — Missing/corrupt backup must never report successful recovery
# ══════════════════════════════════════════════════════════════════════════════

class TestRecoveryBackupValidation:

    def test_missing_backup_blocks_before_disk_change(self, tmp_path):
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        bp.unlink()   # backup gone
        before = (tmp_path / "new.mp3").read_bytes()
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "missing_backup"
        assert (tmp_path / "new.mp3").read_bytes() == before, "no disk change"
        assert not (tmp_path / "old.mp3").exists()

    def test_corrupt_backup_json_blocks(self, tmp_path):
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        bp.write_text("{ not json", encoding="utf-8")
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "corrupt_backup"

    def test_wrong_schema_blocks(self, tmp_path):
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        bp.write_text(json.dumps({"schema": 99, "records": []}), encoding="utf-8")
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "wrong_schema"

    def test_missing_tag_record_blocks(self, tmp_path):
        # Journal says a file changed tags, but the backup has no record for it.
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")],
                                backup_records=[])
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "missing_record"

    def test_mismatched_operation_id_blocks(self, tmp_path):
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")],
                                op="opAAAA", backup_op="opBBBB")
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "operation_mismatch"

    def test_missing_backup_operation_id_blocks(self, tmp_path):
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        raw = json.loads(bp.read_text(encoding="utf-8"))
        raw.pop("operation_id", None)
        bp.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "operation_mismatch"

    def test_legacy_list_backup_rejected_for_crash_recovery(self, tmp_path):
        jp, bp = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")])
        # A legacy schema-1 list has no operation id → invalid for crash recovery.
        bp.write_text(json.dumps([
            {"path": str(tmp_path / "old.mp3"), "original": {"title": ""}}]),
            encoding="utf-8")
        with pytest.raises(mp.RecoveryPreflightError) as ei:
            mp.execute_recovery(jp)
        assert ei.value.code == "wrong_schema"

    def test_rename_only_file_not_falsely_restored_without_tags(self, tmp_path):
        # A rename-only file (no tag change) has no required tag record; recovery
        # renames back and that is legitimately RESTORED.
        jp, _ = _seed_recovery(tmp_path, [("old.mp3", "new.mp3")], changed=False,
                               backup_records=[])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok
        assert (tmp_path / "old.mp3").exists()


# ══════════════════════════════════════════════════════════════════════════════
# Blocker 1 — Recovery is a coordinated disk operation
# ══════════════════════════════════════════════════════════════════════════════

class _FakeRunningWorker:
    def __init__(self):
        self.cancelled = False
    def isRunning(self):
        return True
    def cancel(self):
        self.cancelled = True
    class _F:
        def connect(self, *a, **k):
            pass
    finished = _F()


class TestRecoveryCoordination:

    def _controller(self, tmp_path, monkeypatch):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication
        from ui.controllers.metadata_controller import MetadataController
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        monkeypatch.setenv("HOME", str(tmp_path))
        QApplication.instance() or QApplication([])
        return MetadataController(config=None)

    def test_recovery_counts_as_disk_op(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            c._recovery_worker = _FakeRunningWorker()   # noqa: SLF001
            assert c.is_disk_op_active() is True
        finally:
            c.deleteLater()

    def test_apply_refused_during_recovery(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            c._recovery_worker = _FakeRunningWorker()   # noqa: SLF001
            it = _item(tmp_path / "s.mp3", title="X")
            c.apply_changes(backup_dir=tmp_path / "bk", tracks_to_apply=[it])
            assert c._apply_worker is None               # noqa: SLF001
        finally:
            c.deleteLater()

    def test_restore_refused_during_recovery(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            c._recovery_worker = _FakeRunningWorker()   # noqa: SLF001
            c.restore_from_backup([(tmp_path / "x.mp3", OriginalTags())])
            assert c._restore_worker is None             # noqa: SLF001
        finally:
            c.deleteLater()

    def test_scan_refused_during_recovery(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            c._recovery_worker = _FakeRunningWorker()   # noqa: SLF001
            c.scan(tmp_path, recursive=False)
            assert c._scan_worker is None                # noqa: SLF001
        finally:
            c.deleteLater()

    def test_second_recovery_refused(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            first = _FakeRunningWorker()
            c._recovery_worker = first                   # noqa: SLF001
            c.recover_from_journal_backup({"journal_path": str(tmp_path / "j.json")})
            assert c._recovery_worker is first, "must not start a second recovery"
        finally:
            c.deleteLater()

    def test_shutdown_defers_and_cancels_recovery(self, tmp_path, monkeypatch):
        c = self._controller(tmp_path, monkeypatch)
        try:
            fake = _FakeRunningWorker()
            c._recovery_worker = fake                    # noqa: SLF001
            assert c.request_shutdown() is False
            assert fake.cancelled is True
        finally:
            c.deleteLater()

    def test_stale_recovery_result_dropped(self, tmp_path, monkeypatch):
        from ui.workers.metadata_worker import MetadataRecoveryWorker
        c = self._controller(tmp_path, monkeypatch)
        try:
            worker = MetadataRecoveryWorker(tmp_path / "j.json", op_generation=0)
            c._recovery_worker = worker                  # noqa: SLF001
            worker.finished.connect(c._on_recovery_finished)   # noqa: SLF001
            c._op_generation = 9                          # noqa: SLF001 - newer workspace
            fired = []
            c.recovery_complete.connect(lambda *a: fired.append(a))
            worker.finished.emit([], True, str(tmp_path / "j.json"), "")
            assert fired == [], "a stale recovery completion must be dropped"
        finally:
            c.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════
# Blocker 3 — No journal/crash window during Apply rename execution
# ══════════════════════════════════════════════════════════════════════════════

def _fail_journal_when(monkeypatch, predicate):
    """Fail ww.write_journal when predicate(data) is True (once)."""
    real = ww.write_journal
    state = {"armed": True}

    def wrapper(path, data, **k):
        if state["armed"] and predicate(data):
            state["armed"] = False
            raise OSError("journal write failed")
        return real(path, data, **k)

    monkeypatch.setattr(ww, "write_journal", wrapper)


class TestApplyRenameWriteAhead:

    def _swap(self, tmp_path):
        a = _item(tmp_path / "A.mp3", title="TA")
        b = _item(tmp_path / "B.mp3", title="TB")
        a.proposed_filename = "B.mp3"
        b.proposed_filename = "A.mp3"
        return a, b

    def _all_discoverable(self, tmp_path, journal_path):
        # Every original file is locatable from durable state.
        assert journal_path.exists()
        journal = mp.read_journal(journal_path)
        for key, rec in journal["files"].items():
            assert mp._reconstruct_current_path(rec, journal) is not None, (
                f"{key} not discoverable from durable state")

    @staticmethod
    def _n_completed(d):
        return sum(1 for e in d.get("rename_ledger", []) if e.get("state") == "completed")

    def test_journal_failure_after_first_temp_hop(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        bp = tmp_path / "bk" / "b.json"
        # Fail once the first completed rename step has been recorded in the ledger.
        _fail_journal_when(monkeypatch, lambda d: self._n_completed(d) == 1)
        res = _run_worker(_make_worker([a, b], bp))
        assert res["result"].recovery_required is True
        self._all_discoverable(tmp_path, mp.apply_journal_path(bp, res["result"].operation_id))
        assert _no_temp_files(tmp_path)

    def test_journal_failure_after_middle_cycle_step(self, tmp_path, monkeypatch):
        a = _item(tmp_path / "A.mp3", title="TA"); a.proposed_filename = "B.mp3"
        b = _item(tmp_path / "B.mp3", title="TB"); b.proposed_filename = "C.mp3"
        cc = _item(tmp_path / "C.mp3", title="TC"); cc.proposed_filename = "A.mp3"
        bp = tmp_path / "bk" / "b.json"
        _fail_journal_when(monkeypatch, lambda d: self._n_completed(d) == 2)
        res = _run_worker(_make_worker([a, b, cc], bp))
        assert res["result"].recovery_required is True
        self._all_discoverable(tmp_path, mp.apply_journal_path(bp, res["result"].operation_id))
        assert _no_temp_files(tmp_path)

    def test_journal_failure_after_final_step_before_final_path(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        bp = tmp_path / "bk" / "b.json"
        # Fail the component-final persist (a file marked COMPLETE).
        _fail_journal_when(
            monkeypatch,
            lambda d: any(f.get("state") == "complete" for f in d.get("files", {}).values()))
        res = _run_worker(_make_worker([a, b], bp))
        assert res["result"].recovery_required is True
        self._all_discoverable(tmp_path, mp.apply_journal_path(bp, res["result"].operation_id))
        assert _no_temp_files(tmp_path)

    def test_journal_failure_during_rollback_persist(self, tmp_path, monkeypatch):
        a, b = self._swap(tmp_path)
        A, B = tmp_path / "A.mp3", tmp_path / "B.mp3"
        bp = tmp_path / "bk" / "b.json"
        real_replace = os.replace

        # Force a rename runtime failure to trigger rollback.
        def replace(src, dst, *x, **k):
            if str(src) == str(B) and str(dst) == str(A):
                raise OSError("rename fail → rollback")
            return real_replace(src, dst, *x, **k)

        monkeypatch.setattr(os, "replace", replace)
        # Then fail the durable persist that records the rollback (rolled_back) state.
        _fail_journal_when(monkeypatch, lambda d: any(
            e.get("state") == "rolled_back" for e in d.get("rename_ledger", [])))
        res = _run_worker(_make_worker([a, b], bp))
        # Files remain discoverable; nothing overwritten; no temp orphaned.
        jp = mp.apply_journal_path(bp, res["result"].operation_id)
        self._all_discoverable(tmp_path, jp)
        assert _no_temp_files(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# Blocker 1/2 — Owner-aware ledger: physical-identity crash-state recovery
# ══════════════════════════════════════════════════════════════════════════════

import re as _re


def _phys(path, token, title="CHANGED"):
    """Create a physical audio file carrying valid tags AND a unique trailing
    marker so its identity is trackable across renames and tag rewrites."""
    make_empty_audio(path)
    mp.atomic_write_tags(path, ProposedTags(title=title), mp.read_tags(path))
    with open(path, "ab") as f:
        f.write(b"\x00YTPHYS:" + token.encode() + b":")
    return path


def _phys_token(path):
    if not Path(path).exists():
        return None
    m = _re.search(rb"YTPHYS:([A-Za-z0-9_]+):", Path(path).read_bytes())
    return m.group(1).decode() if m else None


def _write_crash_journal(tmp_path, op, files, ledger, backup_records):
    bk = tmp_path / "bk"
    bk.mkdir(exist_ok=True)
    bp = bk / "backup.json"
    bp.write_text(json.dumps({"schema": 2, "operation_id": op,
                              "records": backup_records}), encoding="utf-8")
    jp = mp.apply_journal_path(bp, op)
    jp.write_text(json.dumps({
        "schema": 1, "operation_id": op, "backup_path": str(bp),
        "batch_state": "applying", "files": files, "rename_ledger": ledger,
    }), encoding="utf-8")
    return jp, bp


def _frec(path):
    return {"original_path": str(path), "changed_fields": ["title"]}


def _brec(path, token):
    return {"original_path": str(path), "original": {"title": "orig_" + token}}


def _led(owner, comp, src, dst, state, seq):
    return {"owner": str(owner), "component_id": comp, "src": str(src),
            "dst": str(dst), "state": state, "seq": seq}


def _assert_one_to_one(jp):
    """Every original record must resolve to exactly one DISTINCT current file."""
    plan = mp.plan_recovery(jp)
    currents = {}
    for it in plan["items"]:
        cur = it["current_path"]
        currents[str(it["original_path"])] = str(cur) if cur is not None else None
    resolved = [v for v in currents.values() if v is not None]
    assert len(resolved) == len(set(resolved)), (
        f"records resolved to the same physical file: {currents}")
    return currents


class TestOwnerAwareCrashStateRecovery:
    """End-to-end physical-identity recovery from real interrupted-cycle states.

    physA (marker AAA) begins at A.mp3, physB (BBB) at B.mp3. Apply swaps them
    (steps A->A.tmp, B->A, A.tmp->B). Each test seeds the exact durable ledger +
    on-disk layout at a crash point, runs Recovery, and proves each physical
    file returns to its own original path — not merely that a path is non-None.
    """

    def _paths(self, tmp_path):
        return (tmp_path / "A.mp3", tmp_path / "B.mp3",
                tmp_path / ".bananaflow_rn_fixedtmp.mp3")

    def test_crash_after_intent_before_first_hop(self, tmp_path):
        A, B, Atmp = self._paths(tmp_path)
        _phys(A, "AAA"); _phys(B, "BBB")           # nothing moved yet
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "intent", 1)]
        jp, _ = _write_crash_journal(
            tmp_path, op, {str(A): _frec(A), str(B): _frec(B)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB")])
        cur = _assert_one_to_one(jp)
        assert cur[str(A)] == str(A) and cur[str(B)] == str(B)
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert _phys_token(A) == "AAA" and _phys_token(B) == "BBB"
        assert mp.read_tags(A).title == "orig_AAA"
        assert _no_temp_files(tmp_path)

    def test_crash_after_first_hop_before_completion(self, tmp_path):
        A, B, Atmp = self._paths(tmp_path)
        _phys(Atmp, "AAA"); _phys(B, "BBB")        # physA at temp; not COMPLETED
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "intent", 1)]
        jp, _ = _write_crash_journal(
            tmp_path, op, {str(A): _frec(A), str(B): _frec(B)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB")])
        cur = _assert_one_to_one(jp)
        assert cur[str(A)] == str(Atmp), "disk inspection maps physA to the temp"
        assert cur[str(B)] == str(B)
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert _phys_token(A) == "AAA" and _phys_token(B) == "BBB"
        assert _no_temp_files(tmp_path)

    def test_crash_after_middle_cycle_step(self, tmp_path):
        A, B, Atmp = self._paths(tmp_path)
        # A->A.tmp COMPLETED, B->A COMPLETED: physA at temp, physB at A.
        _phys(Atmp, "AAA"); _phys(A, "BBB")
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "completed", 1),
                  _led(B, "comp0", B, A, "completed", 2)]
        jp, _ = _write_crash_journal(
            tmp_path, op, {str(A): _frec(A), str(B): _frec(B)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB")])
        cur = _assert_one_to_one(jp)
        assert cur[str(A)] == str(Atmp) and cur[str(B)] == str(A)
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert _phys_token(A) == "AAA", "physA back at A"
        assert _phys_token(B) == "BBB", "physB back at B"
        assert _no_temp_files(tmp_path)

    def test_completed_step_then_successful_rollback(self, tmp_path):
        A, B, Atmp = self._paths(tmp_path)
        # A->A.tmp COMPLETED then ROLLED_BACK: physA back at A, physB at B.
        _phys(A, "AAA"); _phys(B, "BBB")
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "rolled_back", 1)]
        jp, _ = _write_crash_journal(
            tmp_path, op, {str(A): _frec(A), str(B): _frec(B)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB")])
        cur = _assert_one_to_one(jp)
        assert cur[str(A)] == str(A) and cur[str(B)] == str(B)
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok
        assert _phys_token(A) == "AAA" and _phys_token(B) == "BBB"
        assert _no_temp_files(tmp_path)

    def test_unresolved_ledger_entry_is_not_guessed(self, tmp_path):
        A, B, Atmp = self._paths(tmp_path)
        # Rollback failed → owner A UNRESOLVED (physA could be at A or temp).
        _phys(Atmp, "AAA"); _phys(B, "BBB")
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "unresolved", 1)]
        jp, _ = _write_crash_journal(
            tmp_path, op, {str(A): _frec(A), str(B): _frec(B)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB")])
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok is False, "an unresolved owner must not report success"
        assert _phys_token(Atmp) == "AAA", "unresolved file was not tag-written"
        assert jp.exists(), "journal retained for retry"

    def test_retry_recovery_after_interrupted_recovery(self, tmp_path):
        A, B, Atmp = self._paths(tmp_path)
        # Completed swap on disk: physA at B, physB at A.
        _phys(B, "AAA"); _phys(A, "BBB")
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "completed", 1),
                  _led(A, "comp0", Atmp, B, "completed", 2),
                  _led(B, "comp0", B, A, "completed", 3)]
        jp, _ = _write_crash_journal(
            tmp_path, op, {str(A): _frec(A), str(B): _frec(B)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB")])

        class _Cancel:
            def __init__(self): self._c = False
            def set(self): self._c = True
            def is_set(self): return self._c

        cancel = _Cancel(); cancel.set()   # cancel → rename phase runs, tags skip
        _o1, ok1 = mp.execute_recovery(jp, cancel_event=cancel)
        assert ok1 is False, "cancelled recovery is not complete"
        assert _phys_token(A) == "AAA" and _phys_token(B) == "BBB"

        _o2, ok2 = mp.execute_recovery(jp)   # retry, no cancel → finishes
        assert ok2 is True, [(o.path.name, o.status, o.error) for o in _o2]
        assert _phys_token(A) == "AAA" and _phys_token(B) == "BBB"
        assert mp.read_tags(A).title == "orig_AAA"
        assert mp.read_tags(B).title == "orig_BBB"
        assert _no_temp_files(tmp_path)

    def test_three_file_cycle_crash_mid_sequence(self, tmp_path):
        A, B, C = tmp_path / "A.mp3", tmp_path / "B.mp3", tmp_path / "C.mp3"
        Atmp = tmp_path / ".bananaflow_rn_cyc.mp3"
        # Cycle A->B, B->C, C->A. Durable: A->A.tmp COMPLETED (physA at tmp),
        # C->A COMPLETED (physC at A); physB still at B.
        _phys(Atmp, "AAA"); _phys(A, "CCC"); _phys(B, "BBB")
        op = "op0000"
        ledger = [_led(A, "comp0", A, Atmp, "completed", 1),
                  _led(C, "comp0", C, A, "completed", 2)]
        jp, _ = _write_crash_journal(
            tmp_path, op,
            {str(A): _frec(A), str(B): _frec(B), str(C): _frec(C)}, ledger,
            [_brec(A, "AAA"), _brec(B, "BBB"), _brec(C, "CCC")])
        cur = _assert_one_to_one(jp)
        assert cur[str(A)] == str(Atmp) and cur[str(B)] == str(B) and cur[str(C)] == str(A)
        outcomes, all_ok = mp.execute_recovery(jp)
        assert all_ok, [(o.path.name, o.status, o.error) for o in outcomes]
        assert _phys_token(A) == "AAA" and _phys_token(B) == "BBB" and _phys_token(C) == "CCC"
        assert _no_temp_files(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# Additional corrections — atomic recovery writes + mode restore on failed replace
# ══════════════════════════════════════════════════════════════════════════════

class TestAtomicCorrections:

    def test_recovery_tag_write_failure_leaves_file_byte_identical(self, tmp_path, monkeypatch):
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        mp.atomic_write_tags(p, ProposedTags(title="CurrentTitle"), mp.read_tags(p))
        before = p.read_bytes()
        # Recovery restores a different title, but the atomic write is forced to fail.
        monkeypatch.setattr(mp, "_dispatch_write",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        outcome = mp._restore_tags_for(p, OriginalTags(title="OriginalTitle"))
        from core.metadata_models import RestoreStatus
        assert outcome.status == RestoreStatus.FAILED
        assert p.read_bytes() == before, "recovery write failure must be byte-identical"
        assert not list(tmp_path.glob(".bananaflow_tmp_*"))

    def test_failed_replace_restores_mode_and_removes_temp(self, tmp_path, monkeypatch):
        import stat as _stat
        p = tmp_path / "song.mp3"
        make_empty_audio(p)
        mp.atomic_write_tags(p, ProposedTags(title="Seed"), mp.read_tags(p))
        os.chmod(str(p), _stat.S_IREAD)
        mode_before = _stat.S_IMODE(os.stat(str(p)).st_mode)
        before = p.read_bytes()

        real_replace = os.replace

        def replace(src, dst, *a, **k):
            if str(dst) == str(p):
                raise OSError("replace denied")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(mp.ApplyWriteError):
            mp.atomic_write_tags(p, ProposedTags(title="Changed"), mp.read_tags(p))
        assert p.read_bytes() == before, "original bytes unchanged"
        assert _stat.S_IMODE(os.stat(str(p)).st_mode) == mode_before, "mode restored"
        assert not list(tmp_path.glob(".bananaflow_tmp_*")), "temp removed"
        os.chmod(str(p), _stat.S_IWRITE | _stat.S_IREAD)
