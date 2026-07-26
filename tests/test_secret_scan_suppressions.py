"""
tests/test_secret_scan_suppressions.py  –  Secret-scan suppressions must be stable
====================================================================================
The Gitleaks gate (.github/workflows/security.yml) scans the whole history.
A false positive can be silenced two ways, and only one of them survives the
way this repository actually merges work:

  * a ``.gitleaksignore`` fingerprint is ``commit:file:rule:start-line`` —
    pinned to the SHA of the commit that introduced the line. Every one of
    these branches is squash-merged, so that SHA ceases to exist the moment
    the work lands on main: the suppression silently stops matching and the
    "already reviewed and dismissed" finding comes back red, on main, with
    nobody expecting it. It is also useless on a stacked PR whose own
    history doesn't contain the pinned commit at all.
  * removing whatever the scanner matched on — which is the right answer
    when (as here) the match was never a secret, just an identifier that
    happened to sit next to the word "key".

So: fix the source, don't pin a SHA. This test fails if a commit-pinned
suppression is ever added back.

Pure stdlib, no Qt.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# A gitleaks git-scan fingerprint always begins with the full 40-hex commit
# SHA it was generated against.
_COMMIT_PINNED = re.compile(r"^\s*[0-9a-f]{40}:")


def test_no_commit_pinned_gitleaks_suppressions():
    ignore_file = _REPO_ROOT / ".gitleaksignore"
    if not ignore_file.exists():
        return  # nothing suppressed at all — the preferred state

    offenders = [
        line.rstrip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if _COMMIT_PINNED.match(line)
    ]
    assert not offenders, (
        "commit-pinned .gitleaksignore fingerprints do not survive a squash "
        "merge (the introducing commit's SHA changes), so the suppression "
        "silently lapses and the finding reappears on main:\n  "
        + "\n  ".join(offenders)
        + "\nFix the flagged source instead, or use a scoped allowlist regex "
          "in .gitleaks.toml, which is stable across rewrites."
    )
