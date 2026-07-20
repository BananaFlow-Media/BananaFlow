"""Tests for scripts/run_isolated_tests.py — the Windows release test gate.

The gate decides whether a release may proceed, so its own classification logic
needs to be exercised against real child processes, not mocks: the whole point
of the runner is that it reads a *real* process exit code. Each scenario below
synthesises a throwaway test file in a temporary git repository and runs the
real runner over it.

A native exit is emulated with ``os._exit(<code>)`` from a conftest hook. That
is the only honest way to produce a specific process exit code on demand — the
runner itself never does this, and the prohibition on ``os._exit`` applies to
production/gate code, not to a fixture whose job is to fake a crash.

The emulated codes are platform-dependent, because exit codes are: Windows
reports a full 32-bit status (the real 0xC0000005 the suite exhibits), while a
POSIX exit code is 8-bit, so the real Windows values are unrepresentable there.
What the runner actually keys on is "outside pytest's documented 0-5 range", so
each platform uses a value that satisfies that, and the classification logic
under test is identical on both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_isolated_tests as runner  # noqa: E402

WINDOWS = os.name == "nt"

# The real codes the Windows suite exhibits, recorded in the shipped baseline.
WIN_ACCESS_VIOLATION = 3221225477  # 0xC0000005
WIN_STACK_BUFFER_OVERRUN = 3221226505  # 0xC0000409

# What the synthetic child processes actually exit with on this platform.
KNOWN_CODE = WIN_ACCESS_VIOLATION if WINDOWS else 139
RETRY_CODE = WIN_STACK_BUFFER_OVERRUN if WINDOWS else 134
OTHER_CODE = 3221225725 if WINDOWS else 132  # 0xC00000FD on Windows


def _exit_literal(code: int) -> int:
    """The argument ``os._exit`` needs to make the process report ``code``.

    ``os._exit`` takes a C ``int``, so a Windows status above ``INT_MAX`` has to
    be passed as its signed two's-complement form; Windows then reports it back
    unsigned. Verified: ``os._exit(-1073741819)`` -> returncode 3221225477.
    """
    return code - 2**32 if code > 2**31 - 1 else code


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _write_baseline(
    tmp_path: Path,
    *,
    known: list[str] | None = None,
    retry: list[str] | None = None,
    max_attempts: int = 3,
) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "known_native_exit": {"code": KNOWN_CODE, "files": known or []},
                "intermittent_native_retry": {
                    "code": RETRY_CODE,
                    "max_attempts": max_attempts,
                    "files": retry or [],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _exit_after_session_conftest(code: int) -> str:
    """A conftest that lets pytest finish and write its XML, then faults.

    ``atexit`` runs after ``pytest_sessionfinish``, so the JUnit XML is already
    on disk — this reproduces the real teardown-fault shape: complete passing
    evidence, then a native death.
    """
    return (
        "import atexit, os\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    atexit.register(lambda: os._exit({_exit_literal(code)}))\n"
    )


def _exit_during_run_conftest(code: int) -> str:
    """A conftest that kills the process mid-run, before any result is written."""
    return (
        "import os\n"
        "def pytest_collection_finish(session):\n"
        f"    os._exit({_exit_literal(code)})\n"
    )


def _run(
    tmp_path: Path,
    rel_files: list[str],
    *,
    baseline: Path,
    timeout: int = 120,
) -> dict:
    evidence = tmp_path / "evidence"
    code = runner.main(
        [
            "--repo-root",
            str(tmp_path),
            "--evidence-dir",
            str(evidence),
            "--baseline",
            str(baseline),
            "--timeout",
            str(timeout),
            "--files",
            *rel_files,
        ]
    )
    summary = json.loads((evidence / "isolated_results.json").read_text(encoding="utf-8"))
    summary["_exit_code"] = code
    return summary


def _outcome(summary: dict, path: str) -> str:
    for entry in summary["files"]:
        if entry["path"] == path:
            return entry["outcome"]
    raise AssertionError(f"{path} missing from summary")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway directory that behaves like a checkout for the runner."""
    (tmp_path / "tests").mkdir()
    return tmp_path


# ──────────────────────────────────────────────────────────────────────────────
# Logical outcomes
# ──────────────────────────────────────────────────────────────────────────────


