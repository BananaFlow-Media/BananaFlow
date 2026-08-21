"""Static gates on the Windows release workflow's publication policy.

Two properties are worth failing a build over, because getting either wrong is
irreversible in public:

1. A tag build must produce a **draft**, never a published or "latest" release.
   A published release is visible, mirrored and linked immediately; unpublishing
   it does not unring that bell. Automated gates being green is a necessary
   condition for a release, not a sufficient one — the manual checks in
   docs/release/RELEASING.md still have to be performed by a
   human against the exact artifacts.
2. The gates that make the artifacts trustworthy must actually run before
   publication.

These are text assertions over the workflow, which is the only way to test a
workflow without running it. They are coarse on purpose: they check that the
policy is present, not how it is phrased.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
MACOS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-macos.yml"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parsed() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# Publication policy
# ──────────────────────────────────────────────────────────────────────────────


def test_release_is_created_as_a_draft(workflow: str):
    assert "draft: true" in workflow, (
        "a tag build must create a DRAFT release; publishing is a human decision"
    )


def test_release_is_not_marked_latest(workflow: str):
    assert "make_latest: 'false'" in workflow, (
        "an unaccepted release must not become the latest download"
    )


def test_release_carries_the_manual_acceptance_notice(workflow: str):
    assert "Automated release gates passed; manual release acceptance remains pending." in workflow


def test_only_a_tag_push_can_publish(workflow: str):
    """A dry run must be repeatable without ever creating a release."""
    assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in workflow, (
        "the release step must be gated on a tag push, so workflow_dispatch stays a dry run"
    )


def test_release_notes_are_generated(workflow: str):
    assert "generate_release_notes: true" in workflow


def test_sbom_is_attached_to_the_public_release(workflow: str):
    """The SBOM is generated on every build but was only uploaded to the
    internal CI artifact bundle, not the public GitHub Release -- verifying
    the real v0.2.1-beta.2 draft found it missing as a downloadable asset.
    Owner decision: attach it for public supply-chain transparency."""
    assert "dist/sbom.cyclonedx.json" in workflow
    assert "dist/SBOM_INVENTORY.md" in workflow


def test_macos_workflow_avoids_bash_4_only_syntax():
    """macOS runners execute `run:` steps via the system /bin/bash, which
    Apple has frozen at 3.2 (the last GPLv2 release) -- it does not support
    associative arrays (`declare -A`), `mapfile`/`readarray`, or `local -n`
    namerefs. A real tag push (Phase 12) found `declare -A` had been broken
    since it was introduced, because no prior CI run had ever exercised this
    macOS-only release workflow with a real tag."""
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")

    for construct in ("declare -A", "mapfile ", "readarray ", "local -n "):
        assert construct not in text, (
            f"{construct!r} is bash 4+ only and will fail on macOS's bash 3.2"
        )


def test_macos_release_cannot_publish_the_shared_release():
    """The macOS workflow fires on the same tag and targets the same release,
    so it must be as unable to publish as the Windows one."""
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")

    assert "draft: true" in text, "the macOS release step must also be a draft"
    assert "make_latest: 'false'" in text

    condition = pytest.importorskip("yaml").safe_load(text)["jobs"]["publish-draft"]["if"]
    assert "github.event_name == 'push'" in condition
    assert "startsWith(github.ref, 'refs/tags/v')" in condition, (
        "the macOS release step must be gated on a tag push, like the Windows one"
    )


def test_macos_dmg_is_attached_to_supported_release_tags():
    """macOS Apple Silicon is supported for stable and pre-release builds."""
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")
    condition = pytest.importorskip("yaml").safe_load(text)["jobs"]["publish-draft"]["if"]

    assert "contains(github.ref_name, '-')" not in condition, (
        "supported macOS artifacts must not be excluded from pre-release tags"
    )

    # The DMG must still reach testers as a CI artifact, not vanish entirely.
    assert "Upload macOS artifacts" in text
    assert "dist/BananaFlow-*-macos-arm64.dmg" in text.split("Upload macOS artifacts", 1)[1]


def test_prerelease_flag_is_derived_from_the_tag_not_hardcoded(workflow: str):
    """RELEASE_STRATEGY.md requires a SemVer pre-release tag (e.g.
    v0.2.1-beta.1) to be marked as a GitHub Pre-release, and a clean vX.Y.Z
    tag to be Stable (not a pre-release). A hardcoded `prerelease: false`
    would violate this for every Beta tag -- Phase 12 found and fixed exactly
    this bug before it shipped."""
    assert "prerelease: false" not in workflow, (
        "prerelease must not be hardcoded false -- it must depend on whether "
        "the tag carries a pre-release suffix"
    )
    assert "prerelease: ${{ steps.prerelease.outputs.value }}" in workflow
    assert 'if [[ "$tag" == *-* ]]' in workflow, (
        "the workflow must detect a pre-release tag by checking for a "
        "SemVer pre-release suffix (a hyphen after the version numbers)"
    )


def test_body_and_generate_release_notes_use_append_body(workflow: str):
    """release-windows.yml sets a custom `body:` (the safety disclaimer)
    AND `generate_release_notes: true`. Without `append_body: true`, a real
    tag push (Phase 12) found the custom body silently discarded in favor
    of only the auto-generated notes -- and separately, since
    release-macos.yml's job updates the same shared release, one job's
    update can wipe out the other's body unless both append."""
    assert "generate_release_notes: true" in workflow
    assert "append_body: true" in workflow


