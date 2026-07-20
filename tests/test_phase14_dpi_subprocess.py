"""Phase 14 corrective pass: fresh-process DPI matrix.

Each QT_SCALE_FACTOR must be exercised in its own interpreter -- Qt reads the
scale factor once when QApplication is constructed, so reusing a process
across factors would silently test only the first one. This replaces the
removed ``test_scale_is_identity_at_the_authored_baseline_and_grows_with_dpi``,
which asserted that 100 logical pixels become 200 logical pixels at 192 DPI:
that was the defect (manual DPI multiplication of ordinary widget geometry),
not the desired behaviour.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = Path(__file__).resolve().parent / "_phase14_dpi_probe.py"

SCALE_FACTORS = ["1", "1.25", "1.5", "1.75", "2"]


@pytest.mark.parametrize("scale_factor", SCALE_FACTORS)
def test_widget_geometry_is_not_double_scaled(scale_factor):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_SCALE_FACTOR"] = scale_factor
    env.pop("QT_AUTO_SCREEN_SCALE_FACTOR", None)
    env["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    # Decode as UTF-8 explicitly. qfluentwidgets prints a startup banner
    # containing an emoji (📢) to stdout on import; under a non-UTF-8
    # Windows locale (e.g. cp1252) the default text=True decoder raises
    # UnicodeDecodeError on that byte in the pipe-reader thread, the marker
    # is lost, and the check fails for a reason that has nothing to do with
    # DPI. Pinning the codec makes the probe's output readable everywhere.
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90)

    # The success marker is written and flushed before any interpreter
    # teardown, and this Windows/offscreen/Qt combination has a known,
    # pre-existing, non-deterministic native exit during shutdown (recorded
    # in the project's accessibility/DPI hardening records
    # and already tolerated the same way by the other panel-constructing
    # Phase 14 tests). A native exit *after* the marker is not a check
    # failure; a native exit or an assertion failure *before* it is.
    if "PHASE14-DPI-OK" not in result.stdout:
        assert result.returncode == 0, (
            f"DPI probe failed at QT_SCALE_FACTOR={scale_factor} before "
            f"completing its checks\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}")
        assert False, (
            f"DPI probe exited 0 without printing its success marker at "
            f"QT_SCALE_FACTOR={scale_factor}\nstdout:\n{result.stdout}")