def test_passing_file_is_accepted(project: Path):
    (project / "tests" / "test_pass.py").write_text(
        "def test_a(): assert True\ndef test_b(): assert 1 + 1 == 2\n", encoding="utf-8"
    )
    summary = _run(project, ["tests/test_pass.py"], baseline=_write_baseline(project))

    assert summary["_exit_code"] == 0
    assert summary["ok"] is True
    assert _outcome(summary, "tests/test_pass.py") == runner.OUTCOME_PASSED
    assert summary["totals"]["passed"] == 2
    assert summary["totals"]["failed"] == 0


def test_failing_file_fails_the_gate(project: Path):
    (project / "tests" / "test_fail.py").write_text(
        "def test_ok(): assert True\ndef test_bad(): assert False, 'boom'\n",
        encoding="utf-8",
    )
    summary = _run(project, ["tests/test_fail.py"], baseline=_write_baseline(project))

    assert summary["_exit_code"] == 1
    assert summary["ok"] is False
    assert _outcome(summary, "tests/test_fail.py") == runner.OUTCOME_TESTS_FAILED
    assert summary["totals"]["failed"] == 1


def test_skipped_tests_are_reported_and_do_not_fail(project: Path):
    (project / "tests" / "test_skip.py").write_text(
        "import pytest\n"
        "def test_a(): assert True\n"
        "@pytest.mark.skip(reason='synthetic')\n"
        "def test_b(): assert False\n",
        encoding="utf-8",
    )
    summary = _run(project, ["tests/test_skip.py"], baseline=_write_baseline(project))

    assert summary["_exit_code"] == 0
    assert _outcome(summary, "tests/test_skip.py") == runner.OUTCOME_PASSED
    assert summary["totals"]["skipped"] == 1
    assert summary["totals"]["passed"] == 1


def test_collection_error_fails_the_gate(project: Path):
    (project / "tests" / "test_broken_import.py").write_text(
        "import a_module_that_does_not_exist_anywhere\n"
        "def test_never_runs(): assert True\n",
        encoding="utf-8",
    )
    summary = _run(
        project, ["tests/test_broken_import.py"], baseline=_write_baseline(project)
    )

    assert summary["_exit_code"] == 1
    assert summary["ok"] is False
    assert _outcome(summary, "tests/test_broken_import.py") == runner.OUTCOME_COLLECTION_ERROR


def test_timeout_fails_the_gate_and_is_counted(project: Path):
    (project / "tests" / "test_hang.py").write_text(
        "import time\ndef test_hangs(): time.sleep(120)\n", encoding="utf-8"
    )
    summary = _run(
        project, ["tests/test_hang.py"], baseline=_write_baseline(project), timeout=3
    )

    assert summary["_exit_code"] == 1
    assert _outcome(summary, "tests/test_hang.py") == runner.OUTCOME_TIMEOUT
    assert summary["totals"]["timeouts"] == 1
    attempt = summary["files"][0]["attempts"][0]
    assert attempt["timed_out"] is True
    assert attempt["returncode"] is None


# ──────────────────────────────────────────────────────────────────────────────
# Native exits
# ──────────────────────────────────────────────────────────────────────────────


def test_complete_pass_then_known_native_exit_is_classified(project: Path):
    (project / "tests" / "test_teardown_fault.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    (project / "conftest.py").write_text(
        _exit_after_session_conftest(KNOWN_CODE), encoding="utf-8"
    )
    baseline = _write_baseline(project, known=["tests/test_teardown_fault.py"])
    summary = _run(project, ["tests/test_teardown_fault.py"], baseline=baseline)

    assert summary["_exit_code"] == 0
    assert summary["ok"] is True
    assert _outcome(summary, "tests/test_teardown_fault.py") == runner.OUTCOME_KNOWN_NATIVE_EXIT
    assert summary["totals"]["classified_native_exits"] == 1

    attempt = summary["files"][0]["attempts"][0]
    assert attempt["returncode"] == KNOWN_CODE
    assert attempt["returncode_hex"] == runner.to_hex(KNOWN_CODE)
    assert attempt["complete_result"] is True
    assert attempt["passed"] == 1


