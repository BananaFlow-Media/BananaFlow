#!/usr/bin/env python3
"""BananaFlow documentation consistency gate.

Runs with the standard library only. Static mode checks the checked-out tree.
PR mode (``--base``/``--head``) additionally checks Code → Documentation
impact expectations using the pull-request body from ``GITHUB_EVENT_PATH``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DOCS = (
    "AGENTS.md",
    "docs/README.md",
    "docs/AI_CONTEXT.md",
    "docs/DOCUMENTATION_POLICY.md",
    "docs/architecture/overview.md",
    "docs/testing/TESTING.md",
    "docs/security/threat-model.md",
    "docs/security/supply-chain.md",
    "docs/accessibility/ACCESSIBILITY.md",
    "docs/i18n/TRANSLATING.md",
    "docs/user-guide/user-manual.md",
    "docs/user-guide/user-guide-he.md",
    "docs/user-guide/cli.md",
    "SECURITY.md",
    "PRIVACY.md",
    "CHANGELOG.md",
)

CURRENT_DOCS = (
    "README.md",
    "SECURITY.md",
    "PRIVACY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "docs/README.md",
    "docs/AI_CONTEXT.md",
    "docs/DOCUMENTATION_POLICY.md",
    "docs/user-guide/user-manual.md",
    "docs/user-guide/user-guide-he.md",
    "docs/release/RELEASING.md",
)

COMMUNITY_ROUTE_FILES = (
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "docs/user-guide/user-manual.md",
    "docs/user-guide/user-guide-he.md",
)

STABLE_FORBIDDEN_PHRASES = (
    "no stable release yet",
    "first stable release will be",
    "current beta series",
    "road from the current beta",
    "v0.1.0 is the latest public release",
    "no public project website is currently operated",
    "a project website, winget",
)

PLATFORM_FORBIDDEN_PHRASES = (
    "macos packaged support is experimental",
    "macos is experimental",
    "experimental packaged support",
    "source/developer use unless a release explicitly says otherwise",
    "source install only, unsupported",
    "source-install-only, unsupported",
    "linux remains source/developer-oriented",
    "linux remains source-install-only",
    "linux (unsupported/experimental)",
    "ubuntu is non-blocking",
    "ubuntu leg is non-blocking",
    "windows the only official beta target",
    'macos "experimental; not a supported public beta target',
)

CURRENT_PLATFORM_SURFACES = (
    *CURRENT_DOCS,
    ".github/workflows/tests.yml",
    ".github/workflows/release-macos.yml",
)

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
# Plain code-formatted repository paths are resolved from the repository root.
# Exclude code-formatted link labels: their actual destinations are checked by
# ``MARKDOWN_LINK_RE`` relative to the source file.
BACKTICK_MD_RE = re.compile(r"`([^`\n]+\.md(?:#[^`\n]+)?)`(?!\]\()")

DOCUMENTATION_IMPACT_CATEGORIES = (
    "User behavior / user guide",
    "CLI",
    "Architecture / design",
    "Configuration / persistence / migration",
    "Security / privacy / trust boundary",
    "Packaging / release / dependencies / licenses",
    "Accessibility / RTL / translation",
    "Historical/QA evidence only",
    "No documentation impact",
)


@dataclass(frozen=True)
class ImpactRule:
    name: str
    path_patterns: tuple[str, ...]
    required_all: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    review_required: tuple[str, ...] = ()
    allow_no_impact: bool = True


IMPACT_RULES = (
    ImpactRule(
        "downloader/reliability",
        (
            r"^error_handler\.py$",
            r"^core/downloader\.py$", r"^core/download_orchestrator\.py$",
            r"^core/retry_policy\.py$", r"^core/youtube_reliability\.py$",
            r"^core/(?:hls_downloader|universal_extractor)\.py$",
            r"^utils/yt_dlp_opts\.py$",
        ),
        review_required=(
            "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md",
            "docs/architecture/overview.md", "docs/testing/TESTING.md",
        ),
    ),
    ImpactRule(
        "CLI",
        (r"^cli\.py$",),
        required_any=("docs/user-guide/cli.md", "docs/user-guide/user-manual.md"),
        review_required=("docs/user-guide/cli.md", "docs/user-guide/user-manual.md"),
    ),
    ImpactRule(
        "authentication/privacy",
        (
            r"^(?:core|utils)/.*(?:cookie|auth|browser_session|youtube_doctor)",
            r"^ui/.*(?:cookie|sign_in|youtube_doctor)",
            r"^utils/security\.py$",
        ),
        review_required=(
            "SECURITY.md", "PRIVACY.md", "docs/security/threat-model.md",
            "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "Spotify/search",
        (
            r"^core/(?:search_engine|scraper|spotify[^/]*)\.py$",
            r"^utils/(?:spotify[^/]*|ytm_scraper)\.py$",
        ),
        review_required=(
            "docs/user-guide/spotify-proxy-api.md", "docs/user-guide/user-manual.md",
            "docs/user-guide/user-guide-he.md", "PRIVACY.md",
            "docs/architecture/overview.md",
        ),
    ),
    ImpactRule(
        "Tag Editor safety",
        (
            r"^core/(?:metadata_|undo_applied_batch|restore_preview|change_drafts|tag_actions|backup_manager)",
            r"^core/(?:apply_plan|operation_manifest)\.py$",
            r"^ui/controllers/metadata_controller\.py$", r"^ui/workers/metadata_worker\.py$",
        ),
        review_required=(
            "docs/architecture/tag-editor-safety.md",
            "docs/architecture/tag-editor-undo-rollback-guarantees.md",
            "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md",
            "docs/design/tag-editor/current-design.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "Tag Editor UI/actions",
        (r"^ui/panels/metadata_editor/", r"^core/tag_actions"),
        review_required=(
            "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md",
            "docs/design/tag-editor/current-design.md",
            "docs/accessibility/ACCESSIBILITY.md", "docs/i18n/TRANSLATING.md",
        ),
    ),
    ImpactRule(
        "persistence/config",
        (
            r"^config\.py$", r"^config_migrate\.py$", r"^utils/paths\.py$",
            r"^core/.*(?:history_db|queue_persistence|update_state|cache|store|draft|journal|backup)",
        ),
        review_required=(
            "docs/migrations/README.md", "PRIVACY.md",
            "docs/architecture/overview.md", "docs/user-guide/user-manual.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "visible UI",
        (r"^main\.py$", r"^ui/.*\.py$"),
        review_required=(
            "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md",
            "docs/accessibility/ACCESSIBILITY.md", "docs/i18n/TRANSLATING.md",
        ),
    ),
    ImpactRule(
        "user-facing translations",
        (r"^ui/i18n\.py$",),
        review_required=("docs/i18n/TRANSLATING.md", "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md"),
    ),
    ImpactRule(
        "dependency inventory",
        (r"^requirements[^/]*\.txt$", r"^constraints(?:/|[^/]*\.txt$)"),
        required_all=("THIRD_PARTY_NOTICES.md",),
        review_required=("docs/security/supply-chain.md", "docs/release/RELEASING.md"),
        allow_no_impact=False,
    ),
    ImpactRule(
        "packaging/dependencies",
        (
            r"^packaging/", r"^pyproject\.toml$", r"^\.github/workflows/release-",
            r"^scripts/(?:build_|fetch_|stage_|generate_sbom)",
        ),
        required_any=("docs/release/RELEASING.md", "docs/security/supply-chain.md"),
        review_required=(
            "docs/release/RELEASING.md", "THIRD_PARTY_NOTICES.md",
            "SOURCE_OFFER.md", "docs/security/supply-chain.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "updaters/components",
        (r"^core/(?:update_checker|component_updates|runtime_components)\.py$",),
        review_required=(
            "docs/user-guide/user-manual.md", "SECURITY.md", "PRIVACY.md",
            "docs/architecture/secure-component-updater.md", "docs/security/threat-model.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "external metadata services",
        (
            r"^core/(?:musicbrainz|lyrics|metadata_lookup)",
            r"^core/providers/.*_provider\.py$",
            r"^utils/.*(?:api|client|scraper)\.py$",
        ),
        review_required=(
            "PRIVACY.md", "docs/user-guide/user-manual.md",
            "docs/security/threat-model.md", "docs/legal/acceptable-use.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "version/release channel",
        (r"^version\.py$",),
        required_all=("CHANGELOG.md",),
        review_required=("SECURITY.md", "docs/release/RELEASING.md"),
        allow_no_impact=False,
    ),
    ImpactRule(
        "platform/package support",
        (r"^\.github/workflows/(?:tests|release-[^/]+)\.yml$", r"^packaging/(?:bananaflow|macos)"),
        review_required=(
            "README.md", "docs/release/RELEASING.md", "SECURITY.md",
            "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md",
            "docs/AI_CONTEXT.md", "docs/testing/TESTING.md",
        ),
        allow_no_impact=False,
    ),
    ImpactRule(
        "documentation ownership/layout",
        (
            r"^AGENTS\.md$", r"^CLAUDE\.md$", r"^GEMINI\.md$",
            r"^\.github/(?:copilot-instructions|instructions/[^/]+\.instructions)\.md$",
            r"^\.github/(?:pull_request_template\.md|workflows/documentation\.yml)$",
            r"^docs/(?:README|AI_CONTEXT|DOCUMENTATION_POLICY)\.md$",
            r"^scripts/check_documentation\.py$", r"^tests/test_documentation_gate\.py$",
        ),
        required_any=("docs/README.md", "docs/DOCUMENTATION_POLICY.md", "docs/AI_CONTEXT.md"),
        review_required=("docs/README.md", "docs/DOCUMENTATION_POLICY.md", "docs/AI_CONTEXT.md"),
    ),
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _tracked_markdown_files() -> list[Path]:
    """Return Markdown files owned by this repository's Git index.

    Developer checkouts commonly contain virtual environments, extracted
    packages and ignored evidence directories. Scanning ``ROOT.rglob()`` would
    treat documentation shipped inside those local artifacts as BananaFlow
    documentation, making the documented local gate fail even though the same
    commit passes in a clean CI checkout.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed: {detail}")
    paths = proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    # During a local rename the index can still contain the deleted source and
    # the replacement can still be untracked. Include non-ignored new files and
    # filter vanished index entries so the local gate matches the proposed tree.
    return [ROOT / path for path in paths if path and (ROOT / path).is_file()]


