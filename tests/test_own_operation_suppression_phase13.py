"""Own-operation suppression must require real terminal identity evidence.

Suppression is the one place where the Tag Editor deliberately ignores a
filesystem event, so it may never rest on a path/kind coincidence: an external
program that touches the same file just after Apply must still be reconciled.
These tests cover the pure ledger contract *and* the production controller
wiring that has to feed it, because a correct ledger nothing calls proves
nothing.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.change_sets import FileIdentity, capture_file_identity
from core.filesystem_monitoring import (
    FilesystemEvidence, FilesystemEventKind, NormalizedFilesystemEvent,
    OwnOperationLedger, OwnOperationMatch, inspect_filesystem_path,
)
from core.metadata_models import ApplyOutcome, RestoreOutcome, RestoreStatus
from ui.controllers.metadata_controller import MetadataController


# Synthetic absolute path root for ledger keys/identities. These paths are never
# created on disk — the ledger uses them as canonical keys — but they must be
# *absolute* on the host OS, or the ledger's path normalization diverges from the
# test's literal: "C:/music/song.mp3" is absolute on Windows but a relative path
# ("C:"/"music"/…) on POSIX, which made classify() return NONE on macOS/Linux.
_ROOT = Path("C:/music") if os.name == "nt" else Path("/music")


def app():
    return QApplication.instance() or QApplication([])


def identity(path, *, size=10, mtime=20, inode=1):
    return FileIdentity(str(path), size, mtime, 1, inode)


def event(path, kind, file_identity=None, prior=None, sequence=1, exists=True):
    evidence = FilesystemEvidence(path, exists, identity=file_identity)
    return NormalizedFilesystemEvent(
        "session", 1, sequence, kind, path, prior, evidence)


def real_event(root, path, kind, prior=None, sequence=1):
    """Build an event from real current filesystem evidence, as production does."""
    return NormalizedFilesystemEvent(
        "session", 1, sequence, kind, Path(path), prior,
        inspect_filesystem_path(Path(root), Path(path)))


# ── Pure ledger contract ────────────────────────────────────────────────────


def test_apply_waits_for_result_then_matches_only_recorded_final_identity():
    path = _ROOT / "song.mp3"
    final = identity(path, size=12, mtime=21)
    ledger = OwnOperationLedger()
    ledger.begin("apply-1", "apply", path, path, original_identity=identity(path),
                 expected_kinds={FilesystemEventKind.MODIFIED})
    changed = event(path, FilesystemEventKind.MODIFIED, final)
    assert ledger.classify(changed) is OwnOperationMatch.WAIT_FOR_RESULT
    ledger.record_final_state("apply-1", path, terminal_result="success",
                              final_identity=final)
    ledger.complete("apply-1", "success")
    assert ledger.classify(changed) is OwnOperationMatch.EXPECTED


def test_terminal_result_without_recorded_identity_never_suppresses():
    """The exact Phase 13 defect: a closed operation is not evidence."""
    path = _ROOT / "song.mp3"
    ledger = OwnOperationLedger()
    ledger.begin("apply-1", "apply", path, path,
                 expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.complete("apply-1", "success")
    external = event(path, FilesystemEventKind.MODIFIED, identity(path, size=99))
    assert ledger.classify(external) is OwnOperationMatch.UNEXPECTED


def test_external_change_after_successful_apply_is_reconciled():
    path = _ROOT / "song.mp3"
    ours = identity(path, size=12, mtime=21)
    theirs = identity(path, size=44, mtime=99)
    ledger = OwnOperationLedger()
    ledger.begin("apply-1", "apply", path, path,
                 expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.record_final_state("apply-1", path, terminal_result="success",
                              final_identity=ours)
    ledger.complete("apply-1", "success")
    assert ledger.classify(event(
        path, FilesystemEventKind.MODIFIED, ours)) is OwnOperationMatch.EXPECTED
    assert ledger.classify(event(
        path, FilesystemEventKind.MODIFIED, theirs)) is OwnOperationMatch.UNEXPECTED


def test_replacement_at_same_path_after_apply_is_external():
    path = _ROOT / "song.mp3"
    ours = identity(path, size=12, mtime=21, inode=1)
    replacement = identity(path, size=12, mtime=21, inode=99)
    ledger = OwnOperationLedger()
    ledger.begin("apply", "apply", path, path,
                 expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.record_final_state("apply", path, terminal_result="success",
                              final_identity=ours)
    ledger.complete("apply", "success")
    assert ledger.classify(event(
        path, FilesystemEventKind.MODIFIED, replacement)) is OwnOperationMatch.UNEXPECTED


def test_successful_rename_correlates_remove_create_and_relocated_paths():
    old, new = _ROOT / "old.mp3", _ROOT / "new.mp3"
    final = identity(new)
    ledger = OwnOperationLedger()
    ledger.begin("rename-1", "rename", old, new, original_identity=identity(old),
                 expected_kinds={FilesystemEventKind.REMOVED,
                                 FilesystemEventKind.CREATED,
                                 FilesystemEventKind.RELOCATED})
    ledger.record_final_state("rename-1", old, terminal_result="success",
                              final_path=new, final_identity=final)
    ledger.complete("rename-1", "success")
    assert ledger.classify(event(
        new, FilesystemEventKind.RELOCATED, final, prior=old)) is OwnOperationMatch.EXPECTED
    assert ledger.classify(event(
        new, FilesystemEventKind.CREATED, final)) is OwnOperationMatch.EXPECTED
    assert ledger.classify(event(
        old, FilesystemEventKind.REMOVED, None, exists=False)) is OwnOperationMatch.EXPECTED
    # A file reappearing at the vacated source is a new external file.
    assert ledger.classify(event(
        old, FilesystemEventKind.CREATED, identity(old, inode=7))) is OwnOperationMatch.UNEXPECTED


def test_recycled_file_expects_absence_and_rejects_a_new_file():
    path = _ROOT / "song.mp3"
    ledger = OwnOperationLedger()
    ledger.begin("recycle", "recycle", path, path,
                 expected_kinds={FilesystemEventKind.REMOVED,
                                 FilesystemEventKind.CREATED})
    ledger.record_final_state("recycle", path, terminal_result="success",
                              expected_absence=True)
    ledger.complete("recycle", "success")
    assert ledger.classify(event(
        path, FilesystemEventKind.REMOVED, None, exists=False)) is OwnOperationMatch.EXPECTED
    assert ledger.classify(event(
        path, FilesystemEventKind.CREATED, identity(path))) is OwnOperationMatch.UNEXPECTED


def test_failed_and_cancelled_operations_never_suppress():
    path = _ROOT / "song.mp3"
    ledger = OwnOperationLedger()
    ledger.begin("restore", "restore", path, path,
                 expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.record_final_state("restore", path, terminal_result="failed")
    ledger.complete("restore", "failed")
    assert ledger.classify(event(
        path, FilesystemEventKind.MODIFIED, identity(path))) is OwnOperationMatch.UNEXPECTED

    other = _ROOT / "other.mp3"
    ledger.begin("apply", "apply", other, other,
                 expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.complete("apply", "cancelled")
    assert ledger.classify(event(
        other, FilesystemEventKind.MODIFIED, identity(other))) is OwnOperationMatch.UNEXPECTED


def test_partial_batch_suppresses_only_the_paths_it_proved():
    proved, unproved = _ROOT / "a.mp3", _ROOT / "b.mp3"
    proved_identity = identity(proved, size=12)
    ledger = OwnOperationLedger()
    for path in (proved, unproved):
        ledger.begin("batch", "apply", path, path,
                     expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.record_final_state("batch", proved, terminal_result="success",
                              final_identity=proved_identity)
    ledger.record_final_state("batch", unproved, terminal_result="failed")
    ledger.complete("batch", "partial")
    assert ledger.classify(event(
        proved, FilesystemEventKind.MODIFIED, proved_identity)) is OwnOperationMatch.EXPECTED
    assert ledger.classify(event(
        unproved, FilesystemEventKind.MODIFIED, identity(unproved))) is OwnOperationMatch.UNEXPECTED


def test_unexpected_external_path_is_never_suppressed():
    own, external = _ROOT / "own.mp3", _ROOT / "external.mp3"
    ledger = OwnOperationLedger()
    ledger.begin("apply", "apply", own, own,
                 expected_kinds={FilesystemEventKind.MODIFIED})
    ledger.record_final_state("apply", own, terminal_result="success",
                              final_identity=identity(own))
    ledger.complete("apply", "success")
    assert ledger.classify(event(
        external, FilesystemEventKind.MODIFIED, identity(external))) is OwnOperationMatch.NONE


def test_one_operation_tracks_multiple_files_with_independent_evidence():
    ledger = OwnOperationLedger()
    paths = [_ROOT / f"{index}.mp3" for index in range(3)]
    finals = {path: identity(path, size=index + 1) for index, path in enumerate(paths)}
    for path in paths:
        ledger.begin("batch", "apply", path, path,
                     expected_kinds={FilesystemEventKind.MODIFIED})
    assert len(ledger.entries()) == 3
    for path in paths:
        ledger.record_final_state("batch", path, terminal_result="success",
                                  final_identity=finals[path])
    ledger.complete("batch", "success")
    assert all(ledger.classify(event(path, FilesystemEventKind.MODIFIED, finals[path]))
               is OwnOperationMatch.EXPECTED for path in paths)
    # Each entry is bound to its own identity, never to a sibling's.
    assert ledger.classify(event(
        paths[0], FilesystemEventKind.MODIFIED, finals[paths[1]])) is OwnOperationMatch.UNEXPECTED


def test_ledger_capacity_is_bounded_without_time_window_suppression():
    ledger = OwnOperationLedger(maximum_entries=2, retention_seconds=600)
    for index in range(5):
        path = _ROOT / f"{index}.mp3"
        ledger.begin(str(index), "apply", path, path)
    assert len(ledger.entries()) == 2


# ── Production controller wiring ────────────────────────────────────────────


def controller(tmp_path):
    app()
    value = MetadataController()
    from core.metadata_processor import build_scan_result
    value._session.scan_result = build_scan_result(tmp_path, [], 0, {tmp_path})
    return value


def test_production_apply_records_real_final_identity_then_rejects_external_change(tmp_path):
    """Apply's own MODIFIED is ignored; a later external edit still reconciles."""
    path = tmp_path / "song.mp3"
    path.write_bytes(b"before")
    control = controller(tmp_path)
    try:
        control._begin_monitored_disk_operation("apply-1", "apply", [path])
        path.write_bytes(b"after-apply")           # the worker's own write
        outcome = ApplyOutcome(original_path=path, final_path=path, status="success")
        control._finish_monitored_disk_operation(
            "apply-1", "success",
            records=[(outcome.original_path, outcome.final_path, True, False)])

        ours = real_event(tmp_path, path, FilesystemEventKind.MODIFIED)
        assert control._own_operations.classify(ours) is OwnOperationMatch.EXPECTED

        path.write_bytes(b"changed by another program entirely")
        theirs = real_event(tmp_path, path, FilesystemEventKind.MODIFIED)
        assert control._own_operations.classify(theirs) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()