def test_unknown_file_native_exit_fails_the_gate(project: Path):
    (project / "tests" / "test_new_crash.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    (project / "conftest.py").write_text(
        _exit_after_session_conftest(KNOWN_CODE), encoding="utf-8"
    )
    # Baseline lists a *different* file: this one has no reviewed classification.
    baseline = _write_baseline(project, known=["tests/test_something_else.py"])
    summary = _run(project, ["tests/test_new_crash.py"], baseline=baseline)

    assert summary["_exit_code"] == 1
    assert summary["ok"] is False
    assert _outcome(summary, "tests/test_new_crash.py") == runner.OUTCOME_UNEXPECTED_NATIVE_EXIT
    assert summary["totals"]["unexpected_exits"] == 1


def test_known_file_with_wrong_native_code_fails_the_gate(project: Path):
    (project / "tests" / "test_wrong_code.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    (project / "conftest.py").write_text(
        _exit_after_session_conftest(OTHER_CODE), encoding="utf-8"
    )
    baseline = _write_baseline(project, known=["tests/test_wrong_code.py"])
    summary = _run(project, ["tests/test_wrong_code.py"], baseline=baseline)

    assert summary["_exit_code"] == 1
    assert (
        _outcome(summary, "tests/test_wrong_code.py")
        == runner.OUTCOME_NATIVE_EXIT_CATEGORY_MISMATCH
    )


