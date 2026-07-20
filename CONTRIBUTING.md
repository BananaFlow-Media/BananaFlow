# Contributing to BananaFlow

Thanks for helping make BananaFlow better. This project aims to be a
practical, community-friendly open-source desktop app.

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.10 or newer (3.10/3.11/3.12 are all tested in CI) |
| FFmpeg | Any recent version, on `PATH` |
| Playwright (Chromium) | Latest, for Spotify / channel scraping |
| Deno / PO Token provider | Optional — see below |

## Dev Environment Setup

```bash
python -m venv venv
# Windows: venv\Scripts\activate    macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
```

Install Playwright's Chromium browser (required for Spotify and YouTube
channel scraping; skipping this only disables those two features):

```bash
# Windows
install_playwright.bat
# macOS/Linux
python3 -m playwright install chromium
```

Install FFmpeg and make sure it is on `PATH` (`choco install ffmpeg` on
Windows, `brew install ffmpeg` on macOS, or your distro's package manager
on Linux).

The PO Token provider and Deno runtime are optional for source
development: `pip install -e ".[dev,po-token]"` adds
`bgutil-ytdlp-pot-provider`; without it, some YouTube videos may show a
PO Token error, exactly as `README.md`'s "PO Token Provider" section
describes for a packaged build without the bundled provider.

## Running the App

**GUI:**
```bash
python main.py
```

**CLI (headless):**
```bash
python cli.py <url>
python cli.py --doctor      # environment/dependency health check
python cli.py --version
```

## Running Tests

```bash
QT_QPA_PLATFORM=offscreen pytest tests/ -q
```

`QT_QPA_PLATFORM=offscreen` is required, not optional — without it,
PySide6-based tests try to open a real window and either crash (no
display) or pop up windows during the run. Use plain `pytest` for a
single file or subset while iterating.

**Before opening a PR, run the isolated full gate** — this is the actual
release/CI gate, not a single `pytest` process:

```bash
python scripts/run_isolated_tests.py
```

This runs every tracked test file in its own interpreter and reads each
child's real exit code. A single-process `pytest tests/` run of the full
suite is not reliable on Windows (Qt state accumulates across GUI test
files until a native fault partway through with no summary) — the
isolated runner is what `scripts/build_windows.ps1` and every CI leg run,
so a green local run and a green CI run check the same thing.

### Real-network tests

Almost every test mocks yt-dlp and Playwright, deliberately: the suite
must not depend on YouTube being reachable or unchanged. The cost is that
it structurally cannot notice the failure mode that actually breaks this
app in the field — YouTube changing something on their side. A few tests
do hit the real network and are gated so they stay out of ordinary runs:

```bash
python scripts/run_network_tests.py
```

**Run this before cutting a release**, from a real network. It is a
required pre-release check, not an optional extra: channel tab discovery
has already broken once against live YouTube (issue #27) and was found by
accident rather than by a test. `.github/workflows/network-tests.yml`
also runs it weekly, but that run is advisory only — YouTube blocks many
GitHub Actions egress IPs, so a red weekly run may mean the runner was
challenged rather than that BananaFlow is broken. A failure from your own
machine is the signal that counts.

Adding a network-gated test to a new file? Add that file to
`NETWORK_TEST_FILES` in `scripts/run_network_tests.py` — otherwise it is
a test nobody runs, which is worth less than no test at all because it
looks like coverage.

## Architecture

`core/` and `utils/` import **no Qt/PySide6** and run headlessly — the
CLI (`cli.py`) drives the exact same core through the same
`OrchestratorCallbacks` protocol the Qt worker implements. See
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) for the full layer diagram
and module map. In short:

* **UI layer** (`ui/`, PySide6/Qt6) — panels, dialogs, components.
* **Controllers** (`ui/controllers/`) — business logic between the window
  and the core engines.
* **Workers** (`ui/workers/`) — `QThread` bridges: run core code off the
  UI thread, communicate only via Qt signals.
* **Core** (`core/`) — the actual download/scrape/convert/tag engines,
  zero Qt imports, directly unit-testable and CLI-usable.
* **Utils** (`utils/`, `config.py`) — shared helpers, also zero Qt.

### Qt thread rules

Long-running work (downloads, scraping, conversion, metadata scans)
always runs on a `QThread` worker, never on the UI thread. Workers
communicate results back to the UI **only** via Qt signals — never by
calling UI methods directly from a worker thread, and never by mutating
Qt widgets from outside the main thread. If a change needs to call into
`core/`/`utils/` from a new context, add or extend a worker rather than
importing Qt into `core/`/`utils/`.

### Core/UI separation

