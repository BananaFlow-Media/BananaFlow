"""
core/queue_persistence.py  –  Crash-safe incremental queue resume
==================================================================
Tracks which download jobs have completed during a batch so that if the
app crashes or is killed mid-download, only the unfinished items are
restored on next launch.

Problem solved
--------------
The existing ``config.queue_state`` saves the entire queue on close and
offers to restore it on startup.  But it has no notion of "which items
already finished" — so after a crash halfway through a 50-track playlist,
all 50 get re-queued, and the first 25 download again (or get caught by
duplicate_checker, wasting time).

Solution
--------
``QueueStateManager`` wraps the config persistence with a live
``completed_keys`` set.  As each track finishes, ``mark_completed(key)``
is called.  When the state is saved (on close, on crash signal, or
periodically), only non-completed items are written.

Integration
-----------
* Created once by AppWindow alongside the orchestrator.
* ``mark_completed()`` is called from ``on_track_finished`` callback.
* ``save()`` is called from ``closeEvent`` and optionally on a periodic
  timer (every 30s) for crash resilience.
* ``load()`` returns the list of unfinished items for the resume dialog.

Zero GUI imports.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional

from core.media_formats import DEFAULT_VIDEO_FORMAT
from core.quality_presets import DEFAULT_AUDIO_QUALITY_ID

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Serialisable queue item
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class QueueItem:
    """One item in the persisted queue state."""
    key:            str
    url:            str
    title:          str           = ""
    artist:         str           = ""
    duration_str:   str           = ""
    thumbnail_url:  str           = ""
    platform:       str           = "youtube"

    # Download options snapshot (so resume uses the same settings)
    output_dir:     str           = ""
    media_type:     str           = "audio"
    audio_format:   str           = "mp3"
    video_format:   str           = DEFAULT_VIDEO_FORMAT
    quality_label:  str           = DEFAULT_AUDIO_QUALITY_ID
    playlist_name:  str           = ""
    forced_index:   Optional[int] = None
    forced_disc:    Optional[int] = None
    forced_total:   Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QueueItem":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)


# ──────────────────────────────────────────────────────────────────────────────
# Queue State Manager
# ──────────────────────────────────────────────────────────────────────────────

class QueueStateManager:
    """
    Thread-safe manager for persisted download queue state.

    Parameters
    ----------
    config : AppConfig instance (used for reading/writing queue_state).
    """

    def __init__(self, config) -> None:
        self._cfg = config
        self._lock = threading.Lock()
        self._items: dict[str, QueueItem] = {}         # key → QueueItem
        self._completed: set[str] = set()              # keys that finished

    # ── Batch setup ───────────────────────────────────────────────────────────

    def set_batch(self, items: list[QueueItem]) -> None:
        """
        Register a new batch of items to track.
        Clears any previous state.  Call this when "Download Selected" is
        clicked, before the orchestrator starts.
        """
        with self._lock:
            self._items = {item.key: item for item in items}
            self._completed.clear()
        logger.info(
            "[QueueState] Batch registered: %d items", len(items),
        )
        self.save()

    # ── Progress tracking ─────────────────────────────────────────────────────

    def mark_completed(self, key: str) -> None:
        """
        Mark a job as completed.  Called from on_track_finished callback.
        Thread-safe.
        """
        with self._lock:
            self._completed.add(key)
        logger.debug("[QueueState] Marked completed: %s", key)

    def mark_failed(self, key: str) -> None:
        """
        Mark a job as failed.  Failed items ARE included in the resume
        list so the user can retry them.
        """
        # No-op for now — failed items stay in _items, not in _completed
        logger.debug("[QueueState] Marked failed: %s", key)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        """
        Persist only the UNFINISHED items to config.queue_state.
        Safe to call from any thread.
        """
        with self._lock:
            pending = [
                item.to_dict()
                for key, item in self._items.items()
                if key not in self._completed
            ]
        self._cfg.queue_state = pending
        self._cfg.save()
        logger.debug(
            "[QueueState] Saved %d pending items (%d completed)",
            len(pending), len(self._completed),
        )

    def clear(self) -> None:
        """Clear all state — call when batch finishes successfully."""
        with self._lock:
            self._items.clear()
            self._completed.clear()
        self._cfg.queue_state = []
        self._cfg.save()
        logger.debug("[QueueState] Cleared")

    # ── Resume ────────────────────────────────────────────────────────────────

    def load_pending(self) -> list[QueueItem]:
        """
        Load unfinished items from config.  Called on startup to decide
        whether to show the resume dialog.
        """
        raw = self._cfg.queue_state
        if not raw or not isinstance(raw, list):
            return []
        items = []
        for d in raw:
            try:
                items.append(QueueItem.from_dict(d))
            except Exception as exc:
                logger.debug("[QueueState] Skipping invalid item: %s", exc)
        logger.info("[QueueState] Loaded %d pending items from config", len(items))
        return items

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._items) - len(self._completed)

    @property
    def completed_count(self) -> int:
        with self._lock:
            return len(self._completed)

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._items)