def test_macos_release_also_appends_body():
    """release-macos.yml has no custom body of its own, but its job updates
    the same shared release as release-windows.yml -- it must append too, or
    it can wipe out whatever body the Windows job already wrote."""
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")
    assert "append_body: true" in text


def test_macos_prerelease_flag_matches_windows_logic():
    """Both workflows' publish-draft jobs target the same shared GitHub
    Release for a tag, so they must agree on the prerelease flag they each
    assert -- a mismatch would have one job flip the flag the other just set."""
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")

    assert "prerelease: false" not in text
    assert "prerelease: ${{ steps.prerelease.outputs.value }}" in text
    assert 'if [[ "$tag" == *-* ]]' in text


def test_workflow_builds_component_pin_rc_on_main_but_only_tags_can_publish(parsed: dict):
    """Reviewed pin changes build RCs; the publication job stays tag-only."""
    # PyYAML parses the `on:` key as the boolean True.
    triggers = parsed.get("on", parsed.get(True))

    assert set(triggers) == {"workflow_dispatch", "push"}
    push = triggers["push"]
    assert set(push) == {"branches", "paths", "tags"}
    assert push["branches"] == ["main"]
    assert push["paths"] == [
        "requirements.txt", "constraints-release.txt", "pyproject.toml",
    ]
    assert push["tags"] == [
        "v[0-9]+.[0-9]+.[0-9]+",
        "v[0-9]+.[0-9]+.[0-9]+-*",
    ], (
        "the second pattern was added in Phase 12 so a SemVer pre-release "
        "tag (e.g. v0.2.1-beta.1, per RELEASE_STRATEGY.md's owner-approved "
        "version policy) actually triggers the release workflow — the "
        "original clean-version-only pattern would silently never match it"
    )

    macos = pytest.importorskip("yaml").safe_load(
        MACOS_WORKFLOW.read_text(encoding="utf-8")
    )
    mac_push = macos.get("on", macos.get(True))["push"]
    assert mac_push["branches"] == ["main"]
    assert mac_push["paths"] == push["paths"]


def test_permissions_are_least_required(parsed: dict):
    assert parsed["permissions"] == {"contents": "read"}, (
        "the workflow default must be read-only"
    )
    # The build job scopes in id-token/attestations only for the build
    # provenance attestation step (Phase 4); it must still hold no broader
    # write access — repository contents stay read-only here.
    assert parsed["jobs"]["build"]["permissions"] == {
        "contents": "read", "id-token": "write", "attestations": "write",
    }, "the build job must not gain repository-contents write access"
    publish = parsed["jobs"]["publish-draft"]
    assert publish["permissions"] == {"contents": "write"}, (
        "only the draft-publication job may write repository contents"
    )
    assert publish["needs"] == "build"

    macos = pytest.importorskip("yaml").safe_load(
        MACOS_WORKFLOW.read_text(encoding="utf-8")
    )
    assert macos["permissions"] == {"contents": "read"}
    assert macos["jobs"]["build"]["permissions"] == {
        "contents": "read", "id-token": "write", "attestations": "write",
    }, "the macOS build job must not gain repository-contents write access"
    assert macos["jobs"]["publish-draft"]["permissions"] == {"contents": "write"}
    assert macos["jobs"]["publish-draft"]["needs"] == "build"


