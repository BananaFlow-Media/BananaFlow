#!/usr/bin/env python3
"""BananaFlow documentation consistency gate.

Runs with the standard library only. Static mode checks the checked-out tree.
PR mode (``--base``/``--head``) additionally checks Code → Documentation
impact expectations using the pull-request body from ``GITHUB_EVENT_PATH``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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
    "docs/user-guide/user-manual.md",
    "docs/user-guide/user-guide-he.md",
    "docs/release/RELEASING.md",
)

STABLE_FORBIDDEN_PHRASES = (
    "no stable release yet",
    "first stable release will be",
    "current beta series",
    "road from the current beta",
    "v0.1.0 is the latest public release",
    "no public project website is currently operated",
    "a project website" + ", winget",  # catches old roadmap wording only
)

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
BACKTICK_MD_RE = re.compile(r"`([^`\n]+\.md(?:#[^`\n]+)?)`")


@dataclass(frozen=True)
class ImpactRule:
    name: str
    path_patterns: tuple[str, ...]
    docs_any: tuple[str, ...]


IMPACT_RULES = (
    ImpactRule("CLI", (r"^cli\.py$",), ("docs/user-guide/cli.md", "docs/user-guide/user-manual.md")),
    ImpactRule(
        "authentication/privacy",
        (r"cookie", r"auth", r"youtube_doctor", r"runtime_components", r"update_checker", r"component_updates"),
        ("SECURITY.md", "PRIVACY.md", "docs/security/threat-model.md", "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md"),
    ),
    ImpactRule(
        "Spotify/search",
        (r"core/search_engine\.py$", r"core/scraper\.py$", r"spotify"),
        ("docs/user-guide/spotify-proxy-api.md", "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md", "PRIVACY.md"),
    ),
    ImpactRule(
        "Tag Editor safety",
        (r"metadata_", r"tag_editor", r"undo_applied_batch", r"restore_preview", r"change_drafts", r"tag_actions"),
        ("docs/architecture/tag-editor-safety.md", "docs/architecture/tag-editor-undo-rollback-guarantees.md", "docs/user-guide/user-manual.md", "docs/user-guide/user-guide-he.md"),
    ),
    ImpactRule(
        "persistence/config",
        (r"^config\.py$", r"^config_migrate\.py$", r"history_db", r"queue_persistence", r"update_state"),
        ("docs/migrations/README.md", "PRIVACY.md", "docs/user-guide/user-manual.md"),
    ),
    ImpactRule(
        "packaging/dependencies",
        (r"^packaging/", r"^requirements\.txt$", r"^pyproject\.toml$", r"^constraints", r"^\.github/workflows/release-", r"^scripts/build_", r"^scripts/fetch_", r"generate_sbom"),
        ("docs/release/RELEASING.md", "THIRD_PARTY_NOTICES.md", "SOURCE_OFFER.md", "docs/security/supply-chain.md"),
    ),
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _is_stable() -> bool:
    text = _read("version.py")
    return bool(re.search(r"^PRERELEASE:\s*str\s*\|\s*None\s*=\s*None\s*$", text, re.M))


def _clean_link(raw: str) -> str:
    raw = raw.strip()
    if " " in raw and not raw.startswith("<"):
        # Markdown target may contain an optional quoted title.
        raw = raw.split(" ", 1)[0]
    return raw.strip("<>")


def _looks_like_local_markdown(target: str) -> bool:
    if not target or target.startswith("#"):
        return False
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return False
    path = unquote(parsed.path)
    return path.lower().endswith(".md")


def _resolve_reference(source: Path, target: str) -> Path | None:
    parsed = urlsplit(_clean_link(target))
    rel = unquote(parsed.path)
    if not rel:
        return source
    candidate = (source.parent / rel).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError:
        # Some root community docs use ../../issues or discussions links; only
        # actual Markdown file references are considered local documentation.
        return None
    return candidate


def check_required_files() -> list[str]:
    return [f"required documentation file missing: {path}" for path in REQUIRED_DOCS if not (ROOT / path).is_file()]


def check_markdown_references() -> list[str]:
    errors: list[str] = []
    for source in sorted(ROOT.rglob("*.md")):
        if ".git" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        targets = [_clean_link(m.group(1)) for m in MARKDOWN_LINK_RE.finditer(text)]
        targets.extend(m.group(1) for m in BACKTICK_MD_RE.finditer(text))
        for target in targets:
            if not _looks_like_local_markdown(target):
                continue
            resolved = _resolve_reference(source, target)
            if resolved is None:
                continue
            if not resolved.is_file():
                errors.append(f"{source.relative_to(ROOT)}: broken Markdown reference {target!r}")
    return sorted(set(errors))


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


def _provider_versions() -> dict[str, str]:
    values: dict[str, str] = {}
    stage = _read("packaging/stage_pot_provider.py")
    m = re.search(r'^PROVIDER_VERSION\s*=\s*["\']([^"\']+)', stage, re.M)
    if m:
        values["packaging/stage_pot_provider.py"] = m.group(1)
    pyproject = _read("pyproject.toml")
    m = re.search(r"bgutil-ytdlp-pot-provider==([0-9][^\"']*)", pyproject)
    if m:
        values["pyproject.toml"] = m.group(1)
    readme = _read("packaging/yt-dlp-plugins/README.md")
    m = re.search(r"bgutil-ytdlp-pot-provider==([0-9][0-9A-Za-z.\-+]*)", readme)
    if m:
        values["packaging/yt-dlp-plugins/README.md"] = m.group(1)
    return values


def check_provider_consistency() -> list[str]:
    values = _provider_versions()
    errors: list[str] = []
    if len(values) < 2:
        errors.append("could not read PO Token Provider version from expected sources")
        return errors
    if len(set(values.values())) != 1:
        errors.append("PO Token Provider version drift: " + ", ".join(f"{k}={v}" for k, v in values.items()))
    version = next(iter(values.values()))
    if version not in _read("THIRD_PARTY_NOTICES.md"):
        errors.append(f"THIRD_PARTY_NOTICES.md does not mention staged provider version {version}")
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
    event_path = Path(str(__import__("os").environ.get("GITHUB_EVENT_PATH", "")))
    if not event_path.is_file():
        return ""
    try:
        data = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str((data.get("pull_request") or {}).get("body") or "")


def _no_docs_impact_declared(body: str) -> bool:
    return bool(re.search(r"-\s*\[[xX]\]\s*No documentation impact", body)) and bool(
        re.search(r"No documentation impact reason:\s*\S.+", body, re.I)
    )


def check_pr_impact(base: str, head: str) -> list[str]:
    files = changed_files(base, head)
    codeish = [f for f in files if f.endswith((".py", ".toml", ".txt", ".yml", ".yaml", ".ps1", ".sh", ".iss", ".spec"))]
    changed_md = {f for f in files if f.lower().endswith(".md")}
    body = _pr_body()
    no_docs = _no_docs_impact_declared(body)
    errors: list[str] = []

    if codeish and not changed_md and not no_docs:
        errors.append(
            "code/build behavior changed without any Markdown update; update affected docs or check "
            "'No documentation impact' in the PR template and provide a reason"
        )

    for rule in IMPACT_RULES:
        matched = [f for f in files if any(re.search(p, f, re.I) for p in rule.path_patterns)]
        if not matched or no_docs:
            continue
        if not (changed_md & set(rule.docs_any)):
            errors.append(
                f"{rule.name} change ({', '.join(matched[:3])}) requires review/update of at least one mapped "
                f"document: {', '.join(rule.docs_any)} (or an explicit no-impact reason)"
            )
    return errors


def run(base: str | None = None, head: str | None = None) -> list[str]:
    errors: list[str] = []
    for checker in (
        check_required_files,
        check_markdown_references,
        check_stale_release_language,
        check_provider_consistency,
        check_ai_adapters,
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
