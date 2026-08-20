"""Regression tests for event-driven history updates."""

from __future__ import annotations

import threading

from core.history_db import DownloadRecord, HistoryDB


def _record(title: str = "Track") -> DownloadRecord:
    return DownloadRecord(
        title=title,
        artist="Artist",
        url="https://example.test/item",
        output_path="C:/Downloads/item.mp3",
        media_type="audio",
        platform="youtube",
    )


def test_insert_notifies_after_commit_and_outside_db_lock():
    db = HistoryDB(":memory:")
    seen = []

    def listener(record: DownloadRecord) -> None:
        # These reads would deadlock if HistoryDB invoked listeners while its
        # non-reentrant connection lock was still held.
        seen.append((record, db.exists(record.id), db.count()))

    db.subscribe_inserts(listener)
    record_id = db.insert(_record())

    assert record_id > 0
    assert len(seen) == 1
    saved, exists, count = seen[0]
    assert saved.id == record_id
    assert saved.downloaded_at
    assert exists is True
    assert count == 1


def test_listener_failure_does_not_roll_back_or_escape_insert():
    db = HistoryDB(":memory:")

    def broken(_record: DownloadRecord) -> None:
        raise RuntimeError("cosmetic subscriber failed")

    db.subscribe_inserts(broken)
    record_id = db.insert(_record())

    assert record_id > 0
    assert db.exists(record_id)


def test_unsubscribe_stops_future_notifications():
    db = HistoryDB(":memory:")
    seen = []

    def listener(record: DownloadRecord) -> None:
        seen.append(record.id)

    unsubscribe = db.subscribe_inserts(listener)
    first = db.insert(_record("first"))
    unsubscribe()
    db.insert(_record("second"))

    assert seen == [first]


def test_background_insert_callback_can_read_database_without_deadlock():
    db = HistoryDB(":memory:")
    finished = threading.Event()
    observed = []

    def listener(record: DownloadRecord) -> None:
        observed.append(db.fetch_all(limit=1)[0].id == record.id)
        finished.set()

    db.subscribe_inserts(listener)
    thread = threading.Thread(target=lambda: db.insert(_record()), daemon=True)
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert finished.wait(0.1)
    assert observed == [True]
