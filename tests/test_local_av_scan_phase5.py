"""Phase 5 (code signing, SmartScreen and antivirus) local scan pipeline.

scripts/run_local_av_scan.ps1 must scan release artifacts with the local
Microsoft Defender engine and fail closed on any detection, while never
uploading anything to a public multi-engine scanning service without
explicit owner authorization (HUMAN GATE 4, item 4). These are text
assertions over the script, the only way to test a Windows-only,
Defender-dependent script without actually running it on every platform
this suite's isolated gate covers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_local_av_scan.ps1"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_script_exists():
    assert SCRIPT_PATH.is_file()


def test_does_not_invoke_any_network_upload_cmdlet(script_text: str):
    # The docstring may *mention* public multi-engine scanning (e.g.
    # VirusTotal) as context for why this script deliberately doesn't do
    # it; what must never appear is an actual network-call cmdlet, which
    # would mean the script is silently uploading a build artifact.
    code_only = script_text.split(".EXAMPLE", 1)[-1]
    code_only = code_only.split("#>", 1)[-1]
    lowered = code_only.lower()
    for forbidden in ("invoke-webrequest", "invoke-restmethod", "curl ", "wget "):
        assert forbidden not in lowered, (
            f"run_local_av_scan.ps1's executable body must stay purely local; found '{forbidden}', "
            "which implies a network call. Public multi-engine scanning requires HUMAN GATE 4 "
            "authorization and belongs in the runbook's manual steps, not this script."
        )


def test_locates_mpcmdrun_dynamically_rather_than_hardcoding_a_version(script_text: str):
    assert "Find-MpCmdRun" in script_text
    assert "Get-ChildItem -Path $platformDir -Directory" in script_text


def test_fails_closed_on_disabled_antivirus(script_text: str):
    assert "AntivirusEnabled" in script_text
    assert "throw " in script_text


def test_scans_all_four_release_artifacts(script_text: str):
    for label in ("gui-exe", "cli-exe", "installer-exe", "portable-zip"):
        assert f"Label = '{label}'" in script_text


def test_uses_disable_remediation_flag(script_text: str):
    # -DisableRemediation keeps this a read-only detection pass: it must
    # never auto-quarantine/delete a legitimate release artifact out from
    # under the build.
    assert "-DisableRemediation" in script_text


def test_exits_non_zero_when_a_threat_is_flagged(script_text: str):
    assert "exit 1" in script_text
    assert "threatsFound" in script_text


def test_writes_machine_readable_evidence_under_gitignored_test_evidence(script_text: str):
    assert "test-evidence" in script_text
    assert "ConvertTo-Json" in script_text