Nothing in `core/` or `utils/` may import PySide6/Qt (the one deliberate
exception is `ui.i18n.t()`, a plain-Python translation lookup two backend
modules use for user-facing strings — see `PROJECT_STRUCTURE.md`). This
keeps the CLI and the test suite able to exercise real logic without a
display, and keeps a change usable from both the GUI and `cli.py`.

### Tag Editor safety, undo and rollback

The Tag Editor's Apply/backup/journal/undo behavior is a hard safety
boundary, not an implementation detail — see
[`docs/architecture/tag-editor-undo-rollback-guarantees.md`](docs/architecture/tag-editor-undo-rollback-guarantees.md)
for a contributor-facing summary of what's guaranteed (backup-before-write,
the durable Apply journal, verify-before-replace, the two separate undo
mechanisms) before touching `core/metadata_processor.py`,
`ui/workers/metadata_worker.py`, or `core/undo_applied_batch.py`. The full
binding requirements are `docs/architecture/tag-editor-safety.md`.

## RTL, Accessibility and Translations

The app supports Hebrew (RTL) and English (LTR) with full layout mirroring
(`ui/direction.py`, `ui/i18n.py`). Any new user-facing string must be
added to **both** language tables in `ui/i18n.py` — a string present in
only one language fails `tests/test_i18n_coverage.py`. Do not hardcode
user-facing text directly in a widget; always go through `t()`. New
interactive widgets should have accessible names/roles consistent with
existing panels — see `tests/test_phase14_accessibility_rtl_dpi.py` for
the kind of check a change should not break.

## Dependency Policy

Do not add a new runtime dependency without a clear reason; prefer the
standard library or an already-used dependency. Any new dependency needs
a compatible license (see below) and an entry in `THIRD_PARTY_NOTICES.md`.

## License Policy

Third-party code, assets, or dependencies must have a license compatible
with GPL-3.0-or-later, recorded in `THIRD_PARTY_NOTICES.md`. See
"Third-Party Code" below for what not to commit.

## Commit Style and PR Scope

* Keep commits and PRs focused on one logical change — a bug fix, a
  feature, a refactor. Avoid bundling unrelated changes.
* Write commit messages that explain *why*, not just *what* — the diff
  already shows what changed.
* Include a screenshot or short clip for any visible UI change (before/
  after, or just after for a new element), in the PR description.
* Add or update tests for the behavior your PR changes. A PR that changes
  `core/`/`utils/` logic without a corresponding test is unlikely to be
  merged as-is.
* Update relevant documentation (`README.md`, `PROJECT_STRUCTURE.md`,
  in-code docstrings) when behavior, setup, or architecture changes.
* Security-relevant changes (auth, cookies, secrets handling, network
  behavior, the PO Token provider path) need explicit review — flag them
  clearly in the PR description; see `SECURITY.md` for the reporting
  process if you are instead disclosing a vulnerability rather than
  fixing one.
* Do not commit generated files, staged release inputs (see "Third-Party
  Code" below), or secrets/credentials of any kind — including test
  fixtures that look like real tokens or cookies.
* For a large or architecture-changing proposal, start in
  [Discussions → Development](../../discussions/categories/development)
  first to align on approach before investing in a large PR — this
  avoids rework on both sides. Translation and RTL coordination that is
  broader than a single string belongs in
  [Discussions → Translations](../../discussions/categories/translations).

## License of Contributions

BananaFlow's own code is licensed under GPL-3.0-or-later. Unless a file says
otherwise, contributions you submit to this repository are accepted under
the same license. In short: inbound license equals outbound license.

By opening a pull request, issue patch, or other contribution, you
confirm that you have the right to submit the contribution under
GPL-3.0-or-later and that the project may redistribute it under that
license.

## No CLA or DCO For Now

BananaFlow does not currently require a Contributor License Agreement (CLA)
or Developer Certificate of Origin (DCO) sign-off. This keeps small
community contributions easy.

Future dual licensing, proprietary relicensing, or a commercial license
program may require contributor consent, replacement of contributed
code, or a CLA for future contributions. No such policy is active today.

## Third-Party Code

Do not copy code, assets, binaries, or generated files into the project
unless their license is compatible with GPL-3.0-or-later and the license
notice can be recorded in `THIRD_PARTY_NOTICES.md`.

Generated or staged release inputs such as `packaging/ffmpeg/`,
`packaging/runtime/`, and `packaging/yt-dlp-plugins/` are intentionally
not tracked except for their README files.

## Safety Boundaries

Changes should not weaken Conservative Mode, implement unsafe PO Token
handling, scrape/store/inject PO Tokens manually, or change downloader
behavior without explicit review.
