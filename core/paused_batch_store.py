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

    @property
    def workspace_dir(self) -> str:
        """The job's private workspace subdir — always read from
        ``request``, the single source of truth.

        An earlier version stored this as a SECOND, independent copy
        alongside the one nested in ``request`` (both set from the same
        ``req.workspace_dir`` at save time, but nothing enforced that they
        stayed in sync). Restore validated the top-level copy but
        reconstructed the actual DownloadRequest from the nested one — a
        hand-edited or version-skewed file where the two diverged could
        pass validation against a workspace the resumed job would then
        never actually use, or the reverse. Deriving it, always, from the
        one place the request itself will be rebuilt from removes the
        possibility of divergence entirely."""
        return str(self.request.get("workspace_dir", "") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "request": self.request,
            "card": self.card,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Optional["PausedJob"]:
        """Rebuild a PausedJob, or None if the record is unusable. Never
        raises.

        Unusable means: not a dict, no request payload, or no workspace
        path. The last one matters as much as the others even though the
        record "looks" complete. A paused job IS its workspace — the .part
        file, the intermediates and any already-finished output all live
        there, and every job the orchestrator can hand out for a pause has
        one by construction (run_batch assigns a per-job subdir before any
        job is registered). A record without one can therefore not be
        resumed from, and, worse, it contributes NOTHING to the keep-set
        the startup sweep is about to run: whatever workspace that job
        really had on disk is unprotected and gets deleted. Reporting the
        record as unreadable makes the store's caller skip the sweep
        entirely, which is the safe direction."""
        if not isinstance(d, dict):
            return None
        request = d.get("request")
        if not isinstance(request, dict):
            return None
        workspace = request.get("workspace_dir")
        if not isinstance(workspace, str) or not workspace.strip():
            return None
        return cls(
            key=str(d.get("key", "")),
            request=request,
            card=d.get("card") if isinstance(d.get("card"), dict) else {},
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
        malformed / partially-written file. Never raises.

        Callers that need to tell "genuinely nothing paused" apart from
        "couldn't read this" — the startup stale-workspace sweep, notably —
        must use :meth:`workspace_dirs_or_none` instead; this method
        deliberately collapses both cases to keep its own contract simple
        for ordinary pause/resume callers, who only ever want "what's
        currently resumable" and treat a corrupt file the same as none."""
        jobs, _corrupt = self._load_with_status()
        return jobs

    def _load_with_status(self) -> tuple[list[PausedJob], bool]:
        """Like :meth:`load`, but also reports whether the persisted state
        could not be read IN FULL — as opposed to being missing or genuinely
        empty, both of which mean "nothing was ever paused".

        "In full" is the operative word, and it is why an unreadable
        individual RECORD counts as corrupt just as much as unparseable
        JSON. A syntactically valid file with one malformed record used to
        be reported as perfectly readable: the bad record was silently
        dropped, the caller got a keep-set missing that job's workspace, and
        the startup sweep deleted a workspace that was very likely still
        resumable — the single worst outcome the corrupt-file check exists
        to prevent, reached through the one path that bypassed it.

        The same reasoning applies to the read itself. Only
        FileNotFoundError means "nothing was ever paused"; a permission
        error, a lock, or a transient I/O failure on a network profile
        means the state is unknown, and treating those as "empty" is what
        would let one bad read wipe every workspace on disk."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # The ONLY read failure that genuinely means "nothing was ever
            # paused". Everything else below is a state we could not read,
            # which is a different answer entirely.
            return [], False
        except OSError as exc:
            # A permission problem, a locked file, a directory in the way,
            # a transient I/O error on a network profile. Lumping these in
            # with "missing" handed the startup sweep an empty keep-set and
            # let it delete every workspace on disk because of a hiccup
            # that will very likely be gone next run.
            logger.warning(
                "[PausedBatchStore] Could not read %s (%s) — reporting the "
                "paused state as unreadable, not as empty", self._path, exc,
            )
            return [], True
        except (UnicodeDecodeError, ValueError) as exc:
            # Not valid UTF-8: the file exists and holds something, it just
            # isn't our state. Unreadable, never "nothing paused".
            logger.warning(
                "[PausedBatchStore] %s is not readable as UTF-8 (%s)", self._path, exc,
            )
            return [], True
        if not raw.strip():
            return [], False
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("[PausedBatchStore] Ignoring corrupt resume file %s: %s", self._path, exc)
            return [], True

        records = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            logger.warning(
                "[PausedBatchStore] Resume file %s has an unexpected shape "
                "(no jobs list)", self._path,
            )
            return [], True

        jobs: list[PausedJob] = []
        unreadable = 0
        for rec in records:
            job = PausedJob.from_dict(rec)
            if job is None:
                unreadable += 1
                continue
            jobs.append(job)
        if unreadable:
            logger.warning(
                "[PausedBatchStore] %d unreadable record(s) in %s — treating "
                "the paused state as corrupt so nothing destructive runs "
                "against a keep-set that is missing entries",
                unreadable, self._path,
            )
        return jobs, bool(unreadable)

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
        'keep' set for the startup stale-workspace sweep.

        Collapses a corrupt file to the same empty list as "nothing
        paused" — safe for callers that only display/act on the paused
        set itself, but NOT safe for a destructive sweep. Use
        :meth:`workspace_dirs_or_none` there instead."""
        return [j.workspace_dir for j in self.load() if j.workspace_dir]

    def workspace_dirs_or_none(self) -> Optional[list[str]]:
        """The stale-workspace sweep's 'keep' set, or None when the
        persisted state could not be read IN FULL — unparseable JSON, an
        unexpected shape, or even a single unreadable record inside an
        otherwise valid file.

        Any of those means the true keep-set is UNKNOWN, not empty — and a
        PARTIAL keep-set is just as dangerous as an empty one, because the
        entries it is missing are exactly the workspaces that then get
        swept. A sweep that treated "couldn't read all of this" the same as
        "nothing is paused" would delete still-resumable work because the
        record protecting it failed to parse. Callers doing anything
        destructive with the result must check for None and skip entirely
        rather than pass an incomplete list forward."""
        jobs, corrupt = self._load_with_status()
        if corrupt:
            return None
        return [j.workspace_dir for j in jobs if j.workspace_dir]
