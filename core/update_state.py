"""
core/update_state.py  –  Persistent "remind me later" / "skip version" store
==============================================================================
Small JSON-backed store that remembers, per exact update version, whether
the user chose "don't remind me again" (dismissed) or "remind me later"
(snoozed until a timestamp). Both the app-release check and the runtime
component check consult it before showing a notification, so a dismissed
update never nags again — while a strictly newer future version gets a
fresh identity and notifies normally.

Identity scheme
---------------
Every notifiable update is keyed by a stable string id:

    app:<version>                e.g. "app:1.2.0"
    component:<key>:<version>    e.g. "component:yt-dlp:2026.7.4"

The *target* version is part of the id on purpose: dismissing
"app:1.2.0" says nothing about "app:1.3.0".

Design
------
* Zero GUI imports — pure stdlib.
* Stored in its own file (update_state.json in the app-data dir), not in
  config.json: this is bookkeeping about past prompts, not a user
  preference, and keeping it separate means AppConfig's schema and
  migrations stay untouched.
* Corrupt or missing files are treated as an empty store — the worst
  outcome of losing this file is one extra notification.
* Writes are atomic (tmp file + replace), mirroring config.py.
* ``now`` is injectable for tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional


def app_update_id(version: str) -> str:
    """Stable id for a specific BananaFlow release, e.g. 'app:1.2.0'."""
    return f"app:{version.strip().lstrip('vV')}"


def component_update_id(component_key: str, latest_version: str) -> str:
    """Stable id for a specific component release, e.g. 'component:yt-dlp:2026.7.4'."""
    return f"component:{component_key}:{latest_version.strip()}"


def _default_path() -> Path:
    from utils.paths import get_app_data_dir
    return get_app_data_dir() / "update_state.json"


class UpdateStateStore:
    """
    Remembers dismissed and snoozed update ids across sessions.

    Parameters
    ----------
    path : Path | None
        Storage file. Defaults to <app-data>/update_state.json.
    now : Callable[[], datetime] | None
        Clock, injectable for tests. Must return an aware UTC datetime.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._path = path if path is not None else _default_path()
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._dismissed: set[str] = set()
        self._snoozed: dict[str, str] = {}   # update_id -> ISO-8601 "snoozed until"
        self._load()

    # ── Queries ────────────────────────────────────────────────────────────────

    def is_dismissed(self, update_id: str) -> bool:
        return update_id in self._dismissed

    def is_snoozed(self, update_id: str) -> bool:
        raw = self._snoozed.get(update_id)
        if not raw:
            return False
        until = self._parse_ts(raw)
        if until is None:
            return False   # unreadable timestamp — fail open and notify
        return self._now() < until

    def should_notify(self, update_id: str) -> bool:
        """True when this exact update has been neither dismissed nor snoozed."""
        return not self.is_dismissed(update_id) and not self.is_snoozed(update_id)

    # ── Mutations (each persists immediately) ─────────────────────────────────

    def dismiss(self, update_id: str) -> None:
        """Never notify about this exact update version again."""
        self._dismissed.add(update_id)
        self._snoozed.pop(update_id, None)
        self._save()

    def snooze(self, update_id: str, days: float) -> None:
        """Suppress notifications for this update for ``days`` days."""
        until = self._now() + timedelta(days=days)
        self._snoozed[update_id] = until.isoformat()
        self._save()

    def clear(self, update_id: str) -> None:
        """Forget any dismissal/snooze for this update id."""
        self._dismissed.discard(update_id)
        self._snoozed.pop(update_id, None)
        self._save()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        dismissed = raw.get("dismissed")
        if isinstance(dismissed, list):
            self._dismissed = {str(v) for v in dismissed}
        snoozed = raw.get("snoozed")
        if isinstance(snoozed, dict):
            self._snoozed = {str(k): str(v) for k, v in snoozed.items()}
        self._prune_expired_snoozes()

    def _save(self) -> None:
        self._prune_expired_snoozes()
        payload = {
            "dismissed": sorted(self._dismissed),
            "snoozed": dict(self._snoozed),
        }
        tmp_path = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(self._path)
        except OSError:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _prune_expired_snoozes(self) -> None:
        now = self._now()
        for update_id in list(self._snoozed):
            until = self._parse_ts(self._snoozed[update_id])
            if until is None or until <= now:
                del self._snoozed[update_id]

    @staticmethod
    def _parse_ts(raw: str) -> Optional[datetime]:
        try:
            ts = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
