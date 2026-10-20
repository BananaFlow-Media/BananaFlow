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

Startup-loading regression tests also verify in fresh interpreter processes that importing UI data types does not eagerly load provider/network stacks, that package-level worker exports remain lazy, and that production navigation preparation materializes Settings and the Tag Editor before handoff. Release acceptance should still measure the exact packaged artifact on a clean machine because antivirus and cold-disk effects are outside source-mode unit tests.

Startup-preflight shutdown tests must cover cooperative cancellation before the Playwright probe, joining the worker before interpreter teardown, and a Playwright 1.62 availability probe that exits without pending asyncio task/future errors. A passing functional preflight is insufficient if process exit emits cleanup diagnostics.

Immediate-close coverage must also verify that the startup update worker honours interruption between app and component checks and never emits results after cancellation; Qt must not destroy a running child `QThread`.

## Application updater acceptance

Updater changes require more than button or mocked subprocess tests. Cover both independently prepared downloader components and full application updates, including a successful explicit restart, Later/close without restarting, active-work vetoes, thread ownership and failures before replacement. Retain English/Hebrew UI coverage.

The packaged updater gate must use real Windows installer/portable and macOS application artifacts, disposable installation/profile directories, a running old process, the packaged helper, actual replacement and a fresh process reporting the candidate version. Assert preserved user data and a real failure/recovery case. Configuration migration alone is not old-to-new application upgrade evidence. Record exact artifact hashes, versions and process outcomes. Test fixture releases must never be published or selected by real clients.

The macOS packaged-updater job and release job share the native Apple Silicon `macos-15` runner; changing this build host must retain explicit arm64 checks.

PR jobs must have no release/channel publication path, and must not bypass platform warnings. A build blocked on staging, signing, OS policy or installation is not a passing update test. Normal-network and representative-machine release checks below and in `docs/release/RELEASING.md` remain necessary for public release.

Converter tests must preserve existing read-only destinations on all platforms, including POSIX where writable parent directories otherwise permit atomic replacement. Deterministic coverage checks both an initially read-only file and one made read-only during conversion; real-FFmpeg integration must retain the same guarantee.

## Real-network tests

```bash
python scripts/run_network_tests.py
```

These tests are not part of ordinary deterministic runs. Run them before release from a normal network. The scheduled GitHub Actions run is useful evidence but advisory because YouTube and other services may challenge CI IP ranges. Downloader-pin pull requests also run the complete opt-in suite against their proposed dependency set in a read-only job and retain logs. A failed or challenged run must be investigated; it is never converted to a passing result. PR/manual runs do not create diagnostic issues; only scheduled failures update the rolling issue in a separate write-enabled job. A green CI run does not replace normal-network maintainer release acceptance.

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
- Local HTTP-server fixtures must stop their accept loop, close the server, and join active request handlers before interpreter teardown. Expected client disconnects during cancellation must not leak tracebacks after the test summary.
- New user-facing strings must preserve i18n coverage.
- New persisted state needs migration/failure-path tests.

## Manual QA

Manual checklists live under [`../qa/`](../qa/). They complement, not replace, automated tests. Record the product version/commit and platform when performing a checklist so the evidence does not masquerade as permanently current.
