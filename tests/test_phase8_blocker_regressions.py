"""Focused production-path regressions for the Phase 8 corrective blockers."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import core.metadata_processor as mp
from core.metadata_models import AudioTrackItem, ApplyOutcome, ApplyStatus
from core.operation_manifest import ManifestError, finalize_manifest
from tests.audio_fixtures import make_empty_audio
from tests.test_apply_safety import _make_worker, _run_worker
import ui.workers.metadata_worker as ww


def _track(path: Path, *, title: str = "proposed") -> AudioTrackItem:
    make_empty_audio(path)
    item = AudioTrackItem(path=path, folder=path.parent, ext=".mp3",
                          original=mp.read_tags(path))
    item.proposed.title = title
    return item


@pytest.mark.parametrize("kind", ["occupied", "batch", "owner", "case"])
def test_late_rename_conflict_aborts_whole_apply_before_every_write(tmp_path: Path, monkeypatch, kind: str):
    """B1.1-B1.4: active worker gates tag writes on its final fresh plan."""
    if kind == "case" and not mp._dir_is_case_insensitive(str(tmp_path)):
        pytest.skip(
            "a case-only rename only collides with an independently-existing "
            "file on a case-folding filesystem; on a case-sensitive one "
            "'OCCUPIED.MP3' and 'occupied.mp3' are different, unrelated files, "
            "so there is no collision to gate on here. The 'occupied'/'batch'/"
            "'owner' kinds below use distinct filenames and still cover "
            "collision-blocks-everything on every platform. Asked of the "
            "filesystem holding tmp_path, not inferred from sys.platform "
            "(issue #22) — a case-sensitive APFS volume or a folding exFAT "
            "mount under Linux would make a platform guess pick wrong."
        )
    first = _track(tmp_path / "source.mp3")
    first.proposed_filename = "occupied.mp3"
    tracks = [first]
    if kind == "batch":
        second = _track(tmp_path / "second.mp3", title="second proposed")
        second.proposed_filename = "occupied.mp3"
        tracks.append(second)
    elif kind == "case":
        first.proposed_filename = "OCCUPIED.MP3"
    # The review point is represented by valid proposals while all destinations
    # are absent.  The destination change happens immediately before run().
    occupied = tmp_path / ("occupied.mp3" if kind != "case" else "occupied.mp3")
    if kind == "owner":
        occupied = tmp_path / "occupied.mp3"
    occupied.write_bytes(b"independent owner")
    before = {item.path: item.path.read_bytes() for item in tracks}
    writes = {"metadata": 0, "rename": 0}
    real_write = ww.atomic_write_tags
    def counted_write(*args, **kwargs):
        writes["metadata"] += 1
        return real_write(*args, **kwargs)
    monkeypatch.setattr(ww, "atomic_write_tags", counted_write)
    def counted_rename(*args, **kwargs):
        writes["rename"] += 1
        return {"status": "ok", "failure": None}
    monkeypatch.setattr(ww, "execute_rename_component_txn", counted_rename)

    result = _run_worker(_make_worker(tracks, tmp_path / "backup.json"))["result"]

    assert result.aborted and not result.preflight_ok
    assert result.global_error_key == "meta_apply_blocked_title"
    assert result.blocked_items and result.outcomes == []
    assert writes == {"metadata": 0, "rename": 0}
    assert {item.path: item.path.read_bytes() for item in tracks} == before
    assert all(item.proposed.title for item in tracks)
    assert all(item.proposed_filename for item in tracks)


def test_late_destination_race_is_gated_at_final_worker_preflight(tmp_path: Path, monkeypatch):
    """B1.5: a deterministic hook inserts the collision after review setup."""
    item = _track(tmp_path / "source.mp3")
    item.proposed_filename = "late.mp3"
    original_plan = ww.plan_renames
    barrier_reached = []
    def plan_after_review(items):
        barrier_reached.append(True)
        (tmp_path / "late.mp3").write_bytes(b"late owner")
        return original_plan(items)
    monkeypatch.setattr(ww, "plan_renames", plan_after_review)
    calls = []
    monkeypatch.setattr(ww, "atomic_write_tags", lambda *_a, **_k: calls.append(True))
    result = _run_worker(_make_worker([item], tmp_path / "backup.json"))["result"]
    assert barrier_reached and result.aborted and not calls


def test_unchanged_rename_plan_still_writes_and_reconciles(tmp_path: Path):
    """B1.6: the new gate does not turn a safe reviewed plan into a no-op."""
    item = _track(tmp_path / "source.mp3")
    item.proposed_filename = "safe.mp3"
    result = _run_worker(_make_worker([item], tmp_path / "backup.json"))["result"]
    assert not result.aborted and result.success_count == 1
    assert (tmp_path / "safe.mp3").exists() and item.proposed.title is None


@pytest.mark.parametrize("mutator", [
    lambda raw, other: raw["records"][0]["original"].__setitem__("title", "tampered"),
    lambda raw, other: raw["records"][0].__setitem__("original_path", str(other)),
    lambda raw, other: raw["records"][0].__setitem__("final_path", str(other)),
    lambda raw, other: raw["records"][0].__setitem__("result", {"status": "forged"}),
])
def test_schema4_payload_tampering_is_rejected_before_restore_write(tmp_path: Path, monkeypatch, mutator):
    """B2.1-B2.3/B2.6: every sealed restoration field rejects before writes."""
    source = _track(tmp_path / "source.mp3", title="original")
    backup = tmp_path / "backup.json"
    mp.backup_tags([source], backup, operation_id="sealed", root=tmp_path)
    raw = json.loads(backup.read_text(encoding="utf-8"))
    other = _track(tmp_path / "unrelated.mp3", title="unrelated").path
    mutator(raw, other)
    backup.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(mp, "atomic_write_tags", lambda *_a, **_k: pytest.fail("tampered backup wrote media"))
    with pytest.raises(ManifestError):
        records = mp.load_tag_backup(backup)
        mp.restore_tags(records)


def test_schema4_missing_or_malformed_integrity_is_rejected(tmp_path: Path):
    source = _track(tmp_path / "source.mp3")
    backup = tmp_path / "backup.json"
    mp.backup_tags([source], backup, operation_id="sealed", root=tmp_path)
    raw = json.loads(backup.read_text(encoding="utf-8"))
    raw.pop("integrity")
    backup.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ManifestError): mp.load_tag_backup(backup)
    raw["integrity"] = {"algorithm": "md5", "digest": "x"}
    backup.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ManifestError): mp.load_tag_backup(backup)


def test_schema4_replaced_file_is_a_no_write_authorization_failure(tmp_path: Path, monkeypatch):
    source = _track(tmp_path / "source.mp3", title="original")
    backup = tmp_path / "backup.json"
    mp.backup_tags([source], backup, operation_id="sealed", root=tmp_path)
    finalize_manifest(backup, [ApplyOutcome(source.path, source.path, status=ApplyStatus.SUCCESS)],
                      status="completed")
    source.path.write_bytes(b"replacement")
    monkeypatch.setattr(mp, "atomic_write_tags", lambda *_a, **_k: pytest.fail("replacement was written"))
    outcome = mp.restore_tags(mp.load_tag_backup(backup))[0]
    assert outcome.status == "failed" and outcome.error == "file_identity_changed"