def _is_stable() -> bool:
    text = _read("version.py")
    return bool(re.search(r"^PRERELEASE:\s*str\s*\|\s*None\s*=\s*None\s*$", text, re.M))


def _clean_link(raw: str) -> str:
    raw = raw.strip()
    if " " in raw and not raw.startswith("<"):
        raw = raw.split(" ", 1)[0]
    return raw.strip("<>")


def _looks_like_local_markdown(target: str) -> bool:
    if not target or target.startswith("#"):
        return False
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return False
    return unquote(parsed.path).lower().endswith(".md")


def _inside_root(candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(ROOT.resolve())
        return True
    except ValueError:
        return False


def _resolve_reference(source: Path, target: str, *, root_notation: bool = False) -> Path | None:
    parsed = urlsplit(_clean_link(target))
    rel = unquote(parsed.path)
    if not rel:
        return source

    candidate = ((ROOT if root_notation else source.parent) / rel).resolve()
    if not _inside_root(candidate):
        return None
    return candidate


def _github_anchor_slug(heading: str) -> str:
    """Return the ordinary GitHub-style anchor for a Markdown heading."""
    heading = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", heading)
    heading = heading.replace("`", "").strip().casefold()
    cleaned = "".join(
        char for char in heading
        if char in "-_ " or not unicodedata.category(char).startswith(("P", "S"))
    )
    return re.sub(r"\s+", "-", cleaned.strip())


def _markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        base = _github_anchor_slug(match.group(1))
        if not base:
            continue
        suffix = occurrences.get(base, 0)
        occurrences[base] = suffix + 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    anchors.update(
        unquote(value)
        for value in re.findall(r"<(?:a|[^>]+\s)(?:id|name)=[\"']([^\"']+)[\"']", text, re.I)
    )
    return anchors


def _reference_error(source: Path, target: str, *, root_notation: bool = False) -> str | None:
    parsed = urlsplit(_clean_link(target))
    if parsed.scheme or parsed.netloc:
        return None
    resolved = _resolve_reference(source, target, root_notation=root_notation)
    if resolved is None or not resolved.exists():
        return f"{source.relative_to(ROOT)}: broken Markdown reference {target!r}"
    if resolved.is_dir():
        readme = resolved / "README.md"
        if not readme.is_file():
            return f"{source.relative_to(ROOT)}: local directory link has no README {target!r}"
        resolved = readme
    if parsed.fragment and resolved.suffix.casefold() == ".md":
        fragment = unquote(parsed.fragment).casefold()
        if fragment not in _markdown_anchors(resolved):
            return f"{source.relative_to(ROOT)}: missing Markdown anchor {target!r}"
    return None


def _linked_markdown_files(source: Path) -> set[Path]:
    """Resolve clickable local Markdown links, including directory READMEs."""
    linked: set[Path] = set()
    text = source.read_text(encoding="utf-8")
    for match in MARKDOWN_LINK_RE.finditer(text):
        target = _clean_link(match.group(1))
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        rel = unquote(parsed.path)
        candidate = (source.parent / rel).resolve()
        if not _inside_root(candidate):
            continue
        if candidate.is_dir():
            candidate = candidate / "README.md"
        if candidate.is_file() and candidate.suffix.casefold() == ".md":
            linked.add(candidate.resolve())
    return linked


def check_required_files() -> list[str]:
    return [f"required documentation file missing: {path}" for path in REQUIRED_DOCS if not (ROOT / path).is_file()]


def check_markdown_references() -> list[str]:
    errors: list[str] = []
    for source in _tracked_markdown_files():
        text = source.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = _clean_link(match.group(1))
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            error = _reference_error(source, target)
            if error:
                errors.append(error)
        for match in BACKTICK_MD_RE.finditer(text):
            target = match.group(1)
            if not _looks_like_local_markdown(target):
                continue
            error = _reference_error(source, target, root_notation=True)
            if error:
                # Code-formatted paths are descriptive rather than clickable.
                # Existing policy tables use both repository-root and
                # document-relative notation, so accept either deliberately.
                error = _reference_error(source, target)
            if error:
                errors.append(error)
    return sorted(set(errors))


def check_docs_navigation() -> list[str]:
    """Ensure every tracked file below docs/ is reachable from its index."""
    tracked = {path.resolve() for path in _tracked_markdown_files()}
    entry = (ROOT / "docs/README.md").resolve()
    if entry not in tracked:
        return ["docs/README.md: documentation navigation entry point is not tracked"]

    reachable: set[Path] = set()
    pending = [entry]
    while pending:
        source = pending.pop()
        if source in reachable:
            continue
        reachable.add(source)
        pending.extend((_linked_markdown_files(source) & tracked) - reachable)

    docs_root = (ROOT / "docs").resolve()
    expected = {path for path in tracked if path.is_relative_to(docs_root)}
    return [
        f"{path.relative_to(ROOT)}: not reachable from docs/README.md"
        for path in sorted(expected - reachable)
    ]


def check_stale_release_language() -> list[str]:
    if not _is_stable():
        return []
    errors: list[str] = []
    for path in CURRENT_DOCS:
        file = ROOT / path
        if not file.exists():
            continue
        lowered = file.read_text(encoding="utf-8").casefold()
        for phrase in STABLE_FORBIDDEN_PHRASES:
            if phrase in lowered:
                errors.append(f"{path}: stale Stable/Beta wording contains {phrase!r}")
    return errors


def check_platform_support_language() -> list[str]:
    """Reject the retired platform-status wording in current documents.

    Historical evidence is deliberately not scanned here. Current product
    policy is: Windows 10/11 x64 + macOS Apple Silicon are supported packaged
    targets; Linux is supported from source but has no official package yet.
    """
    errors: list[str] = []
    files = [ROOT / path for path in CURRENT_PLATFORM_SURFACES]
    files.extend((ROOT / ".github/ISSUE_TEMPLATE").glob("*.yml"))
    for file in files:
        if not file.exists():
            continue
        lowered = file.read_text(encoding="utf-8").casefold()
        for phrase in PLATFORM_FORBIDDEN_PHRASES:
            if phrase in lowered:
                errors.append(
                    f"{file.relative_to(ROOT)}: stale platform-support wording contains {phrase!r}"
                )
    return errors


def check_current_community_routes() -> list[str]:
    """Keep user-facing support routes aligned with enabled repo features."""
    paths = [ROOT / path for path in COMMUNITY_ROUTE_FILES]
    paths.extend((ROOT / ".github/ISSUE_TEMPLATE").glob("*.yml"))
    errors: list[str] = []
    for path in paths:
        lowered = path.read_text(encoding="utf-8").casefold()
        if "/discussions" in lowered or "issues and discussions" in lowered:
            errors.append(
                f"{path.relative_to(ROOT)}: routes users to GitHub Discussions, "
                "which are not currently enabled"
            )
    return errors


def _provider_versions() -> dict[str, str]:
    values: dict[str, str] = {}
    stage = _read("packaging/stage_pot_provider.py")
    match = re.search(r'^PROVIDER_VERSION\s*=\s*["\']([^"\']+)', stage, re.M)
    if match:
        values["packaging/stage_pot_provider.py"] = match.group(1)
    pyproject = _read("pyproject.toml")
    match = re.search(r"bgutil-ytdlp-pot-provider==([0-9][^\"']*)", pyproject)
    if match:
        values["pyproject.toml"] = match.group(1)
    readme = _read("packaging/yt-dlp-plugins/README.md")
    match = re.search(r"bgutil-ytdlp-pot-provider==([0-9][0-9A-Za-z.\-+]*)", readme)
    if match:
        values["packaging/yt-dlp-plugins/README.md"] = match.group(1)
    return values


def check_provider_consistency() -> list[str]:
    values = _provider_versions()
    if len(values) < 2:
        return ["could not read PO Token Provider version from expected sources"]
    errors: list[str] = []
    if len(set(values.values())) != 1:
        errors.append("PO Token Provider version drift: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    version = next(iter(values.values()))
    if version not in _read("THIRD_PARTY_NOTICES.md"):
        errors.append(f"THIRD_PARTY_NOTICES.md does not mention staged provider version {version}")
    return errors


def _ytdlp_floor_sources() -> dict[str, str]:
    values: dict[str, str] = {}
    pyproject = _read("pyproject.toml")
    match = re.search(r"yt-dlp\[default\]>=([0-9]+(?:\.[0-9]+){2})", pyproject)
    if match:
        values["pyproject.toml"] = match.group(1)

    doctor = _read("core/youtube_doctor.py")
    match = re.search(r'^MIN_YT_DLP_VERSION\s*=\s*["\']([^"\']+)', doctor, re.M)
    if match:
        values["core/youtube_doctor.py"] = match.group(1)

    readme = _read("README.md")
    match = re.search(r"Compatibility floor\s+\*\*≥\s*([0-9]+(?:\.[0-9]+){2})\*\*", readme)
    if match:
        values["README.md"] = match.group(1)

    notices = _read("THIRD_PARTY_NOTICES.md")
    match = re.search(r"yt-dlp\[default\]>=([0-9]+(?:\.[0-9]+){2})", notices)
    if match:
        values["THIRD_PARTY_NOTICES.md"] = match.group(1)
    return values


def _version_numbers(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _ytdlp_exact_release_pins() -> dict[str, str]:
    values: dict[str, str] = {}
    requirements = _read("requirements.txt")
    match = re.search(r"^yt-dlp\[default\]==([^\s#]+)", requirements, re.M)
    if match:
        values["requirements.txt"] = match.group(1)
    pyproject = _read("pyproject.toml")
    exact = re.findall(r"yt-dlp\[default\]==([^\"']+)", pyproject)
    if exact:
        values["pyproject.toml dev extra"] = exact[0]
    return values


def check_ytdlp_consistency() -> list[str]:
    floors = _ytdlp_floor_sources()
    expected_floor_sources = {
        "pyproject.toml",
        "core/youtube_doctor.py",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
    }
    errors: list[str] = []
    missing = expected_floor_sources - set(floors)
    if missing:
        errors.append("could not read yt-dlp compatibility floor from: " + ", ".join(sorted(missing)))
        return errors
    if len(set(floors.values())) != 1:
        errors.append("yt-dlp compatibility-floor drift: " + ", ".join(f"{k}={v}" for k, v in floors.items()))
        return errors

    exact = _ytdlp_exact_release_pins()
    if {"requirements.txt", "pyproject.toml dev extra"} - set(exact):
        errors.append("could not read reviewed exact yt-dlp release/test pin from requirements.txt and pyproject dev extra")
        return errors
    if len(set(exact.values())) != 1:
        errors.append("yt-dlp exact release/test pin drift: " + ", ".join(f"{k}={v}" for k, v in exact.items()))
        return errors

    floor_tuple = _version_numbers(next(iter(floors.values())))
    exact_tuple = _version_numbers(next(iter(exact.values())))
    if exact_tuple[:3] < floor_tuple[:3]:
        errors.append(
            f"reviewed exact yt-dlp pin {next(iter(exact.values()))} is older than compatibility floor {next(iter(floors.values()))}"
        )
    return errors


def check_ai_adapters() -> list[str]:
    errors: list[str] = []
    for path in ("CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md"):
        text = _read(path) if (ROOT / path).is_file() else ""
        if "AGENTS.md" not in text or "docs/AI_CONTEXT.md" not in text:
            errors.append(f"{path}: must point to AGENTS.md and docs/AI_CONTEXT.md")
    return errors


def changed_files(base: str, head: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()}")
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]


def _pr_body() -> str:
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.is_file():
        return ""
    try:
        data = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str((data.get("pull_request") or {}).get("body") or "")


def _documentation_impact_section(body: str) -> str:
    match = re.search(r"^##\s+Documentation impact\s*$\n(.*?)(?=^##\s|\Z)", body, re.I | re.M | re.S)
    return match.group(1) if match else ""


def _no_docs_impact_checked(body: str) -> bool:
    return bool(
        re.search(
            r"^\s*-\s*\[[xX]\]\s*No documentation impact\s*$",
            _documentation_impact_section(body),
            re.M,
        )
    )


def _no_docs_impact_reason(body: str) -> str | None:
    if not _no_docs_impact_checked(body):
        return None
    match = re.search(
        r"^No documentation impact reason:\s*(.*?)\s*$",
        _documentation_impact_section(body),
        re.I | re.M,
    )
    if not match:
        return None
    reason = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.S).strip()
    placeholders = {"n/a", "none", "no impact", "not applicable", "tbd", "todo"}
    if len(reason) < 12 or reason.casefold().strip(". ") in placeholders:
        return None
    return reason


def _no_docs_impact_declared(body: str) -> bool:
    """Compatibility helper: a checked box counts only with a real reason."""
    return _no_docs_impact_reason(body) is not None


def _checked_documentation_categories(body: str) -> set[str]:
    section = _documentation_impact_section(body)
    checked: set[str] = set()
    for category in DOCUMENTATION_IMPACT_CATEGORIES:
        if re.search(rf"^\s*-\s*\[[xX]\]\s*{re.escape(category)}\s*$", section, re.M):
            checked.add(category)
    return checked


def _reviewed_doc_paths(body: str) -> set[str]:
    known = {
        path
        for rule in IMPACT_RULES
        for path in (*rule.required_all, *rule.required_any, *rule.review_required)
    }
    section = _documentation_impact_section(body).replace("\\", "/")
    match = re.search(
        r"^Relevant documentation updated/reviewed:\s*(.*?)\Z",
        section,
        re.I | re.M | re.S,
    )
    if not match:
        return set()

    reviewed: set[str] = set()
    negative = re.compile(r"\b(?:not|never|without|unreviewed|didn't|did\s+not)\b", re.I)
    for line in match.group(1).splitlines():
        bullet = re.match(r"^\s*[-*+]\s+(.+?)\s*$", line)
        if not bullet:
            continue
        item = bullet.group(1).strip()
        if not item or negative.search(item):
            continue
        for path in known:
            if re.match(
                rf"^`?{re.escape(path)}`?(?:\s*(?:$|[-—:;(]))",
                item,
                re.I,
            ):
                reviewed.add(path)
    return reviewed


def _is_impact_candidate(path: str) -> bool:
    """Return whether an executable/product path must belong to the map."""
    return bool(
        re.search(
            r"^(?:core|utils|ui|packaging|scripts)/|^\.github/workflows/|"
            r"^[^/]+\.py$|^pyproject\.toml$|"
            r"^requirements[^/]*\.txt$|^constraints(?:/|[^/]*\.txt$)",
            path,
            re.I,
        )
    )


def check_pr_impact(base: str, head: str) -> list[str]:
    files = changed_files(base, head)
    codeish = [f for f in files if f.endswith((".py", ".toml", ".txt", ".yml", ".yaml", ".ps1", ".sh", ".iss", ".spec"))]
    changed_md = {f for f in files if f.lower().endswith(".md")}
    body = _pr_body()
    no_docs_checked = _no_docs_impact_checked(body)
    no_docs_reason = _no_docs_impact_reason(body)
    no_docs = no_docs_reason is not None
    reviewed_docs = _reviewed_doc_paths(body)
    categories = _checked_documentation_categories(body)
    errors: list[str] = []

    if not categories:
        errors.append("select at least one Documentation impact category in the PR body")
    if "No documentation impact" in categories and len(categories) > 1:
        errors.append("'No documentation impact' cannot be selected with another impact category")

    if no_docs_checked and not no_docs:
        errors.append(
            "'No documentation impact' is checked, but the reason is missing, "
            "placeholder-only, or too short"
        )

    matched_files = {
        file
        for file in files
        if any(
            re.search(pattern, file, re.I)
            for rule in IMPACT_RULES
            for pattern in rule.path_patterns
        )
    }
    unmapped = sorted(file for file in codeish if _is_impact_candidate(file) and file not in matched_files)
    if unmapped and not no_docs:
        errors.append(
            "executable/product files are outside the Code -> Documentation map: "
            + ", ".join(unmapped)
            + "; add an impact rule or provide a valid no-impact reason"
        )

    for rule in IMPACT_RULES:
        matched = [f for f in files if any(re.search(pattern, f, re.I) for pattern in rule.path_patterns)]
        if not matched:
            continue

        if no_docs and not rule.allow_no_impact:
            errors.append(
                f"{rule.name} change ({', '.join(matched[:3])}) is sensitive and cannot use the global "
                "'No documentation impact' bypass; review the mapped documents explicitly"
            )
        if no_docs and rule.allow_no_impact:
            continue

        missing_all = set(rule.required_all) - changed_md
        if missing_all:
            errors.append(
                f"{rule.name} change ({', '.join(matched[:3])}) must update all required documents: "
                + ", ".join(sorted(missing_all))
            )

        if rule.required_any and not (changed_md & set(rule.required_any)):
            errors.append(
                f"{rule.name} change ({', '.join(matched[:3])}) must update at least one of: "
                + ", ".join(rule.required_any)
            )

        reviewed_or_changed = reviewed_docs | changed_md
        missing_reviews = set(rule.review_required) - reviewed_or_changed
        if missing_reviews:
            errors.append(
                f"{rule.name} change ({', '.join(matched[:3])}) requires explicit review of: "
                + ", ".join(sorted(missing_reviews))
            )
    return errors


def check_impact_policy_coverage() -> list[str]:
    """Keep the human policy table and executable impact rules in lockstep."""
    policy = _read("docs/DOCUMENTATION_POLICY.md")
    marker_lines: dict[str, list[str]] = {}
    for line in policy.splitlines():
        match = re.search(r"<!--\s*impact-rule:\s*(.*?)\s*-->", line)
        if match:
            marker_lines.setdefault(match.group(1), []).append(line)

    expected = {rule.name for rule in IMPACT_RULES}
    errors: list[str] = []
    for name in sorted(expected - set(marker_lines)):
        errors.append(f"docs/DOCUMENTATION_POLICY.md: missing impact-rule marker {name!r}")
    for name in sorted(set(marker_lines) - expected):
        errors.append(f"docs/DOCUMENTATION_POLICY.md: unknown impact-rule marker {name!r}")
    for name, lines in marker_lines.items():
        if len(lines) > 1:
            errors.append(f"docs/DOCUMENTATION_POLICY.md: duplicate impact-rule marker {name!r}")

    for rule in IMPACT_RULES:
        lines = marker_lines.get(rule.name, [])
        if len(lines) != 1:
            continue
        line = lines[0]
        mapped = set(rule.required_all) | set(rule.required_any) | set(rule.review_required)
        documented = set(re.findall(r"`([^`]+\.md)`", line))
        if mapped != documented:
            missing = sorted(mapped - documented)
            extra = sorted(documented - mapped)
            detail: list[str] = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("not enforced " + ", ".join(extra))
            errors.append(
                f"docs/DOCUMENTATION_POLICY.md: impact rule {rule.name!r} mapping differs: "
                + "; ".join(detail)
            )
    return errors


def run(base: str | None = None, head: str | None = None) -> list[str]:
    errors: list[str] = []
    for checker in (
        check_required_files,
        check_markdown_references,
        check_docs_navigation,
        check_stale_release_language,
        check_platform_support_language,
        check_current_community_routes,
        check_provider_consistency,
        check_ytdlp_consistency,
        check_ai_adapters,
        check_impact_policy_coverage,
    ):
        errors.extend(checker())
    if base and head:
        errors.extend(check_pr_impact(base, head))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()
    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be supplied together")

    errors = run(args.base, args.head)
    if errors:
        print("Documentation gate FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Documentation gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
