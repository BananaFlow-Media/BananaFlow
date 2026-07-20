"""Phase 4 (reproducible builds / supply chain) — Python dependency
constraints snapshot.

requirements.txt/pyproject.toml intentionally use floor (`>=`) version
bounds, so a routine install can resolve a different exact version on a
different day. constraints-windows-py312.txt is the reviewed-environment
snapshot that lets a release reproduce the exact tested versions; these
tests confirm the generator produces a well-formed file and that the
committed snapshot itself is present and matches that format.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS_PATH = REPO_ROOT / "constraints-windows-py312.txt"
_PIN_RE = re.compile(r"^[A-Za-z0-9._-]+==[A-Za-z0-9.!+_-]+$")


def test_committed_constraints_file_exists_and_is_well_formed():
    assert CONSTRAINTS_PATH.is_file(), (
        "constraints-windows-py312.txt must be committed as release evidence"
    )
    lines = [
        line for line in CONSTRAINTS_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(lines) > 20, "the reviewed environment has many more than 20 packages"
    malformed = [line for line in lines if not _PIN_RE.match(line)]
    assert not malformed, f"every non-comment line must be an exact name==version pin: {malformed}"


def test_committed_constraints_file_pins_pytest_exactly():
    text = CONSTRAINTS_PATH.read_text(encoding="utf-8")
    assert re.search(r"^pytest==\d", text, re.MULTILINE), (
        "a package known to be installed in the reviewed dev environment must appear"
    )


def test_generator_runs_and_produces_a_well_formed_file(tmp_path):
    """The generator must be re-runnable and produce a well-formed pin file
    in any environment.

    This deliberately does not compare the fresh output against the
    committed constraints-windows-py312.txt: that file is a snapshot of one
    specific reviewed environment (a maintainer's Windows/Python 3.12
    venv carrying the release-build toolchain, Playwright, and the
    supply-chain/security tooling used across this project's release
    process — PyInstaller, cyclonedx-python-lib, pip-audit, detect-secrets,
    pywinauto, etc.). No CI job installs that same superset — the test
    matrix here runs `pip install -e ".[dev]"` only — so a freshly
    generated snapshot in CI (on any OS/Python version, including
    Windows 3.12) is *expected* to contain far fewer packages than the
    committed file without that indicating drift or a hand-edited file.
    Regenerating and reviewing the committed snapshot against its actual
    reviewed environment remains a manual, documented step (see
    scripts/generate_constraints.py's module docstring)."""
    out_path = tmp_path / "constraints-check.txt"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "generate_constraints.py"),
         "--out", str(out_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.is_file()

    fresh_lines = [
        line for line in out_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(fresh_lines) > 20, "a dev+test environment has many more than 20 packages"
    malformed = [line for line in fresh_lines if not _PIN_RE.match(line)]
    assert not malformed, f"every non-comment line must be an exact name==version pin: {malformed}"