def test_known_file_exiting_cleanly_is_success(project: Path):
    """A baseline entry is permission to classify, never a requirement to crash."""
    (project / "tests" / "test_known_clean.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    baseline = _write_baseline(project, known=["tests/test_known_clean.py"])
    summary = _run(project, ["tests/test_known_clean.py"], baseline=baseline)

    assert summary["_exit_code"] == 0
    assert _outcome(summary, "tests/test_known_clean.py") == runner.OUTCOME_PASSED
    assert summary["totals"]["classified_native_exits"] == 0


def test_native_crash_before_any_result_is_rejected_as_incomplete(project: Path):
    (project / "tests" / "test_midrun_death.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    (project / "conftest.py").write_text(
        _exit_during_run_conftest(KNOWN_CODE), encoding="utf-8"
    )
    # Even though the code and the file match the baseline, no complete result
    # was written — classification requires the evidence, not just the code.
    baseline = _write_baseline(project, known=["tests/test_midrun_death.py"])
    summary = _run(project, ["tests/test_midrun_death.py"], baseline=baseline)

    assert summary["_exit_code"] == 1
    assert _outcome(summary, "tests/test_midrun_death.py") == runner.OUTCOME_UNEXPECTED_NATIVE_EXIT
    assert summary["files"][0]["attempts"][0]["complete_result"] is False


# ──────────────────────────────────────────────────────────────────────────────
# Bounded retry (the documented intermittent converter condition, F-11)
# ──────────────────────────────────────────────────────────────────────────────


def test_intermittent_native_failure_is_retried_and_accepted_when_it_passes_cleanly(
    project: Path,
):
    (project / "tests" / "test_flaky_native.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    # Fault on the first attempt only: a marker file makes the second run clean.
    marker = project / "attempted.marker"
    (project / "conftest.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"MARKER = Path(r'{marker}')\n"
        "def pytest_collection_finish(session):\n"
        "    if not MARKER.exists():\n"
        "        MARKER.write_text('1')\n"
        f"        os._exit({_exit_literal(RETRY_CODE)})\n",
        encoding="utf-8",
    )
    baseline = _write_baseline(project, retry=["tests/test_flaky_native.py"])
    summary = _run(project, ["tests/test_flaky_native.py"], baseline=baseline)

    assert summary["_exit_code"] == 0
    assert _outcome(summary, "tests/test_flaky_native.py") == runner.OUTCOME_PASSED_AFTER_RETRY
    assert summary["totals"]["retried_files"] == 1

    attempts = summary["files"][0]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["returncode"] == RETRY_CODE
    assert attempts[0]["complete_result"] is False
    assert attempts[1]["returncode"] == 0
    assert attempts[1]["complete_result"] is True


def test_persistent_intermittent_condition_still_fails_the_gate(project: Path):
    (project / "tests" / "test_always_native.py").write_text(
        "def test_a(): assert True\n", encoding="utf-8"
    )
    (project / "conftest.py").write_text(
        _exit_during_run_conftest(RETRY_CODE), encoding="utf-8"
    )
    baseline = _write_baseline(
        project, retry=["tests/test_always_native.py"], max_attempts=2
    )
    summary = _run(project, ["tests/test_always_native.py"], baseline=baseline)

    assert summary["_exit_code"] == 1
    assert summary["ok"] is False
    assert len(summary["files"][0]["attempts"]) == 2
    assert "persisted across 2 attempts" in summary["files"][0]["reason"]


def test_logical_failure_is_never_retried(project: Path):
    (project / "tests" / "test_logical_fail.py").write_text(
        "def test_bad(): assert False\n", encoding="utf-8"
    )
    baseline = _write_baseline(project, retry=["tests/test_logical_fail.py"])
    summary = _run(project, ["tests/test_logical_fail.py"], baseline=baseline)

    assert summary["_exit_code"] == 1
    assert _outcome(summary, "tests/test_logical_fail.py") == runner.OUTCOME_TESTS_FAILED
    assert len(summary["files"][0]["attempts"]) == 1, "a failing assertion must stay visible"


# ──────────────────────────────────────────────────────────────────────────────
# Classification units
# ──────────────────────────────────────────────────────────────────────────────


def _attempt(**overrides) -> runner.Attempt:
    base = dict(
        attempt=1,
        command=["pytest"],
        duration_seconds=1.0,
        returncode=0,
        returncode_hex="0x00000000",
        timed_out=False,
        complete_result=True,
        collected=1,
        passed=1,
        failed=0,
        skipped=0,
        errors=0,
        junit_xml="x.xml",
        stdout_log="x.out",
        stderr_log="x.err",
    )
    base.update(overrides)
    return runner.Attempt(**base)


def test_exit_zero_without_a_machine_readable_result_is_rejected():
    """A silent success is not evidence of success."""
    outcome, _ = runner.classify_attempt(
        _attempt(complete_result=False, collected=0, passed=0), "tests/t.py", runner.Baseline()
    )
    assert outcome == runner.OUTCOME_INCOMPLETE_RESULT


def test_logical_failure_outranks_a_native_exit():
    """A crash must never be able to hide a real test failure."""
    outcome, _ = runner.classify_attempt(
        _attempt(
            returncode=WIN_ACCESS_VIOLATION,
            returncode_hex="0xC0000005",
            failed=1,
            passed=0,
        ),
        "tests/t.py",
        runner.Baseline(
            known_native_codes=frozenset({WIN_ACCESS_VIOLATION}),
            known_native_files=frozenset({"tests/t.py"}),
        ),
    )
    assert outcome == runner.OUTCOME_TESTS_FAILED


def test_a_reviewed_file_is_accepted_under_any_reviewed_teardown_code():
    """The teardown code is environment-dependent; every reviewed code is accepted.

    A local box reports 0xC0000005, GitHub's windows runner 0xC0000409, and the
    ubuntu runner -11 (SIGSEGV), for the same files. All must be classified, as
    long as a complete passing result was written first.
    """
    baseline = runner.Baseline(
        known_native_codes=frozenset({WIN_ACCESS_VIOLATION, WIN_STACK_BUFFER_OVERRUN, -11}),
        known_native_files=frozenset({"tests/t.py"}),
    )
    for code in (WIN_ACCESS_VIOLATION, WIN_STACK_BUFFER_OVERRUN, -11):
        outcome, _ = runner.classify_attempt(
            _attempt(returncode=code, returncode_hex=runner.to_hex(code)),
            "tests/t.py",
            baseline,
        )
        assert outcome == runner.OUTCOME_KNOWN_NATIVE_EXIT, code


def test_shipped_baseline_reviews_the_linux_teardown_signal():
    """The ubuntu teardown manifests as SIGSEGV (-11); it must be reviewed too."""
    baseline = runner.load_baseline(runner.DEFAULT_BASELINE)
    assert -11 in baseline.known_native_codes


def test_shipped_baseline_retries_the_converter_flake_on_every_platform():
    """The F-11 converter mid-run crash is 0xC0000409 on Windows and SIGSEGV on
    macOS/Linux; both must be retryable or the gate rejects it off-Windows."""
    baseline = runner.load_baseline(runner.DEFAULT_BASELINE)
    assert baseline.allows_retry("tests/test_converter.py", WIN_STACK_BUFFER_OVERRUN)
    assert baseline.allows_retry("tests/test_converter.py", -11)
    # A file that is not the reviewed converter is never retried.
    assert not baseline.allows_retry("tests/test_metadata_backend.py", -11)


def test_a_teardown_code_not_in_the_reviewed_set_still_fails():
    baseline = runner.Baseline(
        known_native_codes=frozenset({WIN_ACCESS_VIOLATION, WIN_STACK_BUFFER_OVERRUN}),
        known_native_files=frozenset({"tests/t.py"}),
    )
    outcome, _ = runner.classify_attempt(
        _attempt(returncode=3221225725, returncode_hex="0xC00000FD"),  # a third code
        "tests/t.py",
        baseline,
    )
    assert outcome == runner.OUTCOME_NATIVE_EXIT_CATEGORY_MISMATCH


def test_a_reviewed_teardown_code_before_a_complete_result_is_still_rejected():
    """The code set never relaxes the complete-passing-result requirement."""
    baseline = runner.Baseline(
        known_native_codes=frozenset({WIN_ACCESS_VIOLATION, WIN_STACK_BUFFER_OVERRUN}),
        known_native_files=frozenset({"tests/t.py"}),
    )
    outcome, _ = runner.classify_attempt(
        _attempt(
            returncode=WIN_STACK_BUFFER_OVERRUN,
            returncode_hex="0xC0000409",
            complete_result=False,
            passed=0,
            collected=0,
        ),
        "tests/t.py",
        baseline,
    )
    assert outcome == runner.OUTCOME_UNEXPECTED_NATIVE_EXIT


def test_no_tests_collected_is_rejected():
    outcome, _ = runner.classify_attempt(
        _attempt(returncode=5, collected=0, passed=0, complete_result=False),
        "tests/t.py",
        runner.Baseline(),
    )
    assert outcome == runner.OUTCOME_NO_TESTS_COLLECTED


def test_to_hex_renders_windows_codes_unsigned():
    assert runner.to_hex(WIN_ACCESS_VIOLATION) == "0xC0000005"
    assert runner.to_hex(WIN_STACK_BUFFER_OVERRUN) == "0xC0000409"
    assert runner.to_hex(0) == "0x00000000"
    assert runner.to_hex(-1073741819) == "0xC0000005", "signed forms render unsigned too"


def test_parse_junit_xml_of_a_missing_file_is_incomplete(tmp_path: Path):
    assert runner.parse_junit_xml(tmp_path / "nope.xml").complete is False


def test_parse_junit_xml_of_a_truncated_file_is_incomplete(tmp_path: Path):
    path = tmp_path / "truncated.xml"
    path.write_text("<testsuites><testsuite tests='3'", encoding="utf-8")
    assert runner.parse_junit_xml(path).complete is False


# ──────────────────────────────────────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────────────────────────────────────


def test_discovery_returns_only_tracked_test_files(tmp_path: Path):
    """Untracked snapshot copies must never enter the release gate."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tracked.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_untracked.py").write_text("def test_b(): pass\n", encoding="utf-8")
    (tmp_path / "tests" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_tracked.py", "tests/helper.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    found = runner.discover_test_files(tmp_path)

    assert found == ["tests/test_tracked.py"]
    assert "tests/test_untracked.py" not in found, "untracked copies must be excluded"
    assert "tests/helper.py" not in found, "only test_*.py files are collected"


def test_discovery_finds_the_real_suite():
    found = runner.discover_test_files(REPO_ROOT)

    assert len(found) > 100, "the tracked suite should be discovered in full"
    assert "tests/test_packaging.py" in found
    assert all(entry.startswith("tests/test_") for entry in found)
    assert all(entry.endswith(".py") for entry in found)
    assert found == sorted(found), "order must be deterministic"


def test_shipped_baseline_is_valid_and_reviewed():
    """The committed baseline is evidence; a typo in it would silently widen the gate."""
    baseline = runner.load_baseline(runner.DEFAULT_BASELINE)
    tracked = set(runner.discover_test_files(REPO_ROOT))

    # Both machine-dependent teardown codes are reviewed and accepted.
    assert WIN_ACCESS_VIOLATION in baseline.known_native_codes
    assert WIN_STACK_BUFFER_OVERRUN in baseline.known_native_codes
    assert WIN_STACK_BUFFER_OVERRUN in baseline.retry_native_codes
    assert baseline.retry_max_attempts >= 2
    for entry in baseline.known_native_files | baseline.retry_native_files:
        assert entry in tracked, f"baseline names {entry}, which is not a tracked test file"


def test_missing_baseline_classifies_nothing(tmp_path: Path):
    """Losing the baseline must make the gate stricter, never more permissive."""
    baseline = runner.load_baseline(tmp_path / "absent.json")

    assert baseline.known_native_files == frozenset()
    assert baseline.known_native_codes == frozenset()
    assert baseline.allows_known_native("tests/anything.py", KNOWN_CODE) is False
    assert baseline.max_attempts_for("tests/anything.py") == 1


# ──────────────────────────────────────────────────────────────────────────────
# Untracked test files must not be skipped in silence
# ──────────────────────────────────────────────────────────────────────────────


def _git_project(tmp_path: Path) -> Path:
    """A real (tiny) git repo, since discovery shells out to git ls-files."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    return tmp_path


def _run_with_discovery(project: Path, baseline: Path, *extra: str) -> int:
    """Invoke the gate WITHOUT --files, so real discovery runs."""
    return runner.main(
        [
            "--repo-root", str(project),
            "--evidence-dir", str(project / "evidence"),
            "--baseline", str(baseline),
            "--timeout", "120",
            *extra,
        ]
    )


def test_untracked_test_file_blocks_the_gate(tmp_path: Path, capsys):
    """Discovery is tracked-only, so an un-added test file runs nowhere --
    and the gate would still print a confident PASS. That green result is
    worse than a red one: it looks like evidence for tests that never
    executed. Observed for real while adding tests/test_community_health_
    files.py, where a clean '134 files PASS' covered none of it."""
    project = _git_project(tmp_path)
    (project / "tests" / "test_tracked.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_tracked.py"], cwd=project, check=True)
    (project / "tests" / "test_brand_new.py").write_text("def test_b(): assert True\n", encoding="utf-8")

    baseline = _write_baseline(tmp_path)
    code = _run_with_discovery(project, baseline)

    assert code == 1, "an untracked test file must fail the gate, not be skipped quietly"
    err = capsys.readouterr().err
    assert "tests/test_brand_new.py" in err, "the offending file must be named"
    assert "git add" in err, "the message must say how to fix it"
    assert not (project / "evidence" / "isolated_results.json").exists(), (
        "the gate must refuse before producing a results file that would "
        "read as evidence"
    )


def test_untracked_test_file_can_be_explicitly_allowed(tmp_path: Path, capsys):
    project = _git_project(tmp_path)
    (project / "tests" / "test_tracked.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_tracked.py"], cwd=project, check=True)
    (project / "tests" / "test_brand_new.py").write_text("def test_b(): assert True\n", encoding="utf-8")

    baseline = _write_baseline(tmp_path)
    code = _run_with_discovery(project, baseline, "--allow-untracked-tests")

    assert code == 0, "the opt-out must let a deliberate omission through"
    out = capsys.readouterr().out
    assert "WARNING" in out and "tests/test_brand_new.py" in out, (
        "even when allowed, the omission must stay visible"
    )
    summary = json.loads((project / "evidence" / "isolated_results.json").read_text(encoding="utf-8"))
    assert len(summary["files"]) == 1, "only the tracked file should have run"


def test_a_fully_tracked_tests_directory_runs_clean(tmp_path: Path):
    project = _git_project(tmp_path)
    (project / "tests" / "test_tracked.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_tracked.py"], cwd=project, check=True)

    baseline = _write_baseline(tmp_path)
    assert _run_with_discovery(project, baseline) == 0


def test_gitignored_test_files_are_not_reported_as_untracked(tmp_path: Path):
    """The tracked-only rule exists to keep .codex_visual_qa/ snapshot
    copies out of the gate. The new check must not undo that by flagging
    every ignored file as a blocking omission."""
    project = _git_project(tmp_path)
    (project / "tests" / "test_tracked.py").write_text("def test_a(): assert True\n", encoding="utf-8")
    (project / ".gitignore").write_text("tests/test_snapshot_*.py\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_tracked.py", ".gitignore"], cwd=project, check=True)
    (project / "tests" / "test_snapshot_copy.py").write_text("def test_c(): assert True\n", encoding="utf-8")

    assert runner.discover_untracked_test_files(project) == [], (
        "an ignored test file is deliberately excluded, not a forgotten one"
    )
    assert _run_with_discovery(project, _write_baseline(tmp_path)) == 0
