# Testing BananaFlow

Status: **Current / normative**

This file is the single source of truth for test strategy and supported test entry points. `CONTRIBUTING.md` links here rather than maintaining a competing policy.

## Fast iteration

Install development dependencies:

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
```

Run a focused file or test while iterating:

```bash
QT_QPA_PLATFORM=offscreen pytest tests/test_example.py -q
```

On PowerShell, set the environment variable using PowerShell syntax when needed. GUI tests must run offscreen unless the test is explicitly a manual visual check.

## Supported full gate

```bash
python scripts/run_isolated_tests.py
```

This is the supported full-suite gate. Each tracked test file runs in a fresh interpreter. BananaFlow uses this because long single-process PySide6 suites can accumulate native Qt state and fault during teardown on Windows even when individual tests are correct.

Do not replace the release/CI gate with a single `pytest tests/` invocation merely because it passes on one machine.

The regular CI matrix exercises the source tree on Windows and Ubuntu across the supported Python versions, and failures on either operating system block the test workflow. That is part of the evidence for Linux source-install support; lack of a Linux installer is a packaging gap, not a statement that the application is expected to fail on Linux. macOS packaged support is additionally validated through its release workflow and manual package acceptance.

## Test layers

1. **Unit tests** — pure functions, parsers, policies, models and helpers; no network.
2. **Component tests** — backend components with mocked filesystem/network/process boundaries.
3. **Qt/headless tests** — panels, controllers, workers, RTL/accessibility contracts with `QT_QPA_PLATFORM=offscreen`.
4. **Packaging/static gates** — version consistency, license/source checks, build metadata and staged-component policy.
5. **Real-network tests** — explicitly separated because third-party sites change and CI egress can be challenged.
6. **Manual acceptance** — clean-machine packaged smoke, visual QA and destructive-file safety checks on disposable fixtures.

## Real-network tests

```bash
python scripts/run_network_tests.py
```

These tests are not part of ordinary deterministic runs. Run them before release from a normal network. The scheduled GitHub Actions run is useful evidence but advisory because YouTube and other services may challenge CI IP ranges.

A new network-gated test file must be registered with the network-test runner so it cannot become invisible coverage.

## Documentation gate

Every repository change should also pass:

```bash
python scripts/check_documentation.py
```

The gate validates internal Markdown references, canonical documentation structure, stale release-status wording, component/document consistency and (in PR mode) Code → Documentation impact expectations.

## Adding tests

- Test behavior, not implementation details where practical.
- Reproduce bugs with a failing test before or with the fix.
- Do not use live credentials, cookies or realistic secret fixtures.
- Network-dependent behavior must be mocked in ordinary tests.
- Artist-catalog duplicate tests must distinguish an exact repeated release
  occurrence from a recording kept in separate releases. Spotify matching
  tests must verify that distinct source identities cannot silently claim the
  same concrete YouTube video, that bounded rematching remains deterministic,
  and that query-variant fallback preserves both artist and title, scores all
  discovered candidates and stops issuing requests after a decisive match.
  Album-fallback tests must verify release/artist identity, bounded expansion
  and reuse of one cached catalog across adjacent tracks. Manual-source tests
  must prove that only a single YouTube track URL is accepted and that the
  retry retains the original metadata, numbering and output layout.
- Filename-numbering tests must cover source/release combinations with the
  provider's original position and must keep filename prefixes independent
  from embedded track-number tags. Queue order is never an accepted fallback.
- Output-layout tests must keep collection identity separate from per-track
  album/artist tags, cover direct and artist-import folder trees, and exercise
  title-collision and multi-disc behavior.
- Download-recovery tests must prove that local errors do not close admission,
  equivalent failures update one incident, authentication/network work stops
  exactly on the third consecutive failure, every collected authentication
  track is resubmitted after repair, and explicit rate limits retain their raw
  message, safety margin, countdown and same-track canary ordering. Use injected
  clocks and sub-second waits; never exercise a real provider limit.
- Filesystem-destructive tests use disposable temporary directories/files only.
- New Qt work must cover teardown/cancellation ownership when threads/timers/callbacks are involved.
- New user-facing strings must preserve i18n coverage.
- New persisted state needs migration/failure-path tests.

## Manual QA

Manual checklists live under [`../qa/`](../qa/). They complement, not replace, automated tests. Record the product version/commit and platform when performing a checklist so the evidence does not masquerade as permanently current.
