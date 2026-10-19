#!/usr/bin/env python3
"""Report upstream candidates without confusing review work with CI failure.

This read-only check never updates the reviewed pin or publishes components.
Transport, malformed metadata and invalid/missing reviewed pins still fail CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.request

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"


def compare(requirements: str, data: dict) -> tuple[str, str]:
    pins = re.findall(r"^yt-dlp\[default\]==([^\s#]+)", requirements, re.MULTILINE)
    if len(pins) != 1:
        raise ValueError("requirements.txt must contain one exact yt-dlp[default] pin")
    reviewed = pins[0]
    Version(reviewed)
    releases = data["releases"]
    if not isinstance(releases, dict):
        raise ValueError("PyPI releases must be an object")
    candidates = []
    for version, files in releases.items():
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise ValueError("Malformed PyPI release files")
        if any(not isinstance(item.get("yanked"), bool) for item in files):
            raise ValueError("PyPI release files must declare their yanked status")
        if files and any(not item["yanked"] for item in files):
            # Validate even versions that would not sort to the front.
            candidates.append((Version(version), str(Version(version))))
    if not candidates:
        raise ValueError("PyPI returned no non-yanked yt-dlp releases")
    if not any(version == Version(reviewed) for version, _ in candidates):
        raise ValueError("The reviewed yt-dlp pin is missing or fully yanked on PyPI")
    return reviewed, max(candidates)[1]


def main() -> int:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    with urllib.request.urlopen(PYPI_URL, timeout=20) as response:
        data = json.load(response)
    reviewed, latest = compare(requirements, data)
    newer = Version(latest) > Version(reviewed)
    status = "Human review required" if newer else "Reviewed pin is current"
    summary = (
        f"## Downloader freshness\n\nReviewed: `{reviewed}`  \nNewest: `{latest}`\n\n"
        f"**{status}.** No dependency was changed or published by this check.\n"
    )
    if newer:
        summary += (
            "\nReview upstream changes and compatibility, update requirements.txt and "
            "the pyproject.toml dev pin together, then run the dependency, downloader "
            "and real-network gates before merging.\n"
        )
        print(f"::warning title=yt-dlp candidate requires human review::"
              f"New candidate {latest}; reviewed pin remains {reviewed}. No automatic update.")
    else:
        print(f"Reviewed yt-dlp pin {reviewed} is current.")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as output:
            output.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
