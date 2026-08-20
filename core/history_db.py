"""
core/history_db.py  –  SQLite-backed download history manager
==============================================================
Responsibilities
----------------
* Persist a permanent, searchable log of every completed download.
* Provide fast read access for the HistoryPanel (last 500 records by default).
* Support full-text search, single-record deletion, bulk clear, and CSV export.
* Notify interested views after a record has committed, without importing Qt.
* Be completely UI-agnostic and safe to call from any thread.

Design decisions
----------------
* Zero GUI imports – pure stdlib only (sqlite3, threading, csv, dataclasses).
* Thread-safety via a single threading.Lock() that wraps every connection use.
  SQLite's default serialised mode handles concurrent reads, but the lock
  prevents interleaved write transactions from background download threads.
* The database connection is opened once in __init__ and kept open for the
  lifetime of the object (WAL journal mode keeps readers non-blocking).
* Insert subscribers are notified after the transaction lock is released, so a
  listener can safely read the DB; bound-method listeners are held weakly so a
  panel is not kept alive by the persistence layer.
* All SQL uses parameterised queries – no string interpolation anywhere.
* DownloadRecord is a plain dataclass: safe to pickle, queue, or copy.

Typical usage
-------------
>>> db = HistoryDB()                         # uses ~/.bananaflow/downloads.db
>>> db.insert(DownloadRecord(...))
>>> records = db.fetch_all(limit=100)
>>> results = db.search("example artist")
>>> db.export_csv("/tmp/history.csv")
>>> db.delete(record_id=42)
"""

from __future__ import annotations

import csv
import logging
import shutil
import sqlite3
import threading
import weakref
from dataclasses import dataclass, field, fields, asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

from utils.time_format import seconds_to_str as _seconds_to_str


# ──────────────────────────────────────────────────────────────────────────────
# Public data-class
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DownloadRecord:
    """
    Represents a single completed download as stored in the history database.

    Fields
    ------
    id              Auto-assigned by SQLite on INSERT; 0 means "not yet saved".
    downloaded_at   ISO-8601 UTC timestamp string, e.g. "2024-11-01T14:32:00Z".
                    Auto-filled by HistoryDB.insert() if left as empty string.
    title           Track / video title.
    artist          Uploader or artist name (may be empty for non-music content).
    url             Original source URL (YouTube watch URL, Spotify track URL, …).
    output_path     Absolute local filesystem path to the saved file.
    media_type      "audio" | "video"
    file_size_mb    Size of the output file in megabytes (None if unknown).
    duration_sec    Duration of the media in seconds (None if unknown / live).
    thumbnail_url   Remote URL of the best-quality thumbnail (may be empty).
    platform        "youtube" | "ytmusic" | "spotify" | "unknown"
    """

    title:         str
    url:           str
    output_path:   str
    media_type:    str                  # "audio" | "video"

    # Optional / auto-filled fields
    id:            int                  = 0
    downloaded_at: str                  = ""   # filled by insert() if empty
    artist:        str                  = ""
    file_size_mb:  Optional[float]      = None
    duration_sec:  Optional[int]        = None
    thumbnail_url: str                  = ""
    platform:      str                  = "unknown"

    # ── Computed helpers ──────────────────────────────────────────────────────

    def duration_str(self) -> str:
        """Return a human-readable duration string, e.g. '3:45' or '1:02:30'."""
        return _seconds_to_str(self.duration_sec, live_label="—")

    def file_size_str(self) -> str:
        """Return a human-readable file-size string, e.g. '8.3 MB'."""
        if self.file_size_mb is None:
            return "—"
        if self.file_size_mb >= 1024:
            return f"{self.file_size_mb / 1024:.2f} GB"
        return f"{self.file_size_mb:.1f} MB"

    def display_date(self) -> str:
        """Return a short local-time date string for display in the UI."""
        if not self.downloaded_at:
            return "—"
        try:
            from utils.time_format import display_timestamp
            dt = datetime.fromisoformat(self.downloaded_at.replace("Z", "+00:00"))
            return display_timestamp(dt.astimezone())
        except ValueError:
            # A stored value we cannot parse still has to render in the same
            # shape as every other date (issue #43) — slicing the raw ISO
            # string left a "2026-03-05T14:30" with a stray T in the middle of
            # a list where every neighbouring row shows "2026-03-05 14:30".
            return self.downloaded_at[:16].replace("T", " ")


