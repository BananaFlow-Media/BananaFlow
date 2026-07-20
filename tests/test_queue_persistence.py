"""
tests/test_queue_persistence.py  –  Queue state manager tests
===============================================================
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.queue_persistence import QueueItem, QueueStateManager


class FakeConfig:
    """Minimal AppConfig mock with queue_state property."""

    def __init__(self):
        self.queue_state: list = []
        self._saved = False

    def save(self):
        self._saved = True


class TestQueueItem:

    def test_round_trip(self):
        item = QueueItem(key="k1", url="http://a", title="Song", artist="Artist")
        d = item.to_dict()
        restored = QueueItem.from_dict(d)
        assert restored.key == "k1"
        assert restored.title == "Song"

    def test_from_dict_ignores_unknown_keys(self):
        d = {"key": "k1", "url": "http://a", "future_field": "x"}
        item = QueueItem.from_dict(d)
        assert item.key == "k1"


class TestQueueStateManager:

    @pytest.fixture
    def mgr(self):
        cfg = FakeConfig()
        return QueueStateManager(cfg), cfg

    def test_set_batch_and_save(self, mgr):
        m, cfg = mgr
        items = [
            QueueItem(key="a", url="http://a", title="A"),
            QueueItem(key="b", url="http://b", title="B"),
            QueueItem(key="c", url="http://c", title="C"),
        ]
        m.set_batch(items)
        assert m.total_count == 3
        assert m.pending_count == 3
        assert len(cfg.queue_state) == 3

    def test_mark_completed_reduces_pending(self, mgr):
        m, cfg = mgr
        items = [
            QueueItem(key="a", url="http://a"),
            QueueItem(key="b", url="http://b"),
        ]
        m.set_batch(items)
        m.mark_completed("a")
        assert m.completed_count == 1
        assert m.pending_count == 1

    def test_save_excludes_completed(self, mgr):
        m, cfg = mgr
        items = [
            QueueItem(key="a", url="http://a", title="A"),
            QueueItem(key="b", url="http://b", title="B"),
            QueueItem(key="c", url="http://c", title="C"),
        ]
        m.set_batch(items)
        m.mark_completed("a")
        m.mark_completed("c")
        m.save()
        # Only "b" should remain
        assert len(cfg.queue_state) == 1
        assert cfg.queue_state[0]["key"] == "b"

    def test_clear_empties_everything(self, mgr):
        m, cfg = mgr
        m.set_batch([QueueItem(key="a", url="http://a")])
        m.mark_completed("a")
        m.clear()
        assert m.total_count == 0
        assert m.completed_count == 0
        assert cfg.queue_state == []

    def test_load_pending(self, mgr):
        m, cfg = mgr
        cfg.queue_state = [
            {"key": "x", "url": "http://x", "title": "X"},
            {"key": "y", "url": "http://y", "title": "Y"},
        ]
        loaded = m.load_pending()
        assert len(loaded) == 2
        assert loaded[0].key == "x"

    def test_load_pending_empty(self, mgr):
        m, cfg = mgr
        cfg.queue_state = []
        assert m.load_pending() == []

    def test_load_pending_skips_corrupt(self, mgr):
        m, cfg = mgr
        cfg.queue_state = [
            {"key": "ok", "url": "http://ok"},
            "not_a_dict",
        ]
        loaded = m.load_pending()
        assert len(loaded) == 1

    def test_mark_failed_keeps_in_pending(self, mgr):
        m, cfg = mgr
        m.set_batch([QueueItem(key="a", url="http://a")])
        m.mark_failed("a")
        m.save()
        assert len(cfg.queue_state) == 1  # still pending
