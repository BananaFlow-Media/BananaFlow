from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_documentation", ROOT / "scripts" / "check_documentation.py"
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def test_documentation_gate_static_tree_is_green():
    assert mod.run() == []


def test_markdown_reference_scan_ignores_untracked_local_artifacts(monkeypatch, tmp_path):
    tracked = tmp_path / "tracked.md"
    tracked.write_text("[missing](missing.md)\n", encoding="utf-8")
    untracked = tmp_path / "ignored-local-copy.md"
    untracked.write_text("[also missing](also-missing.md)\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_tracked_markdown_files", lambda: [tracked])

    assert mod.check_markdown_references() == [
        "tracked.md: broken Markdown reference 'missing.md'"
    ]


def test_clickable_markdown_link_does_not_fall_back_to_repository_root(monkeypatch, tmp_path):
    docs = tmp_path / "docs" / "nested"
    docs.mkdir(parents=True)
    source = docs / "source.md"
    source.write_text("[Wrong relative path](ROOT.md)\n", encoding="utf-8")
    (tmp_path / "ROOT.md").write_text("# Root\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_tracked_markdown_files", lambda: [source])

    assert mod.check_markdown_references() == [
        f"{Path('docs/nested/source.md')}: broken Markdown reference 'ROOT.md'"
    ]