# ──────────────────────────────────────────────────────────────────────────────
# Database schema
# ──────────────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    downloaded_at TEXT     NOT NULL,
    title         TEXT     NOT NULL,
    artist        TEXT     NOT NULL DEFAULT '',
    url           TEXT     NOT NULL,
    output_path   TEXT     NOT NULL,
    media_type    TEXT     NOT NULL,
    file_size_mb  REAL,
    duration_sec  INTEGER,
    thumbnail_url TEXT     NOT NULL DEFAULT '',
    platform      TEXT     NOT NULL DEFAULT 'unknown'
);
"""

# A lightweight FTS index on title + artist for fast keyword search.
_CREATE_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS downloads_fts
USING fts5(
    title,
    artist,
    content='downloads',
    content_rowid='id'
);
"""

# Triggers to keep the FTS index in sync with the main table.
_CREATE_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS downloads_ai AFTER INSERT ON downloads BEGIN
    INSERT INTO downloads_fts(rowid, title, artist)
    VALUES (new.id, new.title, new.artist);
END;

CREATE TRIGGER IF NOT EXISTS downloads_ad AFTER DELETE ON downloads BEGIN
    INSERT INTO downloads_fts(downloads_fts, rowid, title, artist)
    VALUES ('delete', old.id, old.title, old.artist);
END;

CREATE TRIGGER IF NOT EXISTS downloads_au AFTER UPDATE ON downloads BEGIN
    INSERT INTO downloads_fts(downloads_fts, rowid, title, artist)
    VALUES ('delete', old.id, old.title, old.artist);
    INSERT INTO downloads_fts(rowid, title, artist)
    VALUES (new.id, new.title, new.artist);
END;
"""

_INSERT_SQL = """
INSERT INTO downloads
    (downloaded_at, title, artist, url, output_path,
     media_type, file_size_mb, duration_sec, thumbnail_url, platform)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_FETCH_ALL_SQL = """
SELECT id, downloaded_at, title, artist, url, output_path,
       media_type, file_size_mb, duration_sec, thumbnail_url, platform
FROM   downloads
ORDER  BY id DESC
LIMIT  ?
"""

_SEARCH_SQL = """
SELECT d.id, d.downloaded_at, d.title, d.artist, d.url, d.output_path,
       d.media_type, d.file_size_mb, d.duration_sec, d.thumbnail_url, d.platform
FROM   downloads d
JOIN   downloads_fts f ON d.id = f.rowid
WHERE  downloads_fts MATCH ?
ORDER  BY d.id DESC
LIMIT  ?
"""

_DELETE_SQL  = "DELETE FROM downloads WHERE id = ?"
_CLEAR_SQL   = "DELETE FROM downloads"
_COUNT_SQL   = "SELECT COUNT(*) FROM downloads"
_EXISTS_SQL  = "SELECT 1 FROM downloads WHERE id = ? LIMIT 1"


# ──────────────────────────────────────────────────────────────────────────────
# HistoryDB
# ──────────────────────────────────────────────────────────────────────────────

