from __future__ import annotations

from pathlib import Path

from core.change_drafts import DraftStore
from core.change_sets import ChangeOperation, ChangeOrigin, ChangeSet
from core.metadata_models import metadata_values_equal


def _equal(field, left, right):
    return metadata_values_equal(field, left, right)


def test_draft_round_trip_restores_proposals_only(tmp_path: Path):
    changes = ChangeSet()
    changes.record(1, "title", "stored", "proposal", operation=ChangeOperation.SET,
                   origin=ChangeOrigin.MANUAL, equal=_equal,
                   source_provider="musicbrainz", source_attribution="MusicBrainz",
                   source_url="https://musicbrainz.org/recording/r")
    changes.record(2, "filename", "old.mp3", "new.mp3", operation=ChangeOperation.RENAME,
                   origin=ChangeOrigin.FILENAME, equal=_equal)
    changes.set_excluded({2}, True)
    store = DraftStore(tmp_path / "draft.json")
    store.save(changes.snapshot(4), root=tmp_path, session_id="session")
    metadata, snapshot = store.load()
    assert metadata["session_id"] == "session"
    assert snapshot.generation == 4
    assert {record.field for record in snapshot.records} == {"title", "filename"}
    assert snapshot.excluded_ids == {2}
    restored_title = next(record for record in snapshot.records if record.field == "title")
    assert (restored_title.source_provider, restored_title.source_attribution) == ("musicbrainz", "MusicBrainz")


def test_discard_removes_only_draft_file(tmp_path: Path):
    store = DraftStore(tmp_path / "draft.json")
    store.path.write_text("{}", encoding="utf-8")
    store.discard()
    assert not store.path.exists()
