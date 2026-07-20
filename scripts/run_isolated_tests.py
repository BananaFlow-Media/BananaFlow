#!/usr/bin/env python3
"""Fresh-process-per-file test runner — the authoritative Windows release gate.

Why this exists (finding F-16)
------------------------------
``python -m pytest tests/`` runs all 117 test files in **one** Python process.
On Windows that process reliably dies at ``0xC0000005`` partway through, without
printing a summary, because Qt state accumulates across GUI test files until
teardown faults. The crash *relocates* when the blamed file is excluded, which
is the evidence that it is accumulated state rather than a defective test
(measured at the tag-editor project baseline).

The consequence was that ``scripts/build_windows.ps1`` and the Windows CI job
could never pass their own test gate, and reported a native crash as "unit tests
failed" — an untruthful gate.

This runner executes every tracked test file in its own interpreter, so Qt state
cannot accumulate, and captures the **real** child return code. Nothing is piped
into another process, so no other program's exit code can shadow pytest's.

What it will and will not tolerate
----------------------------------
It is a gate, not a crash-hider. The run fails on a logical test failure, a
collection error, a timeout, a missing or incomplete pytest result, an
unexpected native exit, a newly crashing file, or a file whose exit category
differs from its reviewed classification.

Two conditions are *classified* rather than treated as gate failures, and only
against ``scripts/isolated_test_baseline.json`` — a reviewed, committed record
of behaviour that Phase 15 measured and proved pre-existing:

1. ``known_native_exit`` — files that print a **complete passing** pytest result
   and *then* fault in Qt's interpreter teardown. Accepted only when the file is
   in the baseline, every test in it completed and passed, a machine-readable
   result exists, and the native code matches the reviewed one. A baseline file
   that exits cleanly is a success, not a violation.
2. ``intermittent_native_retry`` — the documented converter condition (F-11),
   which crashes *mid-run* roughly one run in six, before any summary. Retried a
   bounded number of times, every attempt recorded, and accepted only on clean,
   complete, passing evidence. A logical failure is never retried.

There is deliberately no ``os._exit``, no ignored return code, no blanket skip,
no pytest exclusion, and no acceptance of "any nonzero code is fine on Windows".

Usage
-----
    python scripts/run_isolated_tests.py
    python scripts/run_isolated_tests.py --evidence-dir out --timeout 300
    python scripts/run_isolated_tests.py --files tests/test_converter.py

Exit code is 0 only when every file reached an accepted classification.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "isolated_test_baseline.json"
DEFAULT_TIMEOUT_SECONDS = 600

# pytest's own documented exit codes. Anything outside this range on Windows is
# the OS reporting how the process died, not pytest reporting a result.
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
PYTEST_INTERRUPTED = 2
PYTEST_INTERNAL_ERROR = 3
PYTEST_USAGE_ERROR = 4
PYTEST_NO_TESTS_COLLECTED = 5
PYTEST_EXIT_CODES = frozenset(range(0, 6))

# Accepted outcomes — every other outcome fails the gate.
OUTCOME_PASSED = "passed"
OUTCOME_PASSED_AFTER_RETRY = "passed_after_retry"
OUTCOME_KNOWN_NATIVE_EXIT = "known_native_exit"
ACCEPTED_OUTCOMES = frozenset(
    {OUTCOME_PASSED, OUTCOME_PASSED_AFTER_RETRY, OUTCOME_KNOWN_NATIVE_EXIT}
)

# Failing outcomes.
OUTCOME_TESTS_FAILED = "tests_failed"
OUTCOME_COLLECTION_ERROR = "collection_error"
OUTCOME_NO_TESTS_COLLECTED = "no_tests_collected"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_INCOMPLETE_RESULT = "incomplete_result"
OUTCOME_UNEXPECTED_NATIVE_EXIT = "unexpected_native_exit"
OUTCOME_NATIVE_EXIT_CATEGORY_MISMATCH = "native_exit_category_mismatch"
OUTCOME_USAGE_ERROR = "usage_error"


def to_hex(code: int) -> str:
    """Render a process exit code the way Windows reports it (unsigned 32-bit)."""
    return f"0x{code & 0xFFFFFFFF:08X}"


# ─────────────────────────────────────────────────────────────────────────────
# Baseline
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Baseline:
    """The reviewed, committed record of classified native behaviour.

    ``known_native_codes`` is a *set*, not a single value, because the post-
    teardown fault is the same phenomenon reported under different STATUS codes
    on different machines: a local Windows box reports ``0xC0000005`` while the
    GitHub ``windows-latest`` runner reports ``0xC0000409`` for the very same
    files, and which one appears even varies run to run. Both are reviewed,
    evidence-backed teardown codes; the safety property that makes them safe to
    classify is the **complete passing result written before the fault**, which
    the classifier enforces separately — the code set never relaxes that.
    """

    known_native_codes: frozenset[int] = frozenset()
    known_native_files: frozenset[str] = frozenset()
    retry_native_codes: frozenset[int] = frozenset()
    retry_native_files: frozenset[str] = frozenset()
    retry_max_attempts: int = 1

    def allows_known_native(self, rel_path: str, code: int) -> bool:
        return rel_path in self.known_native_files and code in self.known_native_codes

    def is_known_native_file(self, rel_path: str) -> bool:
        return rel_path in self.known_native_files

    def allows_retry(self, rel_path: str, code: int) -> bool:
        return rel_path in self.retry_native_files and code in self.retry_native_codes

    def max_attempts_for(self, rel_path: str) -> int:
        return self.retry_max_attempts if rel_path in self.retry_native_files else 1

    @property
    def known_codes_hex(self) -> str:
        return ", ".join(to_hex(c) for c in sorted(self.known_native_codes)) or "(none)"


def _codes(entry: dict) -> frozenset[int]:
    """Read a reviewed code set, accepting both ``code`` and ``codes`` forms."""
    values: list[int] = []
    if entry.get("code") is not None:
        values.append(int(entry["code"]))
    for value in entry.get("codes") or ():
        values.append(int(value))
    return frozenset(values)


def load_baseline(path: Path) -> Baseline:
    """Load the reviewed baseline. A missing file means 'classify nothing'."""
    if not path.exists():
        return Baseline()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = raw.get("known_native_exit") or {}
    retry = raw.get("intermittent_native_retry") or {}
    return Baseline(
        known_native_codes=_codes(known),
        known_native_files=frozenset(known.get("files") or ()),
        retry_native_codes=_codes(retry),
        retry_native_files=frozenset(retry.get("files") or ()),
        retry_max_attempts=int(retry.get("max_attempts", 1)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────


def discover_test_files(repo_root: Path) -> list[str]:
    """Return tracked ``tests/test_*.py`` paths, sorted, as POSIX-relative strings.

    Tracked-only on purpose: untracked snapshot copies and the detached QA
    worktree under ``.codex_visual_qa/`` must never be collected into the
    release gate.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "tests/test_*.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    files = [entry for entry in result.stdout.split("\0") if entry.strip()]
    return sorted(files)


