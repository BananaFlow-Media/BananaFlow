"""
tests/test_update_state.py  –  Remind-later / skip-version store
====================================================================
Uses a tmp_path storage file and an injected clock — no app-data dir or
real time involved.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.update_state import (
    UpdateStateStore,
    app_update_id,
    component_update_id,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _store(tmp_path, clock=None):
    return UpdateStateStore(path=tmp_path / "update_state.json", now=clock or _Clock())


# ──────────────────────────────────────────────────────────────────────────────
# Update ids
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateIds:

    def test_app_id_normalises_leading_v(self):
        assert app_update_id("1.2.0") == "app:1.2.0"
        assert app_update_id("v1.2.0") == "app:1.2.0"

    def test_component_id_includes_key_and_target_version(self):
        assert component_update_id("yt-dlp", "2026.7.4") == "component:yt-dlp:2026.7.4"

    def test_different_versions_are_different_updates(self):
        assert app_update_id("1.2.0") != app_update_id("1.3.0")
        assert component_update_id("yt-dlp", "a") != component_update_id("yt-dlp", "b")


# ──────────────────────────────────────────────────────────────────────────────
# Dismiss ("skip this version")
# ──────────────────────────────────────────────────────────────────────────────

class TestDismiss:

    def test_fresh_update_notifies(self, tmp_path):
        store = _store(tmp_path)
        assert store.should_notify("app:1.2.0")

    def test_dismissed_update_never_notifies_again(self, tmp_path):
        store = _store(tmp_path)
        store.dismiss("app:1.2.0")
        assert not store.should_notify("app:1.2.0")

    def test_dismissal_persists_across_instances(self, tmp_path):
        _store(tmp_path).dismiss("component:yt-dlp:2026.8.1")
        reloaded = _store(tmp_path)
        assert not reloaded.should_notify("component:yt-dlp:2026.8.1")

    def test_newer_version_notifies_after_older_was_dismissed(self, tmp_path):
        store = _store(tmp_path)
        store.dismiss(app_update_id("1.2.0"))
        assert store.should_notify(app_update_id("1.3.0"))
        store.dismiss(component_update_id("yt-dlp", "2026.8.1"))
        assert store.should_notify(component_update_id("yt-dlp", "2026.9.1"))

    def test_clear_forgets_a_dismissal(self, tmp_path):
        store = _store(tmp_path)
        store.dismiss("app:1.2.0")
        store.clear("app:1.2.0")
        assert store.should_notify("app:1.2.0")


# ──────────────────────────────────────────────────────────────────────────────
# Snooze ("remind me later")
# ──────────────────────────────────────────────────────────────────────────────

class TestSnooze:

    def test_snoozed_update_stays_quiet_until_deadline(self, tmp_path):
        clock = _Clock()
        store = _store(tmp_path, clock)
        store.snooze("app:1.2.0", days=3)
        assert not store.should_notify("app:1.2.0")

        clock.advance(days=2, hours=23)
        assert not store.should_notify("app:1.2.0")

        clock.advance(hours=2)
        assert store.should_notify("app:1.2.0")

    def test_snooze_persists_across_instances(self, tmp_path):
        clock = _Clock()
        _store(tmp_path, clock).snooze("app:1.2.0", days=7)
        reloaded = _store(tmp_path, clock)
        assert not reloaded.should_notify("app:1.2.0")

    def test_expired_snooze_is_pruned_from_disk(self, tmp_path):
        clock = _Clock()
        store = _store(tmp_path, clock)
        store.snooze("app:1.2.0", days=1)
        clock.advance(days=2)
        store.dismiss("other:thing")   # any save triggers pruning
        raw = json.loads((tmp_path / "update_state.json").read_text(encoding="utf-8"))
        assert "app:1.2.0" not in raw["snoozed"]

    def test_dismiss_wins_over_snooze(self, tmp_path):
        store = _store(tmp_path)
        store.snooze("app:1.2.0", days=1)
        store.dismiss("app:1.2.0")
        assert not store.should_notify("app:1.2.0")


# ──────────────────────────────────────────────────────────────────────────────
# Resilience
# ──────────────────────────────────────────────────────────────────────────────

class TestResilience:

    def test_missing_file_is_empty_store(self, tmp_path):
        assert _store(tmp_path).should_notify("app:1.0.0")

    def test_corrupt_json_is_empty_store(self, tmp_path):
        (tmp_path / "update_state.json").write_text("{not json", encoding="utf-8")
        assert _store(tmp_path).should_notify("app:1.0.0")

    def test_non_dict_json_is_empty_store(self, tmp_path):
        (tmp_path / "update_state.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert _store(tmp_path).should_notify("app:1.0.0")

    def test_unreadable_snooze_timestamp_fails_open(self, tmp_path):
        (tmp_path / "update_state.json").write_text(
            json.dumps({"dismissed": [], "snoozed": {"app:1.2.0": "not-a-date"}}),
            encoding="utf-8",
        )
        # An unparseable "snoozed until" must notify rather than silence forever.
        assert _store(tmp_path).should_notify("app:1.2.0")

    def test_file_written_atomically_and_valid_json(self, tmp_path):
        store = _store(tmp_path)
        store.dismiss("app:1.2.0")
        path = tmp_path / "update_state.json"
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["dismissed"] == ["app:1.2.0"]