# ──────────────────────────────────────────────────────────────────────────────
# Gates that must run before anything is published
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "needle, why",
    [
        ("run_isolated_tests.py", "the isolated suite must gate the release"),
        ("does not match version.FULL_VERSION", "tag/version consistency must be enforced"),
        ("build_windows.ps1", "the build must go through the documented script"),
        ("--version", "the packaged CLI must report its version"),
        ("--doctor", "the packaged CLI doctor must pass"),
        ("PO Token Provider ready", "the bundled provider must be verified ready"),
        ("--internal-smoke-test", "the packaged Tag Editor smoke must run"),
        ("ISCC.exe", "the installer must compile"),
        ("SHA256SUMS.txt does not cover", "checksums must cover every uploaded file"),
    ],
)
def test_required_gate_is_present(workflow: str, needle: str, why: str):
    assert needle in workflow, why


def test_the_smoke_runs_against_the_packaged_exe(workflow: str):
    """Running main.py would prove nothing about what users install."""
    assert 'Start-Process -FilePath "dist\\bananaflow\\bananaflow.exe"' in workflow


def test_the_smoke_classifies_native_exits_instead_of_trusting_or_ignoring_them(workflow: str):
    """bananaflow.exe exits 0xC0000409 in Qt teardown *after* reporting success.

    The result file is the evidence, not the exit code — but only the one
    reviewed code is tolerated, and only alongside a complete passing result.
    An unreviewed native code must still fail the release.
    """
    body = workflow.split("Packaged Tag Editor smoke (the real EXE)", 1)[1].split("- name:", 1)[0]

    assert "wrote no result file" in body, "a smoke that never reported must fail"
    assert "reported ok=false" in body
    assert "-1073740791" in body, "the reviewed teardown code must be named explicitly"
    assert "has no reviewed classification" in body, (
        "any other native code must fail rather than be waved through"
    )


def test_evidence_is_uploaded_even_when_a_gate_fails(workflow: str):
    body = workflow.split("Upload isolated test evidence", 1)[1]

    assert "if: always()" in body.split("uses:", 1)[0], (
        "test evidence must survive a failed gate — that is when it is needed"
    )


def test_the_build_does_not_run_the_suite_twice(workflow: str):
    assert "-SkipTests" in workflow, (
        "the build script must skip its own gate because the workflow already ran it"
    )
    assert "python -m pytest tests/" not in workflow, (
        "the single-process suite cannot complete on Windows (F-16)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Both release workflows must gate on the truthful isolated runner
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["release-windows.yml", "release-macos.yml"])
def test_release_workflow_uses_the_isolated_runner_not_single_process(path):
    """macOS is an advertised release target (README), so its gate must be the
    truthful per-file runner too — the single-process suite segfaults on macOS."""
    text = (REPO_ROOT / ".github" / "workflows" / path).read_text(encoding="utf-8")

    assert "scripts/run_isolated_tests.py" in text or "scripts\\run_isolated_tests.py" in text, (
        f"{path} must gate on the isolated runner"
    )
    assert "python -m pytest tests/" not in text, (
        f"{path} must not gate on the single-process suite"
    )
    assert "pytest tests/ -q" not in text, (
        f"{path} must not gate on the single-process suite"
    )


@pytest.mark.parametrize("path", ["release-windows.yml", "release-macos.yml"])
def test_release_workflow_enforces_tag_version_consistency(path):
    text = (REPO_ROOT / ".github" / "workflows" / path).read_text(encoding="utf-8")

    assert "does not match version.__version__" in text or "not ${{ steps.ver.outputs.version }}" in text or "does not match version" in text, (
        f"{path} must fail a tag whose version disagrees with version.__version__"
    )


@pytest.mark.parametrize("path", ["release-windows.yml", "release-macos.yml"])
def test_release_workflow_uploads_isolated_evidence_always(path):
    text = (REPO_ROOT / ".github" / "workflows" / path).read_text(encoding="utf-8")

    block = text.split("Upload isolated test evidence", 1)
    assert len(block) == 2, f"{path} must upload isolated test evidence"
    assert "if: always()" in block[1].split("uses:", 1)[0]


def test_macos_release_is_draft_only():
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")
    assert "draft: true" in text and "make_latest: 'false'" in text


def test_macos_does_not_claim_notarization_or_developer_id():
    """Ad-hoc signing only — the report and workflow must not overclaim."""
    text = MACOS_WORKFLOW.read_text(encoding="utf-8")
    assert "codesign --force --sign - dist/BananaFlow.app/Contents/MacOS/bananaflow" in text, (
        "signing must be ad-hoc (identity '-')"
    )
    # Any Developer ID / notarization step must stay disabled (`if: false`).
    if "notarize" in text.lower():
        assert "if: false" in text, "notarization must remain disabled"


# ──────────────────────────────────────────────────────────────────────────────
# The PR/push test workflow
# ──────────────────────────────────────────────────────────────────────────────


