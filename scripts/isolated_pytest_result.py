"""Early, strict per-file result for the isolated pytest runner.

Pytest's built-in JUnit writer runs at session finish. A native Qt teardown
fault can occur after the final test's teardown report but before that hook,
leaving no machine-readable evidence even though every test completed. This
plugin writes an equivalent minimal result as soon as the final teardown report
arrives. It never turns a failed setup, call or teardown into a pass.
"""

from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree as ET


_total = 0
_finished: set[str] = set()
_failed_calls: set[str] = set()
_errors: set[str] = set()
_skipped: set[str] = set()


def pytest_collection_finish(session) -> None:
    """Record the exact number of collected tests for this child process."""
    global _total
    _total = len(session.items)


def pytest_runtest_logreport(report) -> None:
    """Write only after every collected test has completed its teardown."""
    if report.failed:
        if report.when == "call":
            _failed_calls.add(report.nodeid)
        else:
            _errors.add(report.nodeid)
    elif report.skipped and report.when == "call":
        _skipped.add(report.nodeid)

    if report.when != "teardown":
        return
    _finished.add(report.nodeid)
    if _total and len(_finished) == _total:
        _write_result()


def _write_result() -> None:
    target = os.environ.get("BANANAFLOW_ISOLATED_JUNIT")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    suite = ET.Element(
        "testsuite",
        tests=str(_total),
        failures=str(len(_failed_calls)),
        errors=str(len(_errors)),
        skipped=str(len(_skipped)),
    )
    temporary = path.with_suffix(path.suffix + ".partial")
    ET.ElementTree(suite).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(path)
