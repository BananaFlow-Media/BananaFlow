# BananaFlow documentation map

This directory is the documentation entry point. It distinguishes **current normative documentation** from **historical evidence** so a reader or AI agent can tell what describes the product now and what records an earlier decision or measurement.

## Start here

| Need | Authoritative document |
|---|---|
| End-user overview/download | [`README.md`](../README.md) and the official website |
| Full English user reference | [`user-guide/user-manual.md`](user-guide/user-manual.md) |
| Full Hebrew user reference | [`user-guide/user-guide-he.md`](user-guide/user-guide-he.md) |
| CLI reference | [`user-guide/cli.md`](user-guide/cli.md) |
| Contributor workflow | [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Test strategy and commands | [`testing/TESTING.md`](testing/TESTING.md) |
| Architecture overview | [`architecture/overview.md`](architecture/overview.md) |
| Detailed module map | [`PROJECT_STRUCTURE.md`](../PROJECT_STRUCTURE.md) |
| Documentation lifecycle / Code → Docs mapping | [`DOCUMENTATION_POLICY.md`](DOCUMENTATION_POLICY.md) |
| AI-agent project context | [`AI_CONTEXT.md`](AI_CONTEXT.md) |
| Security reporting/support | [`SECURITY.md`](../SECURITY.md) |
| Threat model | [`security/threat-model.md`](security/threat-model.md) |
| Supply chain | [`security/supply-chain.md`](security/supply-chain.md) |
| Privacy/network/local data | [`PRIVACY.md`](../PRIVACY.md) |
| Accessibility contract | [`accessibility/ACCESSIBILITY.md`](accessibility/ACCESSIBILITY.md) |
| Translation/RTL contract | [`i18n/TRANSLATING.md`](i18n/TRANSLATING.md) |
| Release process | [`release/RELEASING.md`](release/RELEASING.md) |
| License inventory | [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) |
| Source availability | [`SOURCE_OFFER.md`](../SOURCE_OFFER.md) |

## Current architecture and design

- [`architecture/overview.md`](architecture/overview.md) — component boundaries, threads, persistence, network/trust boundaries.
- [`architecture/tag-editor-safety.md`](architecture/tag-editor-safety.md) — binding Tag Editor disk-safety invariants.
- [`architecture/tag-editor-undo-rollback-guarantees.md`](architecture/tag-editor-undo-rollback-guarantees.md) — contributor-facing undo/restore summary.
- [`architecture/tag-editor-persistence-migrations.md`](architecture/tag-editor-persistence-migrations.md) — persisted Tag Editor migrations.
- [`design/tag-editor/implementation-plan.md`](design/tag-editor/implementation-plan.md) — current Tag Editor design decisions and completed redesign record.

## Decision records and historical evidence

Some documents were originally produced during release-readiness phases. They remain valuable evidence but must not silently override current code or normative documentation. Such documents carry an explicit status banner and date. Examples include browser packaging research, component-updater design, PO-token distribution research and package/runtime measurements.

Historical documents are useful for **why** a decision was made; current normative documents define **what the project promises now**.

## QA and verification

Manual visual/platform checks live under [`qa/`](qa/). They complement automated tests; they are not end-user instructions.

## Documentation rule

Any behavior-changing code change must review and update the documentation rows that apply in [`DOCUMENTATION_POLICY.md`](DOCUMENTATION_POLICY.md). CI runs `scripts/check_documentation.py` to catch broken links, stale release wording and common documentation drift.