def test_ci_gate_uses_the_isolated_runner_on_every_platform():
    """Single-process full-suite is unreliable on both platforms, so neither
    leg gates on it."""
    text = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/run_isolated_tests.py" in text
    # The gate step is not restricted to one OS.
    gate = text.split("Run full test suite (isolated", 1)[1].split("- name:", 1)[0]
    assert "runner.os ==" not in gate and "runner.os !=" not in gate, (
        "the isolated gate must run on every matrix leg"
    )
    # `pytest tests/` must not be the CI gate on any platform.
    assert "pytest tests/ -q" not in text, (
        "the single-process full suite cannot complete on Windows and can crash "
        "the session on Linux (F-16); it must not gate CI"
    )


def test_ci_uploads_evidence_on_every_platform_even_on_failure():
    text = TESTS_WORKFLOW.read_text(encoding="utf-8")

    upload = text.split("Upload isolated test evidence", 1)[1].split("- name:", 1)[0]
    assert "if: always()" in upload
    assert "runner.os" not in upload, "evidence must upload on every leg, red or green"


def test_windows_and_ubuntu_are_blocking_supported_platform_gates():
    """Linux source-install support requires Ubuntu failures to block CI."""
    text = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "continue-on-error" not in text
    assert "Linux source installs are both supported" in text


# ──────────────────────────────────────────────────────────────────────────────
# Asset naming contract — regression suite (issue #35)
#
# Every pattern that references a Windows release file MUST include the
# architecture token (x64) and the v-prefix.  A wildcard that omits x64
# (e.g.  BananaFlow-*-windows-setup.exe) would match the old, non-conforming
# names and silently pass even when the build is broken.
# ──────────────────────────────────────────────────────────────────────────────

# ── Expected exact filenames (the website's naming contract) ─────────────────
_SETUP_GLOB   = "BananaFlow-*-windows-x64-setup.exe"
_PORTABLE_GLOB = "BananaFlow-*-windows-x64-portable.zip"
_DMG_GLOB     = "BananaFlow-*-macos-arm64.dmg"

# Patterns that used to appear in the old (non-conforming) pipeline.
# Any of these surviving in an actionable position is a regression.
_OLD_SETUP_GLOB    = "BananaFlow-*-windows-setup.exe"
_OLD_PORTABLE_GLOB = "BananaFlow-*-windows-portable.zip"
_OLD_LC_SETUP      = "bananaflow-*-windows-setup"
_OLD_LC_PORTABLE   = "bananaflow-*-windows-portable"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _assert_no_old_windows_globs(text: str, source: str) -> None:
    """Fail if any old (x64-less) Windows glob still appears in *text*."""
    for bad in (_OLD_SETUP_GLOB, _OLD_PORTABLE_GLOB,
                _OLD_LC_SETUP, _OLD_LC_PORTABLE):
        assert bad not in text, (
            f"{source}: old non-conforming pattern {bad!r} must be removed; "
            f"use the x64-including form instead"
        )


# ── release-windows.yml ──────────────────────────────────────────────────────

def test_windows_workflow_uses_x64_glob_for_installer_discovery(workflow: str):
    """The 'Build Windows installer' step discovers the .exe by x64 glob."""
    assert _SETUP_GLOB in workflow, (
        f"release-windows.yml must discover the installer with {_SETUP_GLOB!r}"
    )


def test_windows_workflow_uses_x64_glob_for_productversion_check(workflow: str):
    """The ProductVersion verification step filters with x64 glob."""
    assert "*-windows-x64-setup.exe" in workflow, (
        "the ProductVersion check must filter the installer with the x64 pattern"
    )


def test_windows_workflow_uses_x64_globs_for_attestation(workflow: str):
    """Build-provenance subject-path must reference x64 names."""
    attest_block = workflow.split("Generate build provenance attestation", 1)[1].split("- name:", 1)[0]
    assert _PORTABLE_GLOB in attest_block, (
        f"attestation subject-path must contain {_PORTABLE_GLOB!r}"
    )
    assert _SETUP_GLOB in attest_block, (
        f"attestation subject-path must contain {_SETUP_GLOB!r}"
    )


def test_windows_workflow_uses_x64_globs_for_artifact_upload(workflow: str):
    """The 'Upload release artifacts' step must reference x64 names."""
    upload_block = workflow.split("Upload release artifacts", 1)[1].split("- name:", 1)[0]
    assert _PORTABLE_GLOB in upload_block, (
        f"artifact upload path must contain {_PORTABLE_GLOB!r}"
    )
    assert _SETUP_GLOB in upload_block, (
        f"artifact upload path must contain {_SETUP_GLOB!r}"
    )