def test_production_failed_apply_outcome_leaves_event_external(tmp_path):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"before")
    control = controller(tmp_path)
    try:
        control._begin_monitored_disk_operation("apply-2", "apply", [path])
        control._finish_monitored_disk_operation(
            "apply-2", "partial", records=[(path, path, False, False)])
        assert control._own_operations.classify(real_event(
            tmp_path, path, FilesystemEventKind.MODIFIED)) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()


def test_production_restore_records_identity_and_external_edit_follows(tmp_path):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"restored")
    control = controller(tmp_path)
    try:
        control._begin_monitored_disk_operation("restore-1", "restore", [path])
        outcomes = [RestoreOutcome(path=path, status=RestoreStatus.RESTORED)]
        control._finish_monitored_disk_operation(
            "restore-1", "success",
            records=[(value.path, value.path,
                      value.status in {"restored", "unchanged"}, False)
                     for value in outcomes])
        assert control._own_operations.classify(real_event(
            tmp_path, path, FilesystemEventKind.MODIFIED)) is OwnOperationMatch.EXPECTED
        path.write_bytes(b"external edit after restore")
        assert control._own_operations.classify(real_event(
            tmp_path, path, FilesystemEventKind.MODIFIED)) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()


def test_production_rename_registers_evidence_and_binds_the_real_final_path(tmp_path):
    """Blocker 3: the guarded rename path must produce ledger evidence."""
    source = tmp_path / "old.mp3"
    source.write_bytes(b"audio")
    control = controller(tmp_path)
    try:
        result = control.rename_path(source, "new.mp3")
        destination = tmp_path / "new.mp3"
        assert result is not None and result.all_succeeded
        assert destination.exists() and not source.exists()

        entries = control._own_operations.entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.operation_type == "rename"
        assert entry.final_state_recorded and entry.terminal_result == "success"
        assert entry.expected_final_identity == capture_file_identity(destination)

        assert control._own_operations.classify(real_event(
            tmp_path, destination, FilesystemEventKind.CREATED,
            prior=source)) is OwnOperationMatch.EXPECTED
        assert control._own_operations.classify(real_event(
            tmp_path, source, FilesystemEventKind.REMOVED)) is OwnOperationMatch.EXPECTED

        destination.write_bytes(b"external edit right after our rename")
        assert control._own_operations.classify(real_event(
            tmp_path, destination, FilesystemEventKind.MODIFIED)) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()


