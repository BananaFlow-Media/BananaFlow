# Secure Component Updater — Design and Current-State Audit

Date: 2026-07-17

Branch: `release-readiness/phase-07-component-updates`

Per the plan's explicit instruction, this document starts with what already
exists — a real, non-trivial update-checking system is already shipped —
before designing what a fully secure, independent *component* updater would
still need to add.

## Current-state audit (what exists today)

### What is checked automatically

* **App version**: `core/update_checker.py` queries the GitHub Releases API
  (`GET /repos/{owner}/{repo}/releases/latest`, or the last 10 releases when
  pre-releases are included) once per launch, gated by the
  "Check for Updates on Launch" setting (default on,
  `config.py:84,406-411`). Comparison uses a hand-rolled SemVer tuple parse,
  8-second timeout, and never raises — failures resolve to a silent
  `status="error"` on the startup path (only the manual Settings-panel
  button surfaces a failure message).
* **yt-dlp and yt-dlp-ejs versions**: `core/component_updates.py`'s
  `ComponentUpdateChecker` queries PyPI's public JSON API
  (`https://pypi.org/pypi/<name>/json`) for both packages, comparing against
  the installed version via `importlib.metadata` (with a
  `yt_dlp.version.__version__` fallback for frozen builds).
* Both checks run from `ui/workers/update_worker.py` on a background
  `QThread`, once per launch; results are filtered through
  `core/update_state.py`'s per-exact-version dismiss/snooze store
  (`<app-data>/update_state.json`) before `ui/dialogs/update_prompt_dialog.py`
  shows anything. A manual "Check Now" path exists in the Settings panel for
  both app and component checks.

### What is NOT checked automatically

* **The PO Token provider** (`bgutil-ytdlp-pot-provider`) — not in
  `ComponentUpdateChecker.MONITORED_COMPONENTS`. Its version is hard-coded
  in two separate places that nothing cross-checks against each other:
  `pyproject.toml:60` (`bgutil-ytdlp-pot-provider==1.3.1`, the source-install
  optional dependency) and `packaging/stage_pot_provider.py:45`
  (`PROVIDER_VERSION = "1.3.1"`, what the packaged Windows build actually
  stages). Several tests hard-code the string `"1.3.1"` as an expected
  fixture value (`tests/test_license_compliance.py:148`,
  `tests/test_runtime_components.py:56,167`,
  `tests/test_stage_pot_provider.py` throughout), but none of them assert
  the two source-of-truth constants equal *each other* — verified directly:
  no such test exists. A future version bump to only one of the two
  locations would go undetected by CI.
* **Deno** — pinned once at build time
  (`scripts/fetch_deno_runtime.ps1`, `param([string]$Version = '2.9.1')`),
  with its own SHA-256 verification against a checksum fetched from the
  *same* GitHub release (no independent second source — already flagged as
  a recorded supply-chain caveat
  finding F-013). Not a PyPI package, so it cannot be monitored the way
  yt-dlp/yt-dlp-ejs are; no runtime staleness check exists. The bundled
  Deno's *own* upstream self-update-check is explicitly disabled
  (`DENO_NO_UPDATE_CHECK=1`, `DENO_NO_PROMPT=1`,
  `core/runtime_components.py:368-369`).