class HistoryDB:
    """
    Thread-safe SQLite history manager.

    Parameters
    ----------
    db_path : str | None
        Absolute path to the SQLite database file.
        Pass None or omit to use the default location:
        ~/.bananaflow/downloads.db
        Pass ":memory:" for an in-memory database (useful for tests).
    """

    _DEFAULT_LIMIT = 500

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None or db_path == "":
            db_path = self._default_path()

        self._path = db_path
        self._lock = threading.Lock()
        self._insert_listeners: list[object] = []

        # Create parent directory if needed (not for :memory:)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            self._conn = sqlite3.connect(
                db_path,
                check_same_thread=False,
                isolation_level=None,
            )
            # Fails immediately if the file is garbage (invalid header)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")

            # Integrity check (skip for :memory:)
            if db_path != ":memory:" and not self._check_integrity():
                logger.error(
                    "[HistoryDB] Integrity check FAILED for %s — "
                    "backing up and recreating.", db_path,
                )
                self._backup_and_recreate()

        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            logger.error("[HistoryDB] Startup failed: %s — recreating.", exc)
            self._backup_and_recreate()

        self._initialise_schema()
        logger.info("[HistoryDB] Opened %s (%d records)", db_path, self.count())

    # ── Schema setup ──────────────────────────────────────────────────────────

    def _initialise_schema(self) -> None:
        """Create tables and triggers if they do not already exist."""
        with self._lock:
            with self._conn:
                self._conn.executescript(
                    _CREATE_TABLE_SQL
                    + _CREATE_FTS_SQL
                    + _CREATE_TRIGGERS_SQL
                )

    # ── Live insert observers ─────────────────────────────────────────────────

    def subscribe_inserts(
        self,
        callback: Callable[[DownloadRecord], None],
    ) -> Callable[[], None]:
        """Subscribe to records after their INSERT transaction has committed.

        Bound methods are held weakly so the DB cannot keep a destroyed UI
        object alive. Plain functions are kept strongly: callers should not
        have to retain a separate reference merely to keep a subscription
        active. The returned callable explicitly unsubscribes either kind.
        """
        if getattr(callback, "__self__", None) is not None:
            stored: object = weakref.WeakMethod(callback)  # type: ignore[arg-type]
        else:
            stored = callback

        with self._lock:
            self._insert_listeners.append(stored)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._insert_listeners.remove(stored)
                except ValueError:
                    pass

        return unsubscribe

    def _notify_insert(self, record: DownloadRecord) -> None:
        """Notify subscribers outside the write transaction/connection lock."""
        with self._lock:
            listeners = list(self._insert_listeners)

        dead: list[object] = []
        for stored in listeners:
            if isinstance(stored, weakref.ReferenceType):
                callback = stored()
                if callback is None:
                    dead.append(stored)
                    continue
            else:
                callback = stored
            try:
                callback(record)  # type: ignore[operator]
            except Exception:
                logger.exception("[HistoryDB] Insert subscriber failed")

        if dead:
            with self._lock:
                for stored in dead:
                    try:
                        self._insert_listeners.remove(stored)
                    except ValueError:
                        pass

    # ── Write operations ──────────────────────────────────────────────────────

    def insert(self, record: DownloadRecord) -> int:
        """
        Persist a new download record and return its auto-assigned integer id.

        The record's `downloaded_at` field is filled with the current UTC
        timestamp if it is empty. The input object is not mutated. Subscribers
        receive a copied record carrying the committed id/timestamp, and are
        invoked only after the database lock has been released.
        """
        downloaded_at = record.downloaded_at or _utc_now()

        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    _INSERT_SQL,
                    (
                        downloaded_at,
                        record.title,
                        record.artist,
                        record.url,
                        record.output_path,
                        record.media_type,
                        record.file_size_mb,
                        record.duration_sec,
                        record.thumbnail_url,
                        record.platform,
                    ),
                )
                record_id = int(cursor.lastrowid or 0)

        if record_id:
            self._notify_insert(
                replace(record, id=record_id, downloaded_at=downloaded_at)
            )
        return record_id

    def delete(self, record_id: int) -> None:
        """
        Remove a single download record by its id.
        Silently does nothing if the id does not exist.
        """
        with self._lock:
            with self._conn:
                self._conn.execute(_DELETE_SQL, (record_id,))

    def clear_all(self) -> None:
        """
        Delete every record from the history table.
        Also rebuilds the FTS index to stay consistent.
        """
        with self._lock:
            with self._conn:
                self._conn.execute(_CLEAR_SQL)
                # Rebuild FTS index after mass delete
                self._conn.execute(
                    "INSERT INTO downloads_fts(downloads_fts) VALUES('rebuild')"
                )

    # ── Read operations ───────────────────────────────────────────────────────

    def fetch_all(self, limit: int = _DEFAULT_LIMIT) -> list[DownloadRecord]:
        """
        Return the most recent `limit` download records, newest first.
        """
        with self._lock:
            cursor = self._conn.execute(_FETCH_ALL_SQL, (limit,))
            return [_row_to_record(row) for row in cursor.fetchall()]

    def exists(self, record_id: int) -> bool:
        """Return whether a record still exists in the committed history."""
        if not record_id:
            return False
        with self._lock:
            return self._conn.execute(_EXISTS_SQL, (record_id,)).fetchone() is not None

    def search(
        self,
        query: str,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[DownloadRecord]:
        """
        Full-text search over title and artist fields.
        Returns up to `limit` matching records, newest first.

        The query uses SQLite FTS5 syntax:
            "example artist"  – phrase search
            example artist    – both terms anywhere
            example*          – prefix search
        """
        if not query or not query.strip():
            return self.fetch_all(limit=limit)

        # Escape FTS5 special characters to avoid syntax errors on raw user input
        safe_query = _escape_fts5(query.strip())

        with self._lock:
            try:
                cursor = self._conn.execute(_SEARCH_SQL, (safe_query, limit))
                return [_row_to_record(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                # If the FTS query is somehow still malformed, fall back to LIKE
                return self._search_fallback(query, limit)

    def _search_fallback(self, query: str, limit: int) -> list[DownloadRecord]:
        """LIKE-based fallback when the FTS query fails."""
        pattern = f"%{query}%"
        sql = """
            SELECT id, downloaded_at, title, artist, url, output_path,
                   media_type, file_size_mb, duration_sec, thumbnail_url, platform
            FROM   downloads
            WHERE  title LIKE ? OR artist LIKE ?
            ORDER  BY id DESC
            LIMIT  ?
        """
        cursor = self._conn.execute(sql, (pattern, pattern, limit))
        return [_row_to_record(row) for row in cursor.fetchall()]

    def count(self) -> int:
        """Return the total number of records in the history table."""
        with self._lock:
            cursor = self._conn.execute(_COUNT_SQL)
            row = cursor.fetchone()
            return row[0] if row else 0

    # ── Export ────────────────────────────────────────────────────────────────

    def export_csv(self, path: str) -> int:
        """
        Export all download history to a CSV file at `path`.
        Returns the number of rows written.

        The CSV includes a header row with human-friendly column names.
        The file is UTF-8-encoded with a BOM so Excel opens it correctly.
        """
        records = self.fetch_all(limit=100_000)

        col_headers = [
            "ID", "Date / Time", "Title", "Artist", "Platform",
            "Type", "Duration", "File Size", "Output Path",
            "Source URL", "Thumbnail URL",
        ]

        Path(path).parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(col_headers)
            for r in records:
                writer.writerow([
                    r.id,
                    r.display_date(),
                    r.title,
                    r.artist,
                    r.platform,
                    r.media_type,
                    r.duration_str(),
                    r.file_size_str(),
                    r.output_path,
                    r.url,
                    r.thumbnail_url,
                ])

        return len(records)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _check_integrity(self) -> bool:
        """Run a full integrity check on the database."""
        try:
            cursor = self._conn.execute("PRAGMA integrity_check;")
            result = cursor.fetchone()
            ok = result is not None and result[0] == "ok"
            if ok:
                logger.debug("[HistoryDB] Integrity check passed")
            else:
                logger.warning(
                    "[HistoryDB] integrity_check returned: %s",
                    result[0] if result else "(no result)",
                )
            return ok
        except sqlite3.DatabaseError as exc:
            logger.warning("[HistoryDB] integrity_check raised: %s", exc)
            return False

    def _backup_and_recreate(self) -> None:
        """Close connection, rename corrupt file, and recreate fresh database."""
        try:
            self._conn.close()
        except Exception:
            pass

        db_file = Path(self._path)
        if db_file.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup_name = db_file.with_suffix(f".db.corrupt.{ts}")
            try:
                shutil.move(str(db_file), str(backup_name))
                logger.info("[HistoryDB] Corrupt DB backed up to %s", backup_name)
            except OSError as exc:
                logger.error("[HistoryDB] Could not rename corrupt DB: %s", exc)
                try:
                    db_file.unlink()
                except OSError:
                    pass

            for suffix in (".db-wal", ".db-shm"):
                journal = db_file.with_suffix(suffix)
                if journal.exists():
                    try:
                        journal.unlink()
                    except OSError:
                        pass

        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        logger.info("[HistoryDB] Fresh database created at %s", self._path)

    def close(self) -> None:
        """Close the database connection. Call this on application shutdown."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "HistoryDB":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _default_path() -> str:
        """Return the canonical default DB path inside the app-data dir.

        Delegates to ``utils.paths.get_app_data_dir`` so the location is
        correct on every platform (Windows %APPDATA%, macOS
        ~/Library/Application Support, Linux XDG).
        """
        from utils.paths import get_app_data_dir
        return str(get_app_data_dir() / "downloads.db")

    def __repr__(self) -> str:
        return f"HistoryDB(path={self._path!r}, records={self.count()})"


# ──────────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_record(row: tuple) -> DownloadRecord:
    """Convert a raw SQLite row tuple into a DownloadRecord dataclass."""
    (
        db_id, downloaded_at, title, artist, url, output_path,
        media_type, file_size_mb, duration_sec, thumbnail_url, platform,
    ) = row
    return DownloadRecord(
        id=db_id,
        downloaded_at=downloaded_at,
        title=title,
        artist=artist,
        url=url,
        output_path=output_path,
        media_type=media_type,
        file_size_mb=float(file_size_mb) if file_size_mb is not None else None,
        duration_sec=int(duration_sec)   if duration_sec  is not None else None,
        thumbnail_url=thumbnail_url,
        platform=platform,
    )


def _escape_fts5(query: str) -> str:
    """
    Lightly escape a raw user query for safe use in an FTS5 MATCH expression.

    Strategy
    --------
    * Wrap the entire query in double-quotes to treat it as a phrase if it
      contains characters that are special in FTS5 syntax.
    * If the query looks like plain words only, pass it through so the user
      benefits from implicit AND and prefix (*) matching.
    """
    fts5_special = set('"^*:()OR AND NOT')
    if any(ch in fts5_special for ch in query):
        # Escape any embedded double-quotes and wrap in quotes
        escaped = query.replace('"', '""')
        return f'"{escaped}"'
    return query


# ──────────────────────────────────────────────────────────────────────────────
# Smoke-test  (python core/history_db.py)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    print("Running HistoryDB smoke-test against an in-memory database …\n")

    db = HistoryDB(":memory:")

    # ── Insert ────────────────────────────────────────────────────────────────
    sample_records = [
        DownloadRecord(
            title="Example Song One",
            artist="Example Artist",
            url="https://www.youtube.com/watch?v=EXAMPLE_ID_1",
            output_path="/home/user/Downloads/Music/01 Example Artist - Example Song One.mp3",
            media_type="audio",
            file_size_mb=8.4,
            duration_sec=213,
            thumbnail_url="",
            platform="youtube",
        ),
        DownloadRecord(
            title="Example Song Two",
            artist="Example Artist",
            url="https://www.youtube.com/watch?v=EXAMPLE_ID_2",
            output_path="/home/user/Downloads/Music/02 Example Artist - Example Song Two.mp3",
            media_type="audio",
            file_size_mb=15.2,
            duration_sec=354,
            thumbnail_url="",
            platform="youtube",
        ),
        DownloadRecord(
            title="Example Video",
            artist="Example Channel",
            url="https://www.youtube.com/watch?v=EXAMPLE_ID_3",
            output_path="/home/user/Downloads/Videos/Example Video.mp4",
            media_type="video",
            file_size_mb=245.0,
            duration_sec=7200,
            thumbnail_url="",
            platform="youtube",
        ),
    ]

    inserted_ids: list[int] = []
    for rec in sample_records:
        rid = db.insert(rec)
        inserted_ids.append(rid)
        print(f"  Inserted id={rid}: {rec.title!r}")

    print(f"\n  Total records: {db.count()}  (expected 3)")
    assert db.count() == 3, "Insert count mismatch"

    # ── Fetch all ─────────────────────────────────────────────────────────────
    all_records = db.fetch_all()
    print(f"\n  fetch_all() returned {len(all_records)} records (newest first):")
    for r in all_records:
        print(f"    [{r.id}] {r.display_date()} | {r.title} | {r.duration_str()} | {r.file_size_str()}")

    # ── Search ────────────────────────────────────────────────────────────────
    results = db.search("example")
    print(f"\n  search('example') → {len(results)} result(s)")
    assert len(results) >= 1, "Expected at least 1 result for 'example'"
    assert results[0].title == "Example Song One"
    print(f"    Found: {results[0].title!r}  ✅")

    results2 = db.search("video")
    print(f"\n  search('video') → {len(results2)} result(s)")
    assert len(results2) >= 1
    print(f"    Found: {results2[0].title!r} by {results2[0].artist!r}  ✅")

    # ── Delete ────────────────────────────────────────────────────────────────
    db.delete(inserted_ids[0])
    assert db.count() == 2, "Expected 2 records after delete"
    print(f"\n  delete(id={inserted_ids[0]}) succeeded — {db.count()} records remain  ✅")

    # ── Export CSV ────────────────────────────────────────────────────────────
    csv_path = "/tmp/bananaflow_history_test.csv"
    rows_written = db.export_csv(csv_path)
    print(f"\n  export_csv() → {rows_written} rows written to {csv_path}  ✅")
    assert rows_written == 2

    # ── Clear all ─────────────────────────────────────────────────────────────
    db.clear_all()
    assert db.count() == 0, "Expected 0 records after clear_all"
    print(f"\n  clear_all() succeeded — {db.count()} records remain  ✅")

    db.close()
    print("\n✅  All smoke-tests passed.")
    sys.exit(0)