def test_production_failed_rename_registers_no_suppression(tmp_path):
    source = tmp_path / "old.mp3"
    source.write_bytes(b"audio")
    (tmp_path / "taken.mp3").write_bytes(b"other")
    control = controller(tmp_path)
    try:
        result = control.rename_path(source, "taken.mp3")
        assert result is not None and not result.succeeded
        assert result.failed[0].error_code == "destination_exists"
        assert source.exists()
        entry = control._own_operations.entries()[0]
        assert not entry.final_state_recorded and entry.terminal_result == "failed"
        assert control._own_operations.classify(real_event(
            tmp_path, source, FilesystemEventKind.MODIFIED)) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()


def test_production_recycle_records_expected_absence(tmp_path):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"audio")
    control = controller(tmp_path)
    try:
        result = control.recycle_paths([path])
        assert result is not None and result.all_succeeded
        entry = control._own_operations.entries()[0]
        assert entry.operation_type == "recycle"
        assert entry.expected_absence and entry.final_state_recorded
        assert control._own_operations.classify(real_event(
            tmp_path, path, FilesystemEventKind.REMOVED)) is OwnOperationMatch.EXPECTED
        # A new file dropped at the freed path is not our deletion.
        path.write_bytes(b"a different file now lives here")
        assert control._own_operations.classify(real_event(
            tmp_path, path, FilesystemEventKind.CREATED)) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()


def test_production_partial_move_proves_only_the_moved_file(tmp_path):
    moved = tmp_path / "moved.mp3"
    blocked = tmp_path / "blocked.mp3"
    moved.write_bytes(b"a")
    blocked.write_bytes(b"b")
    destination = tmp_path / "target"
    destination.mkdir()
    (destination / "blocked.mp3").write_bytes(b"already here")
    control = controller(tmp_path)
    try:
        result = control.move_paths([moved, blocked], destination)
        assert result is not None
        assert len(result.succeeded) == 1 and len(result.failed) == 1
        assert (destination / "moved.mp3").exists() and blocked.exists()

        assert control._own_operations.classify(real_event(
            tmp_path, destination / "moved.mp3",
            FilesystemEventKind.CREATED)) is OwnOperationMatch.EXPECTED
        assert control._own_operations.classify(real_event(
            tmp_path, blocked, FilesystemEventKind.MODIFIED)) is OwnOperationMatch.UNEXPECTED
    finally:
        control.deleteLater()
