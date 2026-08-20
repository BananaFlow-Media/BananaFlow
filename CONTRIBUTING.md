# Contributing to BananaFlow

BananaFlow is an open-source desktop application. This repository contains the application, CLI, tests, packaging and application documentation; the official website is maintained separately at <https://bananaflow.bananaflow-media.workers.dev/>.

## Start here

Before changing code:

1. Read [`AGENTS.md`](AGENTS.md) if you are using an AI coding agent.
2. Read [`docs/README.md`](docs/README.md) and the subsystem documentation.
3. Review the Code → Documentation map in [`docs/DOCUMENTATION_POLICY.md`](docs/DOCUMENTATION_POLICY.md).
4. For large/architecture-changing work, open a focused [feature/proposal Issue](https://github.com/BananaFlow-Media/BananaFlow/issues/new?template=feature_request.yml) and align on the approach before investing in a large diff.

## Development setup

Requirements:

- Python 3.10+ (supported CI versions are defined by the workflow matrix);
- a recent FFmpeg on `PATH` for source-mode media operations;
- Playwright Chromium for source workflows that use browser-backed Spotify/channel/generic features;
- optional PO-token/runtime dependencies when exercising those source-mode paths.

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
```

Install Playwright Chromium for source development when your work needs it. Packaged builds stage their own browser/runtime inputs through release tooling.

## Running

```bash
python main.py
python cli.py --help
python cli.py --doctor
```

## Architecture rules

Read [`docs/architecture/overview.md`](docs/architecture/overview.md) and [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md).

- `core/` and `utils/` do not import Qt/PySide6 symbols; the documented plain-Python `ui.i18n.t()` lookup exception does not introduce Qt objects.
- Long-running GUI work uses `QThread` workers and returns through signals; never mutate widgets from a worker thread.
- The CLI drives the same backend logic without the Qt presentation layers.
- Tag Editor disk-changing paths are safety boundaries; read the Tag Editor safety/undo documents before modifying them.

## Testing

[`docs/testing/TESTING.md`](docs/testing/TESTING.md) is the single source of truth.

Focused iteration example:

```bash
QT_QPA_PLATFORM=offscreen pytest tests/test_example.py -q
```

Supported full gate:

```bash
python scripts/run_isolated_tests.py
```

Documentation gate:

```bash
python scripts/check_documentation.py
```

Real-network compatibility tests:

```bash
python scripts/run_network_tests.py
```

The real-network suite is intentionally separate from deterministic CI because third-party sites can challenge CI egress. **Run `scripts/run_network_tests.py` from a normal network before cutting a release.** The scheduled workflow is supporting evidence, not a substitute for the maintainer pre-release run.

## Documentation is part of the change

A behavior-changing PR must update the mapped documentation or explicitly explain why it has no documentation impact. The PR template and documentation CI enforce the common cases.

In particular, review:

- English + Hebrew user manuals for user-visible behavior;
- `PRIVACY.md` for new/changed network calls, retained data or transmitted fields;
- `SECURITY.md` + threat model for authentication, credentials, downloaded-code/update or destructive-path changes;
- migration docs for persisted schema/path/meaning changes;
- release/notices/source docs for dependency, packaging or bundled-component changes.

Do not create a new source of truth when an existing one can be linked.

## RTL, accessibility and translations

Read [`docs/i18n/TRANSLATING.md`](docs/i18n/TRANSLATING.md) and [`docs/accessibility/ACCESSIBILITY.md`](docs/accessibility/ACCESSIBILITY.md).

Every new user-facing string must exist in both language tables. New/custom interactive widgets need keyboard access and meaningful accessible semantics. Technical paths/URLs/identifiers must remain readable in RTL layouts.

Translation/RTL problems use the dedicated [Hebrew / Translation Issue form](https://github.com/BananaFlow-Media/BananaFlow/issues/new?template=hebrew_translation.yml). Pre-release/nightly/build feedback should use the closest Issue form and include the exact build identifier. GitHub Discussions are not currently enabled for this repository, so current documentation must not route contributors there.

## Dependencies and third-party code

Prefer existing dependencies/standard library when reasonable. A new runtime dependency or bundled asset requires a compatible license review and update to `THIRD_PARTY_NOTICES.md`; release/source obligations must remain satisfied.

Do not commit staged release inputs or generated binaries under packaging staging slots. Their tracked README files describe how builds regenerate them.

## Security

Never commit or paste live credentials, cookies, access/proxy tokens or private diagnostic data. Vulnerability reports belong in the private channel documented by `SECURITY.md`, not a public PR/Issue.

Security-sensitive changes — cookies/auth, update/component retrieval, runtime execution, downloader request behavior and destructive filesystem paths — need explicit review.

## Commits and PRs

- Keep a PR focused on one logical change.
- Explain **why** in commit messages/PR text; the diff already shows what.
- Include screenshots/recordings for visible UI changes when practical.
- Add/update tests for changed behavior.
- Complete the documentation-impact section of the PR template.
- Do not hide known failures or silently weaken a gate to make CI green.

## Contribution license

BananaFlow's own code is GPL-3.0-or-later. Unless a file says otherwise, contributions are accepted under the same license (inbound = outbound). By submitting a contribution you confirm you have the right to provide it under those terms.

BananaFlow currently requires neither a CLA nor DCO sign-off. A future relicensing/commercial program would need to address contributor rights explicitly; no such policy is active today.
