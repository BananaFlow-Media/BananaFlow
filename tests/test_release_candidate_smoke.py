"""Regression coverage for the packaged Windows release-candidate smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="DPAPI release smoke is Windows-only")


def _configure_environment(monkeypatch, root: Path, result: Path, scenario: str, secret: str) -> None:
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(root))
    monkeypatch.setenv("BANANAFLOW_SMOKE_RESULT_FILE", str(result))
    monkeypatch.setenv("BANANAFLOW_RC_SMOKE_SCENARIO", scenario)
    monkeypatch.setenv("BANANAFLOW_RC_COOKIE_SECRET", secret)


def _run(monkeypatch, root: Path, scenario: str, secret: str, result: Path) -> dict:
    from core.release_candidate_smoke import run_release_candidate_smoke

    _configure_environment(monkeypatch, root, result, scenario, secret)
    assert run_release_candidate_smoke() == 0
    parsed = json.loads(result.read_text(encoding="utf-8"))
    assert parsed["ok"]
    assert not [step for step in parsed["steps"] if not step["ok"]]
    assert secret not in result.read_text(encoding="utf-8")
    return parsed


def test_fresh_restart_and_delete_packaged_phases(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    secret = "fresh-release-cookie-canary"

    fresh = _run(monkeypatch, root, "fresh", secret, tmp_path / "fresh.json")
    names = {step["step"] for step in fresh["steps"]}
    assert {
        "configuration_saved",
        "dpapi_cookie_access",
        "direct_download_startup",
        "spotify_resolver_startup",
        "eta_snapshot_delivery",
        "spotify_track_scoped_metadata",
        "spotify_track_scoped_artwork",
        "spotify_polluted_metadata_detected",
        "spotify_strict_match_flow",
        "spotify_general_fallback_flow",
        "spotify_odeya_album_reasonable_fallback",
        "no_empty_url_engine_submission",
        "mixed_resolved_legacy_fallback_batch",
        "spotify_malformed_ui_state",
        "independent_track_output_context",
        "grouped_source_output_context",
        "spotify_artwork_download_request",
        "txt_import_all_urls",
        "txt_import_failure_continuation",
        "provider_timeout_work_bounded",
    } <= names

    residue = root / ".bananaflow" / "auth_tmp" / "session-crash-residue.txt"
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("residue", encoding="utf-8")
    restart = _run(monkeypatch, root, "restart", secret, tmp_path / "restart.json")
    assert next(step for step in restart["steps"] if step["step"] == "crash_residue_cleanup")["ok"]

    deleted = _run(monkeypatch, root, "delete", secret, tmp_path / "delete.json")
    assert next(step for step in deleted["steps"] if step["step"] == "stored_sign_in_deletion")["ok"]
    assert not (root / ".bananaflow" / "app_cookies.dpapi").exists()
    assert not list(root.rglob("session-*.txt"))


def test_upgrade_migrates_chrome_notice_and_plaintext_once(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    app_data = root / ".bananaflow"
    app_data.mkdir(parents=True)
    secret = "upgrade-release-cookie-canary"
    legacy = app_data / "app_cookies.txt"
    legacy.write_text(
        f".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\t{secret}\n",
        encoding="utf-8",
    )
    (app_data / "config.json").write_text(json.dumps({
        "config_version": 9,
        "cookies_browser": "chrome",
        "cookies_file": str(legacy),
        "check_updates": False,
    }), encoding="utf-8")

    upgraded = _run(monkeypatch, root, "upgrade", secret, tmp_path / "upgrade.json")
    names = {step["step"] for step in upgraded["steps"]}
    assert {
        "unsupported_chromium_migrated",
        "legacy_plaintext_migrated",
        "one_time_migration_notice",
    } <= names
    assert not legacy.exists()

    restarted = _run(monkeypatch, root, "restart", secret, tmp_path / "restart.json")
    assert next(step for step in restarted["steps"] if step["step"] == "restart_after_migration")["ok"]


def test_windows_release_workflow_runs_artifact_smoke():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "release-windows.yml").read_text(encoding="utf-8")
    runner = (root / "scripts" / "run_windows_release_smoke.ps1").read_text(encoding="utf-8")
    assert "run_windows_release_smoke.ps1" in workflow
    assert "release-candidate-summary.json" in runner
    assert "Get-PeSubsystem" in runner
    assert "New-UpgradeFixture" in runner
    assert "Assert-NoPlaintextResidue" in runner
    assert "$info.ArgumentList" not in runner
    assert "[System.Text.UTF8Encoding]::new($false)" in runner
    assert '"bf-rc-"' in runner
