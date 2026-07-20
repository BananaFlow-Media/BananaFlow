#!/usr/bin/env python3
"""
scripts/run_network_tests.py — run the opt-in real-network tests.

Most of this project's suite mocks yt-dlp and Playwright, which is the right
default: tests must not depend on YouTube being reachable, unblocked, or
unchanged. But that means the suite structurally cannot notice the one failure
mode that actually breaks this app in the field — YouTube changing something on
their side.

A handful of tests do hit the real network and are gated behind
``BANANAFLOW_RUN_NETWORK_TESTS=1`` so they stay out of ordinary runs. Issue #34
asked for one of them ("would have caught the stale-selector bug directly"),
and it was written — but nothing ever ran it, and nothing documented that it
should be run, so it caught nothing. This script is the thing that runs them:
invoked weekly by .github/workflows/network-tests.yml and required before a
release (see CONTRIBUTING.md, "Real-network tests").

Exit codes: 0 all passed, 1 something failed, 2 nothing was collected (which
usually means the gate variable is not reaching pytest).

Usage:
    python scripts/run_network_tests.py           # run them
    python scripts/run_network_tests.py --list    # show what would run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GATE_ENV_VAR = "BANANAFLOW_RUN_NETWORK_TESTS"

# Test files that contain network-gated tests. Kept explicit rather than
# discovered so that adding a gated test to a new file is a deliberate act
# that also adds it here — a gated test nobody runs is the bug this script
# exists to fix.
NETWORK_TEST_FILES = [
    "tests/test_channel_tab_discoverer_phase6.py",
]


def _pytest_args(list_only: bool) -> list[str]:
    args = [sys.executable, "-m", "pytest", *NETWORK_TEST_FILES, "-v"]
    if list_only:
        args.append("--collect-only")
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="show which tests would run without running them")
    options = parser.parse_args()

    env = dict(os.environ)
    env[GATE_ENV_VAR] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    print(f"==> Running real-network tests ({GATE_ENV_VAR}=1)")
    print("    These hit YouTube directly. A failure can mean a real "
          "regression OR that YouTube is blocking this network — read the "
          "output before concluding which.")
    print()

    result = subprocess.run(_pytest_args(options.list), cwd=REPO_ROOT, env=env)

    if options.list:
        return result.returncode

    if result.returncode == 5:
        print("\nNo tests were collected — the gate variable is not reaching "
              "pytest, so nothing was actually verified.", file=sys.stderr)
        return 2
    if result.returncode != 0:
        print("\n==> Real-network tests FAILED. If this is a release check, "
              "do not ship until it is understood.", file=sys.stderr)
        return 1

    print("\n==> Real-network tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
