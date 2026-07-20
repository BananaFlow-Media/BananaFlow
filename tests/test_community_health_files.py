"""Community health files: GitHub Discussions links must not rot.

`.github/ISSUE_TEMPLATE/config.yml` once pointed its "Usage question"
contact link at a Discussions category that did not exist on GitHub, on
the page every new contributor sees first. These tests keep every
in-repo Discussions link tied to the planned category set declared
below, so renaming or deleting a category surfaces as a failing test
instead of a dead link.

Scope note, deliberately: this is a *consistency* check between the
repo's links and the repo's own declared slug set. It cannot prove a
category still exists on GitHub -- the test suite does not reach the
network. The live check is `gh api graphql` against
`repository.discussionCategories`, and creating the custom categories on
a fresh repository is a manual maintainer step (the GitHub API has no
mutation for it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The planned Discussions category slugs for this repository. GitHub's
#: defaults plus four custom ones the maintainer creates by hand.
PLANNED_SLUGS = frozenset({
    "announcements",
    "help",
    "ideas",
    "show-and-tell",
    "translations",
    "beta-testing",
    "development",
})

#: Files that carry user-facing Discussions links.
LINK_SOURCES = (
    "SUPPORT.md",
    "CONTRIBUTING.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
)

#: GitHub's own defaults, kept in place alongside the planned set.
_GITHUB_DEFAULT_SLUGS = frozenset({"general", "polls", "q-a"})

_LINK_RE = re.compile(r"discussions/categories/([a-z0-9-]+)")


def _documented_slugs() -> set[str]:
    """The declared category set (kept as a function so the assertions
    below read the same as when the table lived in a standalone doc)."""
    return set(PLANNED_SLUGS)


def test_the_proposal_documents_every_planned_category():
    """All seven planned categories must be declared with a real slug,
    not just a display name."""
    documented = _documented_slugs()
    for slug in (
        "announcements",
        "help",
        "ideas",
        "show-and-tell",
        "translations",
        "beta-testing",
        "development",
    ):
        assert slug in documented, (
            f"category slug {slug!r} is missing from PLANNED_SLUGS"
        )


@pytest.mark.parametrize("relative_path", LINK_SOURCES)
def test_discussions_links_point_at_documented_categories(relative_path: str):
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing"

    known = _documented_slugs() | _GITHUB_DEFAULT_SLUGS
    for slug in _LINK_RE.findall(path.read_text(encoding="utf-8")):
        assert slug in known, (
            f"{relative_path} links to Discussions category {slug!r}, which is "
            f"not in PLANNED_SLUGS. Either the category was renamed/deleted "
            f"on GitHub, or the link is a typo — this is exactly the "
            f"dead-link failure that shipped once already."
        )


def test_support_and_contributing_route_each_new_category_somewhere():
    """The four categories created by hand are only worth having if
    something actually sends people to them."""
    combined = "\n".join(
        (REPO_ROOT / name).read_text(encoding="utf-8")
        for name in ("SUPPORT.md", "CONTRIBUTING.md", ".github/ISSUE_TEMPLATE/config.yml")
    )
    for slug in ("help", "translations", "beta-testing", "development"):
        assert f"discussions/categories/{slug}" in combined, (
            f"nothing routes users to the {slug!r} Discussions category; it "
            f"was created for a reason, so link it or drop it"
        )


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
