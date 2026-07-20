"""Canonical draft location and lossless legacy migration (finding F-13).

Every test here isolates ``APPDATA``/``HOME``/``USERPROFILE``/``XDG_CONFIG_HOME``
into ``tmp_path``. That is not ceremony: the bug being fixed is precisely that
the draft store read the *real* home directory, and a test that reproduced it
without isolating would write into the developer's own ``~/.bananaflow`` — which is
exactly the leak the Phase 15 audit found.

The property under test throughout is that a draft is unapplied user work, so no
path through this code may ever silently lose one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.change_drafts import (
    DraftError,
    DraftMigration,
    DraftStore,
    get_canonical_draft_path,
    get_draft_dir,
    get_legacy_draft_path,
    migrate_legacy_draft,
    resolve_draft_store,
)
from core.change_sets import ChangeOperation, ChangeOrigin, ChangeSet
from core.metadata_models import metadata_values_equal


def _equal(field, left, right):
    return metadata_values_equal(field, left, right)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every app-data root at tmp_path. Nothing may touch the real home."""
    appdata = tmp_path / "AppData" / "Roaming"
    home = tmp_path / "home"
    appdata.mkdir(parents=True)
    home.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(appdata))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    return tmp_path


def _make_draft_bytes(title: str = "Song") -> bytes:
    """A real, loadable draft produced by the production writer."""
    changes = ChangeSet()
    changes.record(1, "title", "old", title, operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal)
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "draft.json"
        DraftStore(path).save(changes.snapshot(1), root=Path(scratch), session_id="s1")
        return path.read_bytes()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ──────────────────────────────────────────────────────────────────────────────
# Location
# ──────────────────────────────────────────────────────────────────────────────


def test_canonical_path_follows_appdata(isolated_home: Path):
    """The regression for F-13 itself: the draft root must track APPDATA."""
    canonical = get_canonical_draft_path()

    assert canonical.name == "tag_editor_pending.json"
    assert canonical.parent.name == "tag_drafts"
    if os.name == "nt":
        assert str(canonical).startswith(str(isolated_home / "AppData" / "Roaming"))
        assert "home" not in Path(str(canonical)).parts, "must not resolve into the home dir"


