"""Release policy for yt-dlp: reviewed nightly + compatible bundled extras."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
PIN = "2026.8.4.234419.dev0"


def test_pyproject_pins_reviewed_ytdlp_nightly():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    assert f"yt-dlp[default]=={PIN}" in dependencies
    assert not any(re.match(r"^yt-dlp-ejs(?:[<>=!~]|$)", dep) for dep in dependencies)


def test_requirements_matches_pyproject_ytdlp_pin():
    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert f"yt-dlp[default]=={PIN}" in requirements
    active_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(re.match(r"^yt-dlp-ejs(?:[<>=!~]|$)", line) for line in active_lines)