def discover_untracked_test_files(repo_root: Path) -> list[str]:
    """Return untracked, non-ignored ``tests/test_*.py`` paths.

    Discovery above is tracked-only by design, which has a sharp edge: a
    brand-new test file that has not been ``git add``-ed is skipped in
    silence, and the gate reports a confident PASS that never executed it.
    That is the most dangerous shape a green result can take -- it looks
    like evidence and is not. This is what ``main`` blocks on.

    ``--exclude-standard`` keeps .gitignore'd paths out, so the snapshot
    copies under ``.codex_visual_qa/`` stay excluded exactly as before.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", "tests/test_*.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return sorted(entry for entry in result.stdout.split("\0") if entry.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Result parsing
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PytestResult:
    """What pytest managed to record, independent of how the process died."""

    complete: bool = False
    collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0

    @property
    def all_passed(self) -> bool:
        return self.complete and self.failed == 0 and self.errors == 0

    @property
    def has_logical_failure(self) -> bool:
        return self.failed > 0 or self.errors > 0


def parse_junit_xml(path: Path) -> PytestResult:
    """Parse pytest's JUnit XML.

    A readable ``<testsuite>`` means pytest reached session finish and wrote a
    complete result — which is exactly what separates a post-summary teardown
    fault (complete) from a mid-run crash (nothing written). Uses only the
    stdlib, so this adds no project dependency.
    """
    if not path.exists():
        return PytestResult(complete=False)
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return PytestResult(complete=False)
    root = tree.getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return PytestResult(complete=False)

    total = int(suite.get("tests", 0))
    failures = int(suite.get("failures", 0))
    errors = int(suite.get("errors", 0))
    skipped = int(suite.get("skipped", 0))
    return PytestResult(
        complete=True,
        collected=total,
        passed=total - failures - errors - skipped,
        failed=failures,
        skipped=skipped,
        errors=errors,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Execution
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Attempt:
    """One fresh-process execution of one test file. Every attempt is recorded."""

    attempt: int
    command: list[str]
    duration_seconds: float
    returncode: int | None
    returncode_hex: str
    timed_out: bool
    complete_result: bool
    collected: int
    passed: int
    failed: int
    skipped: int
    errors: int
    junit_xml: str | None
    stdout_log: str | None
    stderr_log: str | None
    stdout_tail: str = ""


def build_env(repo_root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The supported test environment, matching the documented local command."""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    existing = env.get("PYTHONPATH", "")
    root = str(repo_root)
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    if extra:
        env.update(extra)
    return env