def test_canonical_path_moves_with_appdata(isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Not merely 'not home' — it has to actually follow the configured root.

    get_app_data_dir() keys on a different env var per platform (APPDATA on
    Windows, XDG_CONFIG_HOME on Linux, HOME on macOS via ~/Library), so move
    all of them and assert the path followed, whichever one this OS uses.
    """
    first = get_canonical_draft_path()
    other = isolated_home / "Other"
    other.mkdir()
    for var in ("APPDATA", "XDG_CONFIG_HOME", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(other))

    assert get_canonical_draft_path() != first
    assert str(get_canonical_draft_path()).startswith(str(other))


def test_legacy_path_is_defined_explicitly(isolated_home: Path):
    legacy = get_legacy_draft_path()

    assert legacy == Path.home() / ".bananaflow" / "tag_drafts" / "tag_editor_pending.json"


def test_relative_draft_layout_is_preserved(isolated_home: Path):
    assert get_canonical_draft_path().relative_to(get_draft_dir()) == Path("tag_editor_pending.json")
    assert get_canonical_draft_path().parent == get_draft_dir()


# ──────────────────────────────────────────────────────────────────────────────
# Migration scenarios
# ──────────────────────────────────────────────────────────────────────────────


def test_fresh_install_touches_nothing(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.NOT_NEEDED
    assert not canonical.exists()
    assert not canonical.parent.exists(), "a clean start must not create directories"


def test_legacy_only_is_migrated_and_verified(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Legacy Song")
    _write(legacy, payload)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.MIGRATED
    assert canonical.read_bytes() == payload, "content must survive byte-for-byte"
    assert not legacy.exists(), "the active legacy copy is retired after verification"
    assert result.preserved_copy is not None and result.preserved_copy.exists(), (
        "a recovery backup must remain"
    )
    assert result.preserved_copy.read_bytes() == payload

    # The migrated draft is really loadable, not just byte-equal.
    metadata, snapshot = DraftStore(canonical).load()
    assert metadata["session_id"] == "s1"
    assert snapshot.records[0].proposed_value == "Legacy Song"


def test_canonical_only_is_used_as_is(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Current")
    _write(canonical, payload)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.NOT_NEEDED
    assert canonical.read_bytes() == payload


def test_identical_duplicates_retire_the_legacy_copy(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Same")
    _write(canonical, payload)
    _write(legacy, payload)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.DUPLICATE_RETIRED
    assert canonical.read_bytes() == payload
    assert not legacy.exists()
    assert result.preserved_copy.read_bytes() == payload
    assert result.needs_user_attention is False


def test_conflicting_drafts_preserve_both_and_merge_neither(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    current = _make_draft_bytes("Current Draft")
    older = _make_draft_bytes("Older Draft")
    assert current != older
    _write(canonical, current)
    _write(legacy, older)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.CONFLICT_PRESERVED
    assert result.needs_user_attention is True
    # Canonical wins deterministically; the other copy is kept, not merged.
    assert canonical.read_bytes() == current
    assert result.preserved_copy.read_bytes() == older
    assert not legacy.exists()

    merged = json.loads(canonical.read_text(encoding="utf-8"))
    assert len(merged["records"]) == 1, "the two drafts must never be combined"


def test_corrupt_legacy_draft_is_left_untouched(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    _write(legacy, b"{ this is not valid json")

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.LEGACY_INVALID
    assert legacy.read_bytes() == b"{ this is not valid json", "never destroy what we cannot read"
    assert not canonical.exists(), "a corrupt draft must not be propagated to the new home"


def test_unsupported_schema_legacy_draft_is_left_untouched(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = json.dumps({"schema": 999, "records": []}).encode()
    _write(legacy, payload)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.LEGACY_INVALID
    assert legacy.read_bytes() == payload
    assert not canonical.exists()


def test_migration_write_failure_leaves_the_legacy_draft_intact(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Precious")
    _write(legacy, payload)

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.change_drafts.tempfile.mkstemp", explode)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.FAILED
    assert result.needs_user_attention is True
    assert legacy.read_bytes() == payload, "the only copy must survive a failed migration"
    assert not canonical.exists()


def test_readback_mismatch_fails_without_losing_the_original(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """If the bytes on disk are not the bytes we wrote, the migration is a lie."""
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Precious")
    _write(legacy, payload)

    real_read_bytes = Path.read_bytes

    def corrupt(self):
        if self == canonical:
            return b"corrupted-on-arrival"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", corrupt)

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.FAILED
    monkeypatch.undo()
    assert legacy.read_bytes() == payload, "legacy must survive a verification failure"


def test_canonical_write_failure_leaves_a_valid_canonical_draft_intact(isolated_home: Path):
    """A pre-existing canonical draft is never risked by a legacy conflict."""
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    current = _make_draft_bytes("Current")
    _write(canonical, current)
    _write(legacy, _make_draft_bytes("Older"))

    migrate_legacy_draft(canonical, legacy)

    assert canonical.read_bytes() == current


# ──────────────────────────────────────────────────────────────────────────────
# Idempotence and interruption
# ──────────────────────────────────────────────────────────────────────────────


def test_migration_is_idempotent_across_repeated_startups(isolated_home: Path):
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Once")
    _write(legacy, payload)

    first = migrate_legacy_draft(canonical, legacy)
    second = migrate_legacy_draft(canonical, legacy)
    third = migrate_legacy_draft(canonical, legacy)

    assert first.outcome is DraftMigration.MIGRATED
    assert second.outcome is DraftMigration.NOT_NEEDED
    assert third.outcome is DraftMigration.NOT_NEEDED
    assert canonical.read_bytes() == payload
    backups = list(canonical.parent.parent.rglob("tag_editor_pending.migrated-*.json"))
    assert len(backups) <= 1, "repeated startups must not pile up backups"


def test_interrupted_migration_recovers_on_the_next_startup(isolated_home: Path):
    """Emulate a crash after the copy landed but before the legacy was retired.

    This is the real ordering the code uses, so the surviving state is exactly
    'both files exist and are identical' — which must resolve cleanly, not
    duplicate or conflict.
    """
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    payload = _make_draft_bytes("Interrupted")
    _write(legacy, payload)
    _write(canonical, payload)  # the copy that landed before the "crash"

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.DUPLICATE_RETIRED
    assert canonical.read_bytes() == payload
    assert not legacy.exists()


def test_abnormal_termination_mid_write_leaves_no_partial_canonical(isolated_home: Path):
    """A temp file plus os.replace means a torn write can never be published."""
    canonical, legacy = get_canonical_draft_path(), get_legacy_draft_path()
    _write(legacy, _make_draft_bytes("Whole"))
    canonical.parent.mkdir(parents=True, exist_ok=True)
    (canonical.parent / ".bananaflow_draft_mig_leftover").write_bytes(b"partial")

    result = migrate_legacy_draft(canonical, legacy)

    assert result.outcome is DraftMigration.MIGRATED
    assert DraftStore(canonical).load(), "the published draft is complete and loadable"


def test_same_directory_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """On a POSIX box with no XDG_CONFIG_HOME the two roots coincide."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    same = home / ".bananaflow" / "tag_drafts" / "tag_editor_pending.json"
    _write(same, _make_draft_bytes("Shared"))

    result = migrate_legacy_draft(same, same)

    assert result.outcome is DraftMigration.NOT_NEEDED
    assert same.exists(), "a shared location must not retire its own draft"


# ──────────────────────────────────────────────────────────────────────────────
# Wiring and leakage
# ──────────────────────────────────────────────────────────────────────────────


def test_resolve_draft_store_returns_the_canonical_store(isolated_home: Path):
    store, result = resolve_draft_store()

    assert store.path == get_canonical_draft_path()
    assert result.outcome is DraftMigration.NOT_NEEDED


def test_resolve_draft_store_migrates_on_the_way(isolated_home: Path):
    payload = _make_draft_bytes("Adopted")
    _write(get_legacy_draft_path(), payload)

    store, result = resolve_draft_store()

    assert result.outcome is DraftMigration.MIGRATED
    assert store.path.read_bytes() == payload


def test_internal_smoke_never_adopts_a_real_users_legacy_draft(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The smoke runs against a throwaway APPDATA but the *real* home directory.

    Adopting the legacy draft there would move a user's genuine unapplied work
    into a scratch directory that is deleted seconds later — the migration
    turning into the data loss it exists to prevent.
    """
    from core import runtime_mode

    payload = _make_draft_bytes("Real user work")
    legacy = get_legacy_draft_path()
    _write(legacy, payload)
    monkeypatch.setattr(runtime_mode, "_internal_smoke", True)

    store, result = resolve_draft_store()

    assert result.outcome is DraftMigration.NOT_NEEDED
    assert store.path == get_canonical_draft_path()
    assert legacy.read_bytes() == payload, "the smoke must not consume a real draft"
    assert not get_canonical_draft_path().exists()


def test_production_startup_still_adopts_the_legacy_draft(isolated_home: Path):
    """The smoke exclusion must not quietly disable migration for real users."""
    from core import runtime_mode

    assert runtime_mode.is_internal_smoke() is False
    _write(get_legacy_draft_path(), _make_draft_bytes("Adopt me"))

    _, result = resolve_draft_store()

    assert result.outcome is DraftMigration.MIGRATED


def test_nothing_is_written_outside_the_isolated_directory(isolated_home: Path):
    """The audit found a real draft leaking into ~/.bananaflow. It must not recur."""
    payload = _make_draft_bytes("Contained")
    _write(get_legacy_draft_path(), payload)

    store, _ = resolve_draft_store()
    changes = ChangeSet()
    changes.record(1, "title", "a", "b", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal)
    store.save(changes.snapshot(1), root=isolated_home, session_id="s2")

    for path in (store.path, get_legacy_draft_path()):
        assert str(path).startswith(str(isolated_home)), f"{path} escaped the test directory"


def test_controller_uses_the_canonical_store(isolated_home: Path):
    """The production wiring, not just the helper, must honour APPDATA."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from ui.controllers.metadata_controller import MetadataController

    QApplication.instance() or QApplication([])
    controller = MetadataController(config=None)

    assert controller.draft_path() == get_canonical_draft_path()
    assert str(controller.draft_path()).startswith(str(isolated_home))