def test_plain_backtick_repository_path_accepts_root_notation(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("Review `ROOT.md`.\n", encoding="utf-8")
    (tmp_path / "ROOT.md").write_text("# Root\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_tracked_markdown_files", lambda: [source])

    assert mod.check_markdown_references() == []


def test_markdown_reference_scan_validates_heading_anchors(monkeypatch, tmp_path):
    source = tmp_path / "source.md"
    target = tmp_path / "target.md"
    source.write_text(
        "[Good](target.md#ordinary-heading)\n[Bad](target.md#missing-heading)\n",
        encoding="utf-8",
    )
    target.write_text("# Ordinary heading\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_tracked_markdown_files", lambda: [source, target])

    assert mod.check_markdown_references() == [
        "source.md: missing Markdown anchor 'target.md#missing-heading'"
    ]


def test_docs_navigation_reports_unreachable_tracked_document(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    index = docs / "README.md"
    visible = docs / "visible.md"
    orphan = docs / "orphan.md"
    index.write_text("[Visible](visible.md)\n", encoding="utf-8")
    visible.write_text("# Visible\n", encoding="utf-8")
    orphan.write_text("# Orphan\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(
        mod,
        "_tracked_markdown_files",
        lambda: [index, visible, orphan],
    )

    assert mod.check_docs_navigation() == [
        f"{Path('docs/orphan.md')}: not reachable from docs/README.md"
    ]


def test_stable_release_detection_uses_version_source_of_truth():
    assert mod._is_stable() is True


def test_current_platform_support_wording_has_no_retired_status():
    assert mod.check_platform_support_language() == []


def test_component_overlay_and_channel_workflow_have_strict_documentation_impact():
    rule = next(item for item in mod.IMPACT_RULES if item.name == "updaters/components")
    assert any(re.search(pattern, "core/component_overlay.py") for pattern in rule.path_patterns)
    assert any(
        re.search(pattern, ".github/workflows/component-channel.yml")
        for pattern in rule.path_patterns
    )
    assert rule.allow_no_impact is False
    assert {
        "docs/architecture/secure-component-updater.md",
        "docs/release/RELEASING.md",
        "docs/security/supply-chain.md",
        "SOURCE_OFFER.md",
    } <= set(rule.review_required)


def test_platform_status_check_includes_workflows_and_issue_forms(monkeypatch, tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    forms = tmp_path / ".github" / "ISSUE_TEMPLATE"
    workflows.mkdir(parents=True)
    forms.mkdir(parents=True)
    for relative in mod.CURRENT_PLATFORM_SURFACES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("current\n", encoding="utf-8")
    (workflows / "tests.yml").write_text("# Ubuntu is non-blocking\n", encoding="utf-8")
    (forms / "bug.yml").write_text("- Linux (unsupported/experimental)\n", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", tmp_path)

    errors = "\n".join(mod.check_platform_support_language())
    assert ".github\\workflows\\tests.yml" in errors or ".github/workflows/tests.yml" in errors
    assert ".github\\ISSUE_TEMPLATE\\bug.yml" in errors or ".github/ISSUE_TEMPLATE/bug.yml" in errors


def test_current_support_routes_avoid_disabled_discussions():
    assert mod.check_current_community_routes() == []


def test_provider_version_sources_agree():
    values = mod._provider_versions()
    assert len(values) >= 2
    assert len(set(values.values())) == 1


def test_ytdlp_compatibility_floor_and_reviewed_pin_agree():
    floors = mod._ytdlp_floor_sources()
    assert set(floors) == {
        "pyproject.toml",
        "core/youtube_doctor.py",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
    }
    assert len(set(floors.values())) == 1
    exact = mod._ytdlp_exact_release_pins()
    assert set(exact) == {"requirements.txt", "pyproject.toml dev extra"}
    assert len(set(exact.values())) == 1
    assert mod.check_ytdlp_consistency() == []


def test_ai_adapters_point_to_canonical_context():
    assert mod.check_ai_adapters() == []


def test_executable_impact_map_matches_documented_policy():
    assert mod.check_impact_policy_coverage() == []


def test_impact_policy_rejects_document_not_enforced_by_code(monkeypatch):
    policy = mod._read("docs/DOCUMENTATION_POLICY.md").replace(
        "<!-- impact-rule: CLI --> `cli.py`",
        "<!-- impact-rule: CLI --> `cli.py` `EXTRA_REQUIRED.md`",
    )
    original_read = mod._read
    monkeypatch.setattr(
        mod,
        "_read",
        lambda path: policy if path == "docs/DOCUMENTATION_POLICY.md" else original_read(path),
    )

    errors = "\n".join(mod.check_impact_policy_coverage())
    assert "not enforced EXTRA_REQUIRED.md" in errors


def _set_pr(
    monkeypatch,
    files: list[str],
    body: str = "",
    *,
    include_category: bool = True,
) -> None:
    if "## Documentation impact" not in body:
        if include_category and not re.search(
            r"^\s*-\s*\[[xX]\]\s*(?:" + "|".join(
                re.escape(category) for category in mod.DOCUMENTATION_IMPACT_CATEGORIES
            ) + r")\s*$",
            body,
            re.M,
        ):
            body = "- [x] Architecture / design\n" + body
        body = "## Documentation impact\n" + body
    monkeypatch.setattr(mod, "changed_files", lambda _base, _head: files)
    monkeypatch.setattr(mod, "_pr_body", lambda: body)


def test_no_impact_template_placeholder_is_not_a_bypass(monkeypatch):
    _set_pr(
        monkeypatch,
        ["scripts/internal_check.py"],
        "- [x] No documentation impact\n"
        "No documentation impact reason: <!-- required when checked -->\n",
    )

    errors = mod.check_pr_impact("base", "head")
    assert any("reason is missing, placeholder-only, or too short" in error for error in errors)
    assert any("outside the Code -> Documentation map" in error for error in errors)


def test_meaningful_no_impact_reason_allows_unmapped_internal_change(monkeypatch):
    _set_pr(
        monkeypatch,
        ["scripts/internal_check.py"],
        "- [x] No documentation impact\n"
        "No documentation impact reason: Refactors an internal assertion without changing behavior.\n",
    )

    assert mod.check_pr_impact("base", "head") == []


def test_unrelated_markdown_edit_does_not_satisfy_downloader_reviews(monkeypatch):
    _set_pr(monkeypatch, ["core/downloader.py", "CHANGELOG.md"])

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "downloader/reliability" in errors
    for path in (
        "docs/user-guide/user-manual.md",
        "docs/user-guide/user-guide-he.md",
        "docs/architecture/overview.md",
        "docs/testing/TESTING.md",
    ):
        assert path in errors


def test_previously_unmapped_sensitive_files_cannot_use_unrelated_markdown(monkeypatch):
    cases = {
        "core/hls_downloader.py": "downloader/reliability",
        "utils/security.py": "authentication/privacy",
        "utils/paths.py": "persistence/config",
        "core/operation_manifest.py": "Tag Editor safety",
        "core/providers/musicbrainz_provider.py": "external metadata services",
    }
    for path, expected_rule in cases.items():
        _set_pr(monkeypatch, [path, "CHANGELOG.md"])
        errors = "\n".join(mod.check_pr_impact("base", "head"))
        assert expected_rule in errors, path


def test_new_executable_file_must_join_map_or_declare_no_impact(monkeypatch):
    _set_pr(monkeypatch, ["core/new_engine.py", "CHANGELOG.md"])
    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "outside the Code -> Documentation map: core/new_engine.py" in errors


def test_sensitive_rule_rejects_even_meaningful_global_bypass(monkeypatch):
    _set_pr(
        monkeypatch,
        ["core/browser_session.py"],
        "- [x] No documentation impact\n"
        "No documentation impact reason: Pure internal refactor with identical behavior.\n",
    )

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "authentication/privacy" in errors
    assert "cannot use the global" in errors
    assert "SECURITY.md" in errors


def test_required_all_document_must_change(monkeypatch):
    _set_pr(
        monkeypatch,
        ["version.py"],
        "Relevant documentation updated/reviewed:\n"
        "- SECURITY.md\n- docs/release/RELEASING.md\n",
    )

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "version/release channel" in errors
    assert "must update all required documents: CHANGELOG.md" in errors


def test_required_update_and_explicit_reviews_pass(monkeypatch):
    _set_pr(
        monkeypatch,
        ["version.py", "CHANGELOG.md"],
        "Relevant documentation updated/reviewed:\n"
        "- SECURITY.md\n- docs/release/RELEASING.md\n",
    )

    assert mod.check_pr_impact("base", "head") == []


def test_review_paths_count_only_as_positive_bullets_in_review_section(monkeypatch):
    body = (
        "SECURITY.md and docs/release/RELEASING.md appear elsewhere.\n"
        "Relevant documentation updated/reviewed:\n"
        "- I did not review SECURITY.md\n"
        "- Never reviewed docs/release/RELEASING.md\n"
        "## How was this tested?\n"
        "- SECURITY.md\n- docs/release/RELEASING.md\n"
    )
    _set_pr(monkeypatch, ["version.py", "CHANGELOG.md"], body)

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "requires explicit review" in errors
    assert "SECURITY.md" in errors
    assert "docs/release/RELEASING.md" in errors


def test_review_paths_require_bullets(monkeypatch):
    _set_pr(
        monkeypatch,
        ["version.py", "CHANGELOG.md"],
        "Relevant documentation updated/reviewed:\n"
        "SECURITY.md\ndocs/release/RELEASING.md\n",
    )

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "requires explicit review" in errors


def test_review_bullets_outside_documentation_impact_section_do_not_count(monkeypatch):
    body = (
        "## Documentation impact\n"
        "- [x] Architecture / design\n"
        "Relevant documentation updated/reviewed:\n\n"
        "## Notes\n"
        "Relevant documentation updated/reviewed:\n"
        "- SECURITY.md\n- docs/release/RELEASING.md\n"
    )
    _set_pr(monkeypatch, ["version.py", "CHANGELOG.md"], body)

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "requires explicit review" in errors


def test_pr_requires_documentation_impact_category(monkeypatch):
    _set_pr(
        monkeypatch,
        ["version.py", "CHANGELOG.md"],
        "Relevant documentation updated/reviewed:\n"
        "- SECURITY.md\n- docs/release/RELEASING.md\n",
        include_category=False,
    )

    errors = mod.check_pr_impact("base", "head")
    assert errors == ["select at least one Documentation impact category in the PR body"]


def test_checkbox_outside_documentation_impact_section_does_not_count(monkeypatch):
    _set_pr(
        monkeypatch,
        ["version.py", "CHANGELOG.md"],
        "## Notes\n- [x] Architecture / design\n"
        "Relevant documentation updated/reviewed:\n"
        "- SECURITY.md\n- docs/release/RELEASING.md\n",
        include_category=False,
    )

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "select at least one Documentation impact category" in errors


def test_root_entry_points_are_impact_candidates(monkeypatch):
    for path, expected in (("main.py", "visible UI"), ("error_handler.py", "downloader/reliability")):
        _set_pr(monkeypatch, [path, "CHANGELOG.md"])
        errors = "\n".join(mod.check_pr_impact("base", "head"))
        assert expected in errors


def test_no_impact_category_is_mutually_exclusive(monkeypatch):
    _set_pr(
        monkeypatch,
        ["scripts/internal_check.py"],
        "- [x] Architecture / design\n"
        "- [x] No documentation impact\n"
        "No documentation impact reason: Internal assertion only; no behavior changes.\n",
    )

    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "cannot be selected with another impact category" in errors


def test_required_any_document_must_actually_change(monkeypatch):
    body = (
        "Relevant documentation updated/reviewed:\n"
        "- docs/release/RELEASING.md\n- docs/security/supply-chain.md\n"
        "- THIRD_PARTY_NOTICES.md\n- SOURCE_OFFER.md\n"
    )
    _set_pr(monkeypatch, ["pyproject.toml", "CHANGELOG.md"], body)
    errors = "\n".join(mod.check_pr_impact("base", "head"))
    assert "must update at least one of" in errors

    _set_pr(monkeypatch, ["pyproject.toml", "docs/release/RELEASING.md"], body)
    assert mod.check_pr_impact("base", "head") == []