def run_once(
    rel_path: str,
    *,
    repo_root: Path,
    evidence_dir: Path,
    timeout: int,
    attempt: int,
    env_extra: dict[str, str] | None = None,
) -> Attempt:
    """Run one test file in a fresh interpreter and capture its real exit code.

    ``sys.executable`` — the interpreter running this script — is used
    deliberately, so the gate can never silently test a different Python than
    the one the build uses.
    """
    stem = rel_path.replace("/", "_").replace("\\", "_").removesuffix(".py")
    suffix = "" if attempt == 1 else f".attempt{attempt}"
    junit = evidence_dir / "junit" / f"{stem}{suffix}.xml"
    stdout_log = evidence_dir / "logs" / f"{stem}{suffix}.out.txt"
    stderr_log = evidence_dir / "logs" / f"{stem}{suffix}.err.txt"
    junit.parent.mkdir(parents=True, exist_ok=True)
    stdout_log.parent.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "pytest",
        rel_path,
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit}",
    ]

    started = time.monotonic()
    timed_out = False
    # Popen + communicate(timeout) rather than a shell pipeline: piping pytest
    # into another process would replace pytest's exit code with that process's,
    # which is the whole failure mode this runner exists to avoid.
    process = subprocess.Popen(
        command,
        cwd=str(repo_root),
        env=build_env(repo_root, env_extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        out, err = process.communicate(timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        out, err = process.communicate()
        returncode = None
    duration = time.monotonic() - started

    stdout_log.write_text(out or "", encoding="utf-8")
    stderr_log.write_text(err or "", encoding="utf-8")
    parsed = parse_junit_xml(junit)

    return Attempt(
        attempt=attempt,
        command=command,
        duration_seconds=round(duration, 2),
        returncode=returncode,
        returncode_hex="" if returncode is None else to_hex(returncode),
        timed_out=timed_out,
        complete_result=parsed.complete,
        collected=parsed.collected,
        passed=parsed.passed,
        failed=parsed.failed,
        skipped=parsed.skipped,
        errors=parsed.errors,
        junit_xml=str(junit.relative_to(evidence_dir)) if junit.exists() else None,
        stdout_log=str(stdout_log.relative_to(evidence_dir)),
        stderr_log=str(stderr_log.relative_to(evidence_dir)),
        stdout_tail="\n".join((out or "").splitlines()[-15:]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────


def classify_attempt(attempt: Attempt, rel_path: str, baseline: Baseline) -> tuple[str, str]:
    """Map one attempt to an outcome plus a human-readable reason.

    The order matters: a logical failure is reported as a logical failure even
    if the process later died natively, so a crash can never mask a real bug.
    """
    if attempt.timed_out:
        return OUTCOME_TIMEOUT, f"no result after {attempt.duration_seconds}s"

    code = attempt.returncode
    assert code is not None  # only a timeout leaves this unset

    # A recorded logical failure outranks every native consideration.
    if attempt.failed or attempt.errors:
        if attempt.errors and not attempt.failed:
            return (
                OUTCOME_COLLECTION_ERROR,
                f"{attempt.errors} error(s) recorded",
            )
        return (
            OUTCOME_TESTS_FAILED,
            f"{attempt.failed} failed, {attempt.errors} error(s)",
        )

    if code in PYTEST_EXIT_CODES:
        if code == PYTEST_OK:
            if not attempt.complete_result:
                return (
                    OUTCOME_INCOMPLETE_RESULT,
                    "pytest exited 0 but wrote no machine-readable result",
                )
            return OUTCOME_PASSED, f"{attempt.passed} passed, {attempt.skipped} skipped"
        if code == PYTEST_TESTS_FAILED:
            return OUTCOME_TESTS_FAILED, "pytest reported test failures"
        if code == PYTEST_INTERRUPTED:
            return OUTCOME_COLLECTION_ERROR, "pytest was interrupted (exit 2)"
        if code == PYTEST_INTERNAL_ERROR:
            return OUTCOME_COLLECTION_ERROR, "pytest internal error (exit 3)"
        if code == PYTEST_USAGE_ERROR:
            return OUTCOME_USAGE_ERROR, "pytest usage error (exit 4)"
        return OUTCOME_NO_TESTS_COLLECTED, "pytest collected no tests (exit 5)"

    # Outside pytest's range: the OS is reporting how the process died.
    if not attempt.complete_result:
        return (
            OUTCOME_UNEXPECTED_NATIVE_EXIT,
            f"died at {attempt.returncode_hex} before writing a complete result",
        )
    if baseline.allows_known_native(rel_path, code):
        return (
            OUTCOME_KNOWN_NATIVE_EXIT,
            f"complete pass ({attempt.passed} passed), then the reviewed "
            f"teardown exit {attempt.returncode_hex}",
        )
    if baseline.is_known_native_file(rel_path):
        return (
            OUTCOME_NATIVE_EXIT_CATEGORY_MISMATCH,
            f"exited {attempt.returncode_hex}, but its reviewed codes are "
            f"{baseline.known_codes_hex}",
        )
    return (
        OUTCOME_UNEXPECTED_NATIVE_EXIT,
        f"passed but exited {attempt.returncode_hex}; this file has no reviewed "
        f"native-exit classification",
    )


@dataclass
class FileReport:
    path: str
    outcome: str
    reason: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.outcome in ACCEPTED_OUTCOMES

    @property
    def final(self) -> Attempt:
        return self.attempts[-1]


def run_file(
    rel_path: str,
    *,
    repo_root: Path,
    evidence_dir: Path,
    timeout: int,
    baseline: Baseline,
    env_extra: dict[str, str] | None = None,
) -> FileReport:
    """Run one file, retrying only the reviewed intermittent native condition."""
    attempts: list[Attempt] = []
    max_attempts = baseline.max_attempts_for(rel_path)

    for index in range(1, max_attempts + 1):
        attempt = run_once(
            rel_path,
            repo_root=repo_root,
            evidence_dir=evidence_dir,
            timeout=timeout,
            attempt=index,
            env_extra=env_extra,
        )
        attempts.append(attempt)
        outcome, reason = classify_attempt(attempt, rel_path, baseline)

        if outcome in ACCEPTED_OUTCOMES:
            if index > 1 and outcome == OUTCOME_PASSED:
                outcome = OUTCOME_PASSED_AFTER_RETRY
                reason = f"{reason} (clean on attempt {index} of {max_attempts})"
            return FileReport(rel_path, outcome, reason, attempts)

        # Retry only a native-only process failure of the reviewed file and
        # reviewed code. A logical failure, a timeout or an incomplete non-native
        # result is never retried — a flaky assertion must stay visible.
        retryable = (
            attempt.returncode is not None
            and attempt.returncode not in PYTEST_EXIT_CODES
            and not attempt.failed
            and not attempt.errors
            and baseline.allows_retry(rel_path, attempt.returncode)
        )
        if not retryable or index == max_attempts:
            if index > 1:
                reason = f"{reason} (persisted across {index} attempts)"
            return FileReport(rel_path, outcome, reason, attempts)

    raise AssertionError("unreachable")  # pragma: no cover


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def build_summary(reports: Sequence[FileReport], duration: float) -> dict:
    totals = {
        "files": len(reports),
        "collected": sum(r.final.collected for r in reports),
        "passed": sum(r.final.passed for r in reports),
        "failed": sum(r.final.failed for r in reports),
        "skipped": sum(r.final.skipped for r in reports),
        "errors": sum(r.final.errors for r in reports),
        "timeouts": sum(1 for r in reports if r.outcome == OUTCOME_TIMEOUT),
        "retried_files": sum(1 for r in reports if len(r.attempts) > 1),
        "classified_native_exits": sum(
            1 for r in reports if r.outcome == OUTCOME_KNOWN_NATIVE_EXIT
        ),
        "unexpected_exits": sum(
            1
            for r in reports
            if r.outcome
            in {OUTCOME_UNEXPECTED_NATIVE_EXIT, OUTCOME_NATIVE_EXIT_CATEGORY_MISMATCH}
        ),
        "rejected_files": sum(1 for r in reports if not r.accepted),
    }
    return {
        "schema": 1,
        "ok": all(r.accepted for r in reports),
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
        "duration_seconds": round(duration, 2),
        "totals": totals,
        "files": [
            {
                "path": r.path,
                "outcome": r.outcome,
                "reason": r.reason,
                "attempts": [asdict(a) for a in r.attempts],
            }
            for r in reports
        ],
    }


def print_report(summary: dict, reports: Sequence[FileReport]) -> None:
    totals = summary["totals"]
    rejected = [r for r in reports if not r.accepted]
    classified = [r for r in reports if r.outcome == OUTCOME_KNOWN_NATIVE_EXIT]
    retried = [r for r in reports if len(r.attempts) > 1]

    print()
    print("=" * 72)
    print("Isolated test gate — fresh process per file")
    print("=" * 72)
    print(f"  files                    : {totals['files']}")
    print(f"  collected                : {totals['collected']}")
    print(f"  passed                   : {totals['passed']}")
    print(f"  failed                   : {totals['failed']}")
    print(f"  skipped                  : {totals['skipped']}")
    print(f"  errors                   : {totals['errors']}")
    print(f"  timeouts                 : {totals['timeouts']}")
    print(f"  retried files            : {totals['retried_files']}")
    print(f"  classified native exits  : {totals['classified_native_exits']}")
    print(f"  unexpected exits         : {totals['unexpected_exits']}")
    print(f"  duration                 : {summary['duration_seconds']}s")

    if classified:
        print()
        print("Classified native exits (complete pass, then reviewed teardown fault):")
        for report in classified:
            print(f"  - {report.path}: {report.reason}")

    if retried:
        print()
        print("Retried files (reviewed intermittent native condition):")
        for report in retried:
            for attempt in report.attempts:
                code = "timeout" if attempt.timed_out else attempt.returncode_hex
                print(
                    f"  - {report.path} attempt {attempt.attempt}: {code} "
                    f"({attempt.duration_seconds}s)"
                )

    if rejected:
        print()
        print("FAILURES:")
        for report in rejected:
            print(f"  - {report.path} [{report.outcome}] {report.reason}")
            tail = report.final.stdout_tail.strip()
            if tail:
                for line in tail.splitlines():
                    print(f"        {line}")
    print()
    print("RESULT: " + ("PASS" if summary["ok"] else "FAIL"))
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--evidence-dir",
        default=str(REPO_ROOT / "test-evidence"),
        help="where per-file JUnit XML, logs and the JSON aggregate are written",
    )
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help="reviewed native-exit classification file",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-file timeout in seconds",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="run only these files (default: every tracked tests/test_*.py)",
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="repository root (tracked-file discovery runs here)",
    )
    parser.add_argument(
        "--allow-untracked-tests",
        action="store_true",
        help=(
            "downgrade the untracked-test-file check from an error to a "
            "warning. The gate is tracked-only, so an un-added test file is "
            "silently skipped; only use this when that is genuinely intended"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline(Path(args.baseline))

    files = list(args.files) if args.files else discover_test_files(repo_root)
    if not files:
        print("ERROR: no tracked test files were discovered.", file=sys.stderr)
        return 1

    # An untracked test file is invisible to this gate. Refuse to report a
    # PASS that quietly excluded it -- a green result that skipped the very
    # tests someone just wrote is worse than a red one.
    if not args.files:
        untracked = discover_untracked_test_files(repo_root)
        if untracked:
            stream = sys.stdout if args.allow_untracked_tests else sys.stderr
            label = "WARNING" if args.allow_untracked_tests else "ERROR"
            print(f"{label}: {len(untracked)} untracked test file(s) would NOT run:", file=stream)
            for path in untracked:
                print(f"    {path}", file=stream)
            print(
                "\n    This gate discovers tracked files only (git ls-files), so\n"
                "    these are skipped in silence and the result below would not\n"
                "    cover them.\n"
                "      -> git add them, then re-run; or pass\n"
                "         --allow-untracked-tests if the omission is intended.",
                file=stream,
            )
            if not args.allow_untracked_tests:
                return 1
            print()

    print(f"==> Isolated test gate: {len(files)} tracked file(s)")
    print(f"    interpreter : {sys.executable}")
    print(f"    evidence    : {evidence_dir}")
    print(f"    baseline    : {args.baseline}")
    print()

    started = time.monotonic()
    reports: list[FileReport] = []
    for index, rel_path in enumerate(files, start=1):
        print(f"[{index:3d}/{len(files)}] {rel_path} ... ", end="", flush=True)
        report = run_file(
            rel_path,
            repo_root=repo_root,
            evidence_dir=evidence_dir,
            timeout=args.timeout,
            baseline=baseline,
        )
        reports.append(report)
        marker = "ok" if report.accepted else "FAIL"
        print(f"{marker} [{report.outcome}] {report.final.duration_seconds}s")
    duration = time.monotonic() - started

    summary = build_summary(reports, duration)
    aggregate = evidence_dir / "isolated_results.json"
    aggregate.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_report(summary, reports)
    print(f"Machine-readable aggregate: {aggregate}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
