"""
tests/test_history_db_resilience.py  –  Integrity check & recovery tests
=========================================================================
Run:
    pytest tests/test_history_db_resilience.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestHistoryDBResilience:
    """Verify the integrity check and backup-and-recreate logic."""

    def test_healthy_db_passes_check(self, tmp_path):
        """A freshly created DB should pass integrity check."""
        from core.history_db import HistoryDB
        db_path = str(tmp_path / "test.db")
        db = HistoryDB(db_path)
        assert db._check_integrity() is True
        db.close()

    def test_corrupt_db_detected_and_recovered(self, tmp_path):
        """
        Manually corrupt a DB file, then open it via HistoryDB.
        The constructor should detect corruption, back it up, and
        create a fresh empty database.
        """
        from core.history_db import HistoryDB, DownloadRecord

        db_path = str(tmp_path / "test.db")

        # Create a valid DB with one record
        db = HistoryDB(db_path)
        db.insert(DownloadRecord(
            title="Test", url="https://example.com",
            output_path="/tmp/test.mp3", media_type="audio",
        ))
        assert db.count() == 1
        db.close()

        # Corrupt the file by overwriting the header with garbage
        p = Path(db_path)
        data = bytearray(p.read_bytes())
        # Overwrite the first 100 bytes (SQLite header)
        data[0:100] = b"\x00" * 100
        p.write_bytes(bytes(data))

        # Re-open — should detect corruption and recreate
        db2 = HistoryDB(db_path)
        # The DB should be empty (fresh) after recovery
        assert db2.count() == 0
        # A backup file should exist
        backups = list(tmp_path.glob("*.corrupt.*"))
        assert len(backups) >= 1
        db2.close()

    def test_memory_db_skips_integrity_check(self):
        """In-memory databases should not attempt integrity checks."""
        from core.history_db import HistoryDB
        db = HistoryDB(":memory:")
        # Should open fine — no file to corrupt
        assert db.count() == 0
        db.close()
