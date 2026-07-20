"""
tests/test_network_test_gate.py  –  The opt-in network tests are actually run
========================================================================
Issue #34 asked for an integration test that "would have caught the Phase 6
stale-selector bug directly". One was written — and then gated behind
BANANAFLOW_RUN_NETWORK_TESTS=1 with nothing setting that variable: no CI job, no
script, no line in CONTRIBUTING.md. So it caught nothing, and could not have.

A gated test nobody runs is worth less than no test, because it reads as
coverage. These guard the wiring that turns it back into a real check:
scripts/run_network_tests.py knows about every file holding gated tests, the
weekly workflow calls that script, and CONTRIBUTING.md tells a releaser to run
it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "scripts" / "run_network_tests.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "network-tests.yml"
GATE_ENV_VAR = "BANANAFLOW_RUN_NETWORK_TESTS"


def _files_with_gated_tests() -> set[str]:
    found = set()
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if GATE_ENV_VAR in path.read_text(encoding="utf-8"):
            found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def _runner_file_list() -> set[str]:
    block = re.search(r"NETWORK_TEST_FILES\s*=\s*\[(.*?)\]",
                      RUNNER.read_text(encoding="utf-8"), re.S)
    assert block, "could not find NETWORK_TEST_FILES in scripts/run_network_tests.py"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def test_every_file_with_gated_tests_is_listed_in_the_runner():
    """The failure mode this whole file exists for: a gated test in a file the
    runner does not know about never executes, anywhere, ever."""
    missing = _files_with_gated_tests() - _runner_file_list()
    assert not missing, (
        f"these test files contain {GATE_ENV_VAR}-gated tests but are not in "
        f"NETWORK_TEST_FILES in scripts/run_network_tests.py, so nothing ever "
        f"runs them: {sorted(missing)}"
    )


def test_the_runner_does_not_list_files_that_no_longer_have_gated_tests():
    """Inverse: a stale entry makes the list look bigger than the coverage."""
    stale = _runner_file_list() - _files_with_gated_tests()
    assert not stale, (
        f"NETWORK_TEST_FILES lists files with no {GATE_ENV_VAR}-gated tests "
        f"left: {sorted(stale)}"
    )


def test_the_runner_sets_the_gate_variable():
    """Without this the runner collects zero tests and exits 0 — a green run
    that verified nothing, which is the worst possible outcome here."""
    source = RUNNER.read_text(encoding="utf-8")
    assert f'GATE_ENV_VAR = "{GATE_ENV_VAR}"' in source
    assert 'env[GATE_ENV_VAR] = "1"' in source
    assert "returncode == 5" in source, (
        "pytest exit code 5 means 'no tests collected' — the runner must treat "
        "that as a failure, not as success"
    )


def test_the_weekly_workflow_invokes_the_runner():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/run_network_tests.py" in workflow, (
        "the scheduled workflow must call the runner, not re-implement the "
        "gate inline where the two can drift apart"
    )
    assert re.search(r"schedule:\s*\n\s*#.*\n?\s*- cron:", workflow) or "cron:" in workflow, (
        "the workflow must actually be scheduled — issue #34's test failed to "
        "catch anything precisely because nothing triggered it"
    )
    assert "playwright install" in workflow, (
        "the Playwright half of the comparison needs a real Chromium"
    )


def test_the_workflow_does_not_block_pull_requests():
    """YouTube blocks many GitHub Actions egress IPs, so this signal is too
    noisy to gate merges on — it is advisory, and CONTRIBUTING.md's
    pre-release check from a real network is the blocking one."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request:" not in workflow
    assert "push:" not in workflow


def test_contributing_documents_the_pre_release_check():
    """The gate that actually catches things is a human running this before a
    release, which only works if it is written down."""
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "scripts/run_network_tests.py" in contributing
    assert "before cutting a release" in contributing.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
