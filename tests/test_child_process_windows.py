"""Every child process must start hidden, and must be recorded.

The packaged Windows build is compiled with ``console=False``. A process
with no console that starts a console-subsystem program (``icacls``,
``ffmpeg``, ``deno``, ``node``, …) makes Windows allocate and *show* a
new console for the child. Enough of those in sequence is
indistinguishable from malware to a user watching their first launch.

These tests are the gate: they assert the central runner sets the right
flags, that it records what ran, and that no module reintroduces a raw
``subprocess`` launch that bypasses it.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from utils import proc

REPO_ROOT = Path(__file__).resolve().parents[1]

# Packages whose launch sites must all go through utils.proc.
_SCANNED_PACKAGES = ("core", "ui", "utils")

# Call sites that legitimately do not create a console window.
_ALLOWED = {
    # Launches the Windows shell, which is a GUI program by definition.
    ("ui/services/file_operation_service.py", "Popen"),
}


# ──────────────────────────────────────────────────────────────────────────────
# The runner itself
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name != "nt", reason="console windows are a Windows concept")
def test_no_window_kwargs_requests_a_hidden_child():
    kwargs = proc.no_window_kwargs()

    assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    startupinfo = kwargs["startupinfo"]
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(os.name != "nt", reason="console windows are a Windows concept")
def test_new_process_group_is_opt_in():
    assert not (
        proc.no_window_kwargs()["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    )
    assert (
        proc.no_window_kwargs(new_process_group=True)["creationflags"]
        & subprocess.CREATE_NEW_PROCESS_GROUP
    )


def test_run_hidden_reports_success_without_raising():
    result = proc.run_hidden(
        [sys.executable, "-c", "print('ok')"], purpose="unit-test", timeout=60,
    )

    assert result.ok
    assert result.returncode == 0
    assert "ok" in result.stdout
    assert result.duration_ms >= 0
    assert result.purpose == "unit-test"


def test_run_hidden_reports_failure_instead_of_raising(caplog):
    with caplog.at_level(logging.WARNING, logger="bananaflow.proc"):
        result = proc.run_hidden(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            purpose="unit-test-failure",
            timeout=60,
        )

    assert not result.ok
    assert result.returncode == 3
    # The failure has to be diagnosable from the log alone.
    record = " ".join(r.getMessage() for r in caplog.records)
    assert "unit-test-failure" in record
    assert "rc=3" in record
    assert "boom" in record


def test_run_hidden_reports_a_missing_executable_as_an_error(caplog):
    with caplog.at_level(logging.ERROR, logger="bananaflow.proc"):
        result = proc.run_hidden(
            ["bananaflow-no-such-program"], purpose="unit-test-missing", timeout=10,
        )

    assert not result.ok
    assert result.error
    assert not result.timed_out
    assert "LAUNCH-FAILED" in " ".join(r.getMessage() for r in caplog.records)


def test_run_hidden_reports_a_timeout_distinctly():
    result = proc.run_hidden(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        purpose="unit-test-timeout",
        timeout=1,
    )

    assert result.timed_out
    assert not result.ok
    assert "timed out" in result.error


def test_diagnostics_never_record_a_secret(caplog):
    """A command line carrying a token must not reach the log verbatim."""
    with caplog.at_level(logging.WARNING, logger="bananaflow.proc"):
        proc.run_hidden(
            [
                sys.executable, "-c", "import sys; sys.exit(1)",
                "--proxy", "https://user:hunter2@proxy.example:8080",
                "--url", "https://example.test/v?token=SUPERSECRETVALUE",
            ],
            purpose="unit-test-redaction",
            timeout=60,
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "SUPERSECRETVALUE" not in logged
    assert "[REDACTED]" in logged


# ──────────────────────────────────────────────────────────────────────────────
# No call site may bypass the runner
# ──────────────────────────────────────────────────────────────────────────────

def _launch_calls(tree: ast.AST):
    """Yield (node, attribute) for every subprocess launch in ``tree``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {"run", "Popen", "call", "check_call", "check_output"}:
            continue
        value = func.value
        if isinstance(value, ast.Name) and value.id == "subprocess":
            yield node, func.attr


def _has_hidden_flags(node: ast.Call) -> bool:
    names = {kw.arg for kw in node.keywords if kw.arg}
    return bool(names & {"creationflags", "startupinfo"}) or any(
        kw.arg is None for kw in node.keywords          # **_popen_kwargs()
    )


def test_no_module_launches_a_child_without_hiding_its_window():
    offenders: list[str] = []

    for package in _SCANNED_PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node, attribute in _launch_calls(tree):
                if (relative, attribute) in _ALLOWED:
                    continue
                if _has_hidden_flags(node):
                    continue
                offenders.append(f"{relative}:{node.lineno} subprocess.{attribute}")

    assert not offenders, (
        "These call sites start a child process without suppressing its "
        "console window. Route them through utils.proc.run_hidden / "
        "popen_hidden, or add the platform kwargs from "
        "utils.proc.no_window_kwargs():\n  " + "\n  ".join(offenders)
    )
