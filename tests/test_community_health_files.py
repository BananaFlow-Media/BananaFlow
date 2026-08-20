"""Community-health routes must match the repository features in use.

GitHub Discussions are currently disabled for this repository. Support
and contribution documentation therefore routes ordinary questions to
the official website and structured project feedback to Issue forms.
These deterministic checks prevent inactive Discussions links from
returning; enabling Discussions later requires an intentional update to
the routes, documentation gate and this contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Files that carry current user-facing support/community routes.
ROUTE_SOURCES = (
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "docs/user-guide/user-manual.md",
    "docs/user-guide/user-guide-he.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
)

@pytest.mark.parametrize("relative_path", ROUTE_SOURCES)
def test_current_routes_do_not_link_disabled_discussions(relative_path: str):
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing"
    text = path.read_text(encoding="utf-8").casefold()
    assert "/discussions" not in text
    assert "issues and discussions" not in text


def test_new_issue_page_routes_to_available_support_and_forms():
    config = (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    assert "bananaflow.bananaflow-media.workers.dev/en/support/" in config
    assert "issues/new?template=feature_request.yml" in config
    assert "issues/new?template=hebrew_translation.yml" in config


def test_blank_issues_stay_disabled():
    """The issue forms exist so reports carry Doctor output, versions and
    repro steps; a blank issue bypasses all of that."""
    config = (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    assert "blank_issues_enabled: false" in config


def test_security_reports_are_routed_away_from_public_issues():
    """SECURITY.md makes private vulnerability reporting the official
    channel; the new-Issue page must say so rather than let someone paste
    an exploit into a public Issue."""
    config = (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")
    assert "security/advisories/new" in config
    assert "SECURITY.md" in config


def test_conduct_reports_have_a_practical_non_security_contact_route():
    conduct = (REPO_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    assert "bananaflow.media@gmail.com" in conduct
    assert "Code of Conduct report" in conduct
    assert "does not present\nsecurity vulnerability reporting as a conduct-reporting channel" in conduct
