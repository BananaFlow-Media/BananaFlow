"""yt-dlp dependency policy: safe source floor + reviewed CI/release pin."""

from __future__ import annotations

from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
FLOOR = "2026.7.4"
REVIEWED_PIN = "2026.8.20.234504.dev0"


def _active_requirements() -> list[str]:
    return [
        line.strip()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_pyproject_uses_safe_upgradeable_source_floor():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    assert f"yt-dlp[default]>={FLOOR}" in dependencies
    assert not any(re.match(r"^yt-dlp-ejs(?:[<>=!~]|$)", dep) for dep in dependencies)


def test_ci_dev_extra_pins_the_reviewed_nightly():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert f"yt-dlp[default]=={REVIEWED_PIN}" in data["project"]["optional-dependencies"]["dev"]


def test_requirements_pins_the_same_reviewed_nightly_for_release_builds():
    active = _active_requirements()
    assert f"yt-dlp[default]=={REVIEWED_PIN}" in active
    assert not any(re.match(r"^yt-dlp-ejs(?:[<>=!~]|$)", line) for line in active)
