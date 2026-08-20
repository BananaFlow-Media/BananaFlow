# BananaFlow AI context

Status: **Current / normative**

This is a compact project map for coding agents. It deliberately links to detailed sources rather than duplicating them.

## Product

BananaFlow is a cross-platform PySide6 desktop application with a headless CLI. Windows 10/11 x64 and macOS Apple Silicon are supported packaged targets; Linux is supported from source even though no official Linux installer/package is published yet. It downloads and organizes media from YouTube/YouTube Music and resolves Spotify metadata to separate downloadable sources; it also includes search, history, a format converter and a safety-oriented batch Tag Editor. English and Hebrew/RTL are first-class UI languages.

The official website is a separate project. This repository contains the application, CLI, tests, packaging and application documentation.

The main English/Hebrew user manuals are written for ordinary non-programmer users. Developer/build/architecture details belong in contributor/testing/architecture/release documentation instead of being duplicated into the user manual.

## Architecture in one minute

- `ui/` — Qt/PySide6 presentation.
- `ui/controllers/` — coordination between UI and backend.
- `ui/workers/` — `QThread` bridges for long-running work.
- `core/` — backend engines, persistence and services; no Qt symbols.
- `utils/` — shared backend helpers; no Qt symbols.
- `packaging/` + `scripts/` — release assembly, staged components and build/test tooling.
- `tests/` — pytest suite; supported full gate is isolated per test file.

Read [`architecture/overview.md`](architecture/overview.md) and [`../PROJECT_STRUCTURE.md`](../PROJECT_STRUCTURE.md) before architecture changes.

## Safety-sensitive surfaces

Treat these as high-risk even when a requested change looks small:

- cookies, browser profiles, authentication and diagnostics;
- update/component-download logic and external executable/script runtimes;
- Tag Editor Apply/restore/rename/delete and persisted drafts/journals/backups;
- filesystem path construction, atomic replacement and migration;
- downloader retry/reliability behavior that can alter request volume;
- release packaging, third-party binaries, licenses, hashes and SBOM generation.

Read the corresponding security/architecture documents before editing them.

## Testing

Use focused tests while iterating. The supported full gate is:

```bash
python scripts/run_isolated_tests.py
```

Real-network tests are separate and release-oriented. See [`testing/TESTING.md`](testing/TESTING.md).

## Documentation is part of implementation

Before modifying code, locate the matching row in [`DOCUMENTATION_POLICY.md`](DOCUMENTATION_POLICY.md). At completion, update every affected source of truth or explain no impact in the PR. Do not leave TODOs that transfer documentation responsibility to an unspecified future task.

## Common references

- User behavior: [`user-guide/user-manual.md`](user-guide/user-manual.md), [`user-guide/user-guide-he.md`](user-guide/user-guide-he.md)
- Platform support/distribution: [`../README.md`](../README.md), [`release/RELEASING.md`](release/RELEASING.md)
- CLI: [`user-guide/cli.md`](user-guide/cli.md)
- Privacy: [`../PRIVACY.md`](../PRIVACY.md)
- Security: [`../SECURITY.md`](../SECURITY.md)
- Threat model: [`security/threat-model.md`](security/threat-model.md)
- Supply chain: [`security/supply-chain.md`](security/supply-chain.md)
- Accessibility: [`accessibility/ACCESSIBILITY.md`](accessibility/ACCESSIBILITY.md)
- Translation/RTL: [`i18n/TRANSLATING.md`](i18n/TRANSLATING.md)
- Release: [`release/RELEASING.md`](release/RELEASING.md)

## Agent finish checklist

1. Implementation matches the request and existing architecture.
2. Focused tests cover changed behavior.
3. Full isolated gate is run when scope warrants it.
4. Documentation impact map reviewed.
5. EN/HE behavior remains consistent.
6. Security/privacy/migration/release impact reviewed where applicable.
7. `python scripts/check_documentation.py` passes.
8. PR describes what was tested and which documentation changed.
