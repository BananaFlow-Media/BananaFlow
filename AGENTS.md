# BananaFlow agent entry point

This file is the mandatory starting point for AI coding agents and automated contributors working in this repository.

## Read before changing code

1. Read [`docs/AI_CONTEXT.md`](docs/AI_CONTEXT.md).
2. Read [`docs/README.md`](docs/README.md) to locate the authoritative documentation for the subsystem you will change.
3. Read [`docs/DOCUMENTATION_POLICY.md`](docs/DOCUMENTATION_POLICY.md), especially the Code → Documentation impact map.
4. Read the subsystem-specific document(s) listed there before editing implementation code.
5. For development and test rules, read [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`docs/testing/TESTING.md`](docs/testing/TESTING.md).

## Non-negotiable completion rule

A behavior-changing task is not complete when the code works. It is complete only when all applicable parts are updated together:

- implementation;
- automated tests;
- user/developer documentation;
- English/Hebrew user-facing text when applicable;
- migration notes for persisted data/schema/path changes;
- architecture notes for architectural changes;
- security/privacy documentation for trust, credential, network, storage, updater or supply-chain changes;
- release/licensing documentation for packaging, dependency or distribution changes.

If a change genuinely has no documentation impact, state why in the pull request. Do not silently omit documentation review.

## Sources of truth

Do not copy facts into new files when an existing source of truth can be referenced. Important examples:

- application version/product metadata → `version.py`;
- release history → `CHANGELOG.md`;
- release procedure → `docs/release/RELEASING.md`;
- architecture overview → `docs/architecture/overview.md` and `PROJECT_STRUCTURE.md`;
- test policy → `docs/testing/TESTING.md`;
- security reporting/support policy → `SECURITY.md`;
- privacy/network/local-data behavior → `PRIVACY.md`;
- third-party license inventory → `THIRD_PARTY_NOTICES.md`;
- source availability → `SOURCE_OFFER.md`;
- user behavior → `docs/user-guide/user-manual.md` and the Hebrew companion;
- documentation ownership/lifecycle → `docs/DOCUMENTATION_POLICY.md`.

## Repository invariants

- `core/` and `utils/` stay Qt/PySide6-free except for the documented plain-Python i18n lookup exception.
- Long-running GUI work runs off the UI thread and returns through Qt signals.
- Tag Editor Apply/restore safety guarantees must not be weakened.
- Never commit credentials, cookies, tokens, staged release binaries or generated release inputs.
- Every new user-facing string must exist in both English and Hebrew translation tables.
- Do not weaken conservative YouTube reliability or credential-handling safeguards without explicit review.

## Before finishing

Run the relevant focused tests, then the supported full gate when the scope warrants it. Run the documentation gate for any repository change:

```bash
python scripts/check_documentation.py
python scripts/run_isolated_tests.py
```

For real-network behavior, follow `docs/testing/TESTING.md`; ordinary unit tests must remain deterministic and network-independent.