* **Chromium/Playwright** — Playwright's version is pinned in
  `constraints-windows-py312.txt` (`playwright==1.61.0`), which *indirectly*
  pins the Chromium revision Playwright's own installer resolves
  (`chromium-1228`, per the project's supply-chain review).
  Neither Playwright nor Chromium is monitored for staleness anywhere at
  runtime.

### Source install vs. frozen EXE

* `core/component_updates.can_update_in_place()` is
  `not utils.paths.is_frozen()` — **True only for source/venv installs**.
  There, the update dialog offers an "Update Components" button that runs
  `pip install --upgrade "yt-dlp[default]"` (deliberately through the
  `[default]` extra so the matched `yt-dlp-ejs` pin rides along —
  `core/component_updates.py`'s own module docstring explains this is to
  avoid producing a mismatched yt-dlp/yt-dlp-ejs pair).
* In a **frozen EXE**, that button is replaced with messaging explaining
  the components are baked into the executable and directing the user to a
  full app update instead (`ui/dialogs/update_prompt_dialog.py:20-29,
  233-237`). Playwright/Chromium have no update path at all in either
  install mode short of a full reinstall or manually re-running
  `scripts/install_playwright.ps1` (source only).

### What happens during an app update, today

There is no in-app app-updater yet (see README.md's "Update System" section
— a fully-specified but explicitly not-yet-built download → verify →
install → relaunch flow). "Updating the app" today means: the user clicks
"Open Download Page," downloads a new full installer or portable ZIP from
GitHub Releases, and runs/extracts it themselves.

For the Windows installer specifically, `packaging/bananaflow.iss`'s `[Files]`
section stages the entire `dist/bananaflow/` one-folder PyInstaller output
(Chromium, FFmpeg, Deno, the PO provider backend, yt-dlp-ejs solver JS,
everything) with `Flags: ignoreversion recursesubdirs createallsubdirs`,
and there is **no `[InstallDelete]` section** (only `[UninstallDelete]`,
which fires on uninstall, not on upgrade-install). For the common case
(file paths unchanged between versions, only contents changed), every file
gets individually overwritten, so the bundled components effectively update
together. But this is not a proven, atomic property of the installer: if a
future version renames or drops a bundled sub-tree, Inno Setup's
overwrite-only semantics would leave the old sub-tree's files behind rather
than removing them, and there is no post-install health check confirming
the components (Deno, the PO provider, Chromium) actually work after the
overwrite completes. `RELEASE_STRATEGY.md`'s existing "Emergency releases"
section already states the resulting policy: a bundled-component-only
emergency fix currently **must** ship as a full application update, because
no independently-approved component updater exists yet.

## A. Secure updater design

A component updater that met every item below would need to look like
this. This is a **design**, not a commitment to build it now — see Section
B for the scope decision.

* **Manifest controlled by BananaFlow**: a single JSON manifest
  (`components.json`) hosted at a URL BananaFlow controls (e.g. a path under
  the project's own GitHub Pages or a release asset on the project's own
  GitHub Releases — not a third-party CDN), listing, per component
  (`yt-dlp`, `yt-dlp-ejs`, the PO provider backend, Deno, the Chromium
  revision): exact version, exact SHA-256, download URL, minimum/maximum
  compatible app version, and a `superseded_by`/`emergency_disable` flag.
* **Signed or authenticated manifest**: the manifest itself must be signed
  (e.g. minisign/Ed25519 with a key BananaFlow generates and never
  distributes) or served only over an authenticated channel the client can
  verify (e.g. a GitHub Release asset, whose download is already
  TLS-authenticated to `api.github.com`/`github.com` and whose contents can
  additionally be checked against a signature asset published alongside
  it). **Blocking dependency, not yet available**: this project has no
  signing-key infrastructure today — HUMAN GATE 4 (Phase 5) explicitly
  deferred both SignPath enrollment and a paid code-signing certificate
  until after the first public Beta ships. A manifest-signing key is a
  separate, smaller piece of infrastructure than binary code-signing, but
  building and safely storing a new private key (and its GitHub Actions
  secret) is exactly the kind of new security-sensitive surface
  `RELEASE_STRATEGY.md`'s feature freeze was written to defer.
* **Exact compatible versions / compatibility matrix**: the manifest
  entry's `min_app_version`/`max_app_version` fields, checked against
  `version.__version__` before offering an update; an incompatible
  component update is never offered, only reported.
* **Exact hashes**: SHA-256 per artifact, verified before any file is
  moved into place; this single item alone would already close the F-013
  gap on the PO provider's currently-uncommitted-hash source download.
* **HTTPS**: all manifest and artifact fetches over HTTPS only, matching
  the existing `nocheckcertificate: True` yt-dlp option's *opposite*
  policy — an updater must never disable certificate checking.
* **Timeout**: a bounded connect/read timeout (the existing
  `UpdateChecker`'s 8s precedent is a reasonable floor for the manifest
  fetch; artifact downloads need a longer, size-proportional timeout, not
  a fixed one).
* **Download size**: the manifest declares an expected byte size per
  artifact; the updater aborts if the actual download exceeds a declared
  cap (protects against a compromised or misconfigured host serving an
  oversized payload).
* **AppData overlay**: never write into the installed program directory in
  place. Downloaded components land in a new versioned subdirectory under
  the app's own AppData tree (alongside the existing `browser_profile`,
  `logs`, `update_state.json`), and the app is pointed at that overlay
  directory at startup (the same pattern `PLAYWRIGHT_BROWSERS_PATH`
  already uses to point at a directory outside the installed tree) rather
  than at the frozen build's bundled copy.
