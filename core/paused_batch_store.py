"""
core/paused_batch_store.py  –  Authoritative persisted paused-download state
==============================================================================
The single source of truth for "which downloads did the user pause, and how
do we resume them after a restart". Replaces the old write-only
``config.paused_items`` list (which nothing ever read back).

Each persisted job record holds:
  * ``key``         – opaque per-record id (card identity is regenerated on
                      restore, so this is only used to de-dupe within a save)
  * ``request``     – core.download_request_codec.request_to_dict output:
                      everything needed to rebuild a resumable DownloadRequest
  * ``card``        – display metadata to rebuild the queue card (title,
                      artist, url, thumbnail, platform, …)
  * ``workspace_dir`` – the job's private workspace subdir (its .part file
                      lives here); doubles as the "keep this workspace" marker
                      for the startup stale-workspace sweep.

The file is stored under the app-data dir. Every read tolerates a missing,
empty, malformed or partially-written file by returning an empty list — a
corrupt resume file must never crash startup.

Zero GUI imports.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from utils.paths import get_app_data_dir

logger = logging.getLogger(__name__)

_STORE_VERSION = 1


def _default_store_path() -> Path:
    return get_app_data_dir() / "paused_batches.json"


@dataclass
class PausedJob:
    """One persisted paused job."""
    key: str
    request: dict[str, Any]
    card: dict[str, Any] = field(default_factory=dict)
    workspace_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "request": self.request,
            "card": self.card,
            "workspace_dir": self.workspace_dir,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["PausedJob"]:
        """Rebuild a PausedJob, or None if the record is unusable (not a
        dict, or has no request payload). Never raises."""
        if not isinstance(d, dict):
            return None
        request = d.get("request")
        if not isinstance(request, dict):
            return None
        return cls(
            key=str(d.get("key", "")),
            request=request,
            card=d.get("card") if isinstance(d.get("card"), dict) else {},
            workspace_dir=str(d.get("workspace_dir", "") or request.get("workspace_dir", "") or ""),
        )


class PausedBatchStore:
    """Load/save the persisted paused jobs. Thread-unaware by design — it is
    driven from the UI thread (pause/resume/startup) only."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else _default_store_path()

    @property
    def path(self) -> Path:
        return self._path

    # ── Read ──────────────────────────────────────────────────────────────────

    def load(self) -> list[PausedJob]:
        """Return the persisted paused jobs, or [] for a missing / empty /
        malformed / partially-written file. Never raises."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        if not raw.strip():
            return []
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("[PausedBatchStore] Ignoring corrupt resume file %s: %s", self._path, exc)
            return []

        records = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            return []

        jobs: list[PausedJob] = []
        for rec in records:
            job = PausedJob.from_dict(rec)
            if job is not None:
                jobs.append(job)
        return jobs

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(self, jobs: list[PausedJob]) -> None:
        """Atomically write the paused jobs. A write failure is logged, not
        raised — failing to persist must never break pause itself."""
        payload = {"version": _STORE_VERSION, "jobs": [j.to_dict() for j in jobs]}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace so a crash mid-write can't leave a half file
            # (which load() would ignore anyway, but this keeps the last-good
            # state instead of losing it).
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, str(self._path))
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except OSError as exc:
            logger.warning("[PausedBatchStore] Could not save paused batches: %s", exc)

    def clear(self) -> None:
        """Remove the persisted state entirely (all paused work resumed or
        cancelled). Never raises."""
        try:
            self._path.unlink()
        except (FileNotFoundError, OSError):
            pass

    # ── Convenience ────────────────────────────────────────────────────────────

    def workspace_dirs(self) -> list[str]:
        """The workspace paths of every currently-persisted paused job — the
        'keep' set for the startup stale-workspace sweep."""
        return [j.workspace_dir for j in self.load() if j.workspace_dir]