def test_windows_workflow_uses_x64_globs_for_github_release(workflow: str):
    """The 'Create DRAFT GitHub Release' files: list must reference x64 names."""
    release_block = workflow.split("Create DRAFT GitHub Release", 1)[1]
    assert _PORTABLE_GLOB in release_block, (
        f"GitHub Release files list must contain {_PORTABLE_GLOB!r}"
    )
    assert _SETUP_GLOB in release_block, (
        f"GitHub Release files list must contain {_SETUP_GLOB!r}"
    )


def test_windows_workflow_has_no_old_x64_less_globs(workflow: str):
    """No old (x64-less) Windows glob must survive in the workflow."""
    _assert_no_old_windows_globs(workflow, "release-windows.yml")


# ── build_windows.ps1 ────────────────────────────────────────────────────────

def test_build_script_zip_name_includes_x64_and_v_prefix():
    """The portable ZIP name produced by the build script must match the contract."""
    text = _read("scripts/build_windows.ps1")
    assert "BananaFlow-v$AppVersion-windows-x64-portable.zip" in text, (
        "build_windows.ps1 must produce BananaFlow-v{ver}-windows-x64-portable.zip"
    )
    _assert_no_old_windows_globs(text, "build_windows.ps1")


def test_build_script_checksum_filter_uses_bananaflow_caps():
    """SHA256SUMS.txt is generated by filtering BananaFlow-* (PascalCase)."""
    text = _read("scripts/build_windows.ps1")
    assert "-Filter 'BananaFlow-*'" in text, (
        "build_windows.ps1 checksum glob must be PascalCase BananaFlow-*"
    )


# ── bananaflow.iss (Inno Setup) ───────────────────────────────────────────────

def test_inno_setup_output_filename_includes_x64_and_v_prefix():
    """The installer output filename must carry the x64 token and v-prefix."""
    text = _read("packaging/bananaflow.iss")
    assert "BananaFlow-v{#AppVersion}-windows-x64-setup" in text, (
        "bananaflow.iss OutputBaseFilename must be BananaFlow-v{AppVersion}-windows-x64-setup"
    )
    _assert_no_old_windows_globs(text, "bananaflow.iss")


# ── run_local_av_scan.ps1 ────────────────────────────────────────────────────

def test_av_scan_script_uses_x64_globs():
    """The local AV scan script must discover files using x64 globs."""
    text = _read("scripts/run_local_av_scan.ps1")
    assert _SETUP_GLOB in text, (
        f"run_local_av_scan.ps1 installer filter must be {_SETUP_GLOB!r}"
    )
    assert _PORTABLE_GLOB in text, (
        f"run_local_av_scan.ps1 portable filter must be {_PORTABLE_GLOB!r}"
    )
    _assert_no_old_windows_globs(text, "run_local_av_scan.ps1")


# ── Cross-source consistency ─────────────────────────────────────────────────

@pytest.mark.parametrize("source,rel", [
    ("release-windows.yml", ".github/workflows/release-windows.yml"),
    ("build_windows.ps1",   "scripts/build_windows.ps1"),
    ("bananaflow.iss",      "packaging/bananaflow.iss"),
    ("run_local_av_scan.ps1", "scripts/run_local_av_scan.ps1"),
])
def test_no_old_naming_in_windows_release_files(source: str, rel: str):
    """No actionable file may reference the pre-#35 naming patterns."""
    _assert_no_old_windows_globs(_read(rel), source)


def test_sha256sums_naming_consistency():
    """SHA256SUMS.txt entries must be byte-for-byte identical to the filenames.

    The checksum filter in build_windows.ps1 directly uses the filenames of
    files found by Get-ChildItem, so if the build script produces the correct
    name the checksum will match.  This test verifies the filter picks up the
    same PascalCase prefix that the ZIP and installer use.
    """
    ps1 = _read("scripts/build_windows.ps1")
    iss = _read("packaging/bananaflow.iss")

    # Both the ZIP name and the checksum filter must be PascalCase BananaFlow-
    assert "BananaFlow-v$AppVersion-windows-x64-portable.zip" in ps1
    assert "-Filter 'BananaFlow-*'" in ps1

    # The installer name must also be PascalCase+x64 so its checksum entry
    # in SHA256SUMS.txt matches the uploaded asset name exactly.
    assert "BananaFlow-v{#AppVersion}-windows-x64-setup" in iss