* **Atomic install**: download to a temp file in the overlay directory,
  verify its hash, then rename into place — never overwrite a
  currently-in-use component file directly (mirrors the resolve→replace
  pattern already used by the Converter, per Phase 3's hardening work).
* **Previous version retention**: keep the last-known-good overlay
  alongside the new one (at minimum one prior version per component) until
  the new version passes its health check.
* **Rollback**: on health-check failure, or on user request, revert the
  active-overlay pointer back to the retained previous version — no
  re-download required.
* **Health check**: after install, run a minimal real invocation of the
  updated component before marking it active (e.g. `deno --version`,
  a yt-dlp `--simulate` info extraction, a provider-backend
  ping) — a component that fails its health check never becomes the
  active one.
* **No update during active download**: an update-in-progress lock
  (already a natural fit for `core/update_state.py`'s existing JSON
  store) that the main download pipeline checks before starting a new
  video/audio download, and vice versa, so a component swap never happens
  mid-use.
* **No update during app shutdown**: the updater must not start a new
  install once the app has begun its shutdown sequence; an in-progress
  install should be allowed to finish or cleanly abort before the process
  exits, never left half-applied.
* **User opt-in**: component auto-updating is off by default, matching the
  existing "Check for Updates on Launch" pattern but as a distinct,
  separately-labeled setting — checking for a component update is not the
  same consent as auto-installing one.
* **Manual update button**: already exists today for yt-dlp/yt-dlp-ejs in
  source installs (`ui/workers/component_install_worker.py`); a secure
  updater would extend the same button to the PO provider, Deno and
  Chromium, and — critically — make it available in the **frozen EXE**
  too, which is the actual gap.
* **Automatic update setting**: a separate opt-in toggle for
  "automatically install verified component updates," independent of
  "check for updates."
* **Update logs without secrets**: reuse the existing logging
  infrastructure's redaction (`utils/security.redact_text`, already used
  elsewhere for cookies/tokens) for every line the updater writes; the
  manifest and artifacts contain no secrets in the first place, so this is
  mostly a discipline requirement on log messages (URLs, paths, hashes —
  no auth tokens, since none should be needed to fetch public release
  assets).
* **Emergency disable switch**: a manifest-level `emergency_disable: true`
  flag (or per-component flag) the client checks before using any
  already-downloaded overlay component, in addition to before downloading
  a new one — lets BananaFlow remotely disable a component found to be
  compromised or broken *after* it has already reached some users'
  machines, without requiring a full app update.
* **Offline behavior**: identical to today's `UpdateChecker`/
  `ComponentUpdateChecker` failure handling — a failed manifest fetch is
  silent on the startup path and reported only on manual "Check Now,"
  and the app continues running on whatever component versions are
  currently active (bundled or a previously-installed overlay).
* **Corrupted download handling**: hash mismatch after download → discard
  the temp file, do not touch the active component, surface a clear
  "update failed, still on version X" state (never a partial file in a
  live path).
* **Partial update handling**: if the process is killed mid-download or
  mid-verify, the temp file/directory is orphaned in a location the
  overlay logic recognizes and cleans up on next launch (parallel to how
  `utils/yt_dlp_opts.py`'s `temp_cookies_copy` already uses a
  try/finally-guarded temp file for a similar reason) — the active
  component pointer is only ever updated after a complete, verified,
  health-checked install.
* **Security incident handling**: if BananaFlow ever needs to respond to a
  compromised component (upstream CVE, supply-chain compromise, a bad
  bgutil/Deno release), the response path is: publish a manifest update
  setting `emergency_disable` for the affected component (near-immediate,
  no app release needed) and/or a corrected manifest entry once a fix is
  available; this is precisely the emergency-response capability
  `RELEASE_STRATEGY.md`'s "Emergency releases" section says does not exist
  today (a bundled-component fix currently requires a full app release).

## B. Implementation scope decision

**Not implemented in this phase.** A secure updater meeting every item
above cannot be built without risking the Beta, for concrete, present
reasons rather than general caution:

1. **No signing-key infrastructure exists.** The "signed or authenticated
   manifest" requirement is the design's load-bearing security property —
   without it, a compromised or spoofed manifest host could redirect users
   to an arbitrary payload for any pinned component, a strictly worse
   attack surface than today's status quo (no in-app component fetching
   at all beyond a `pip install` the user explicitly triggers in a source
   install they already control). Building and safely operating a new
   signing key is exactly the kind of new security-sensitive
   infrastructure that has not been created yet — HUMAN GATE 4 deferred
   even *binary* code-signing infrastructure until after the first public
   Beta, and a manifest-signing key would be a second, parallel piece of
   the same class of infrastructure, introduced before the first one is
   even in place.
2. **`RELEASE_STRATEGY.md`'s existing feature freeze already covers this
   exact case.** Its feature-freeze section explicitly bars new
   component-update mechanisms until after the first public Beta ships —
   this phase's own governing policy document already answered the "should
   we build this now" question before Phase 7 began.
3. **The plan explicitly forbids a half-secure version.** Any subset of
   the design above that skipped the signed manifest, the atomic overlay
   install, or the rollback/health-check pair would be exactly the
   "half-secure updater" the plan instructs not to build — there is no
   partial version of this design that is safe to ship.

Per the plan's instruction for this branch: no implementation, no
rollback/manifest/tampering tests, no packaged smoke test were added,
since none of that code exists to test. Instead:

* A precise post-Beta tracking issue is opened (see the Phase 7 report),
  containing this design document's checklist verbatim as the acceptance
  criteria for whenever the owner decides to build it.
* Current messaging is reviewed (see the Phase 7 report) to confirm users
  already understand, today, that a full BananaFlow update is how they get
  component fixes — not silently left to assume otherwise.
* No half-secure updater is built.

## HUMAN GATE 6 — Component Updater Scope

See the accompanying chat message and the Phase 7 report for the four
options, the recommendation, and the risk/time/compatibility/security
analysis behind it.
