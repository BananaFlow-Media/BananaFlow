"""Phase 4 (reproducible builds / supply chain) — GitHub Actions SHA pinning.

Every third-party action reference across the workflows must be pinned to
an exact commit SHA, not a mutable version tag: a tag like ``@v4`` can be
force-moved by the action's maintainer (or, in a supply-chain compromise,
by an attacker) to point at different code with no change visible in this
repository's history. A trailing ``# vX.Y.Z`` comment keeps the pin
human-readable and is the pattern Dependabot understands and can bump on
its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_FILES = sorted(WORKFLOWS_DIR.glob("*.yml"))

_USES_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)@(\S+)(?:\s*#\s*(\S+))?", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _all_uses() -> list[tuple[Path, str, str, str | None]]:
    entries = []
    for path in WORKFLOW_FILES:
        text = path.read_text(encoding="utf-8")
        for match in _USES_RE.finditer(text):
            action, ref, version_comment = match.group(1), match.group(2), match.group(3)
            entries.append((path, action, ref, version_comment))
    return entries


@pytest.fixture(scope="module")
def uses_entries():
    return _all_uses()


def test_every_workflow_file_was_actually_scanned():
    # Guards against a path/glob typo silently skipping every assertion below.
    assert len(WORKFLOW_FILES) >= 4
    assert len(_all_uses()) >= 20


def test_every_action_reference_is_pinned_to_a_full_commit_sha(uses_entries):
    unpinned = [
        f"{path.name}: {action}@{ref}"
        for path, action, ref, _comment in uses_entries
        if not _SHA_RE.match(ref)
    ]
    assert not unpinned, (
        "every third-party action must be pinned to a 40-character commit "
        f"SHA, not a mutable tag/branch: {unpinned}"
    )


def test_every_pinned_action_carries_a_human_readable_version_comment(uses_entries):
    missing_comment = [
        f"{path.name}: {action}@{ref}"
        for path, action, ref, comment in uses_entries
        if _SHA_RE.match(ref) and not comment
    ]
    assert not missing_comment, (
        f"SHA-pinned actions must carry a trailing '# vX.Y.Z' comment so a "
        f"human (and Dependabot) can tell what version the pin represents: {missing_comment}"
    )
