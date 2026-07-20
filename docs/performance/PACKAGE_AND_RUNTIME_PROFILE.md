# BananaFlow — Package and Runtime Profile

Date: 2026-07-17

Branch: `release-readiness/phase-06-playwright-chromium-performance`

## Methodology and scope

All package-size numbers below are measured directly on the real, already-built
`dist/bananaflow-0.2.0-windows-*` artifacts checked into the working tree (built
2026-07-16, PyInstaller one-dir mode, `packaging/bananaflow.spec`,
`SHA256SUMS.txt` checksums `c17ab1a2…` / `125f0c27…`). No packaging-relevant
code changed between that build and the current `main` tip (Phases 4-5 only
added SBOM generation, Action pinning and a code-signing *design*, none of
which alter what gets bundled), so this build is a faithful stand-in for the
current architecture. Sizes are exact (`du -sb` on the real files), not
estimates.

Runtime numbers (startup time, RAM, process count, disk footprint) are
measured by actually launching this real packaged `bananaflow.exe` and the real
bundled Playwright Chromium binaries (`PLAYWRIGHT_BROWSERS_PATH` pointed at
`dist/bananaflow/_internal/ms-playwright`, the exact browser build that ships),
using process-level OS inspection (PowerShell `Get-Process` / `Get-CimInstance
Win32_Process`) rather than in-app instrumentation — no GUI clicking was
automated, consistent with the standing instruction to reserve interactive
click-driven testing for the owner's manual pass. Feature-specific flows that
need a real login or real account state (an actual Spotify/Google sign-in in
Cookie Wizard, a real multi-page channel scrape) are approximated by driving
the *same* Playwright launch pattern the app itself uses (headless vs.
headed-persistent, same launch args) against real public pages, since the
dominant resource cost is the browser process tree itself, not the specific
site. Exact per-feature RAM under a real signed-in session is left to the
owner's consolidated manual pass (`OWNER_VERIFICATION_CHECKLIST.md`) — this
document gives the architecturally-relevant order of magnitude.

## A.1 Package size

| Artifact | Size (exact bytes) | Size (MiB) |
|---|---:|---:|
| Portable folder (`dist/bananaflow/`) | 1,503,279,008 | 1,433.9 |
| Portable ZIP (`bananaflow-0.2.0-windows-portable.zip`) | 698,434,614 | 666.1 |
| Inno Setup installer (`bananaflow-0.2.0-windows-setup.exe`) | 451,431,218 | 430.5 |

The installer is ~65% smaller than the raw portable folder purely from Inno
Setup's LZMA compression — no content is dropped.

## A.2 Component breakdown (inside `dist/bananaflow/_internal/`, 1,454,375,921 bytes total)

| Component | Bytes | MiB | % of `_internal` |
|---|---:|---:|---:|
| Chromium (both variants, see below) | 721,898,219 | 688.4 | 49.6% |
| FFmpeg (yt-dlp postprocessing, shared libs + exe) | 201,678,336 | 192.3 | 13.9% |
| PySide6 / Qt (includes its own embedded FFmpeg, see note) | 119,186,101 | 113.7 | 8.2% |
| Playwright Python package (`playwright/`, driver + client) | 105,160,223 | 100.3 | 7.2% |
| Deno runtime (`runtime/deno.exe`, for `yt-dlp-ejs`/provider scripts) | 99,167,341 | 94.6 | 6.8% |
| bgutil PO Token provider backend | 84,272,804 | 80.4 | 5.8% |
| numpy + scipy native libs | 47,534,672 | 45.3 | 3.3% |
| Python runtime (interpreter, pywin32, stdlib zip) | 15,821,853 | 15.1 | 1.1% |
| Everything else (stdlib modules, misc small packages/DLLs) | 59,656,372 | 56.9 | 4.1% |

Chromium breakdown (`ms-playwright/`, 721,898,219 bytes):

| Sub-component | Bytes | MiB | Used by |
|---|---:|---:|---|
| `chromium-1228` (full `chrome.exe`, headed-capable) | 435,574,347 | 415.4 | Cookie Wizard only (`headless=False`, persistent context) |
| `chromium_headless_shell-1228` (`chrome-headless-shell.exe`) | 282,547,970 | 269.5 | Spotify scraping, channel tab discovery, generic listing, universal extractor (all `headless=True`) |
| Playwright-bundled `ffmpeg-1011` (screen-recording support, unused feature) | 3,517,342 | 3.4 | Not called by any BananaFlow code path (see Section B) |
| `winldd-1007` | 258,560 | 0.25 | Playwright's own DLL-dependency probe tool |

Notable duplication: PySide6 embeds its own separate copy of FFmpeg for Qt
Multimedia (`avcodec-61.dll` + 4 sibling DLLs, 18,762,264 bytes / 17.9 MiB) —
a different version (61.x) from the yt-dlp-facing FFmpeg (62.x, 201.7 MiB).
This is normal for a PySide6 wheel and not fixable without dropping Qt
Multimedia features; noted for completeness, not treated as a Phase 6 defect.

Total files inside the packaged app: 6,953 (mostly Chromium locale/resource
files and Python `.pyd`/`.dll` extension modules).

## A.3 Startup time

Measured end-to-end from process launch to the Qt main window handle
becoming valid (`MainWindowHandle != 0`), on the real packaged `bananaflow.exe`:

| Condition | Time to main window |
|---|---:|
| Cold (first launch this session, OS file cache cold for the 1.4 GiB tree) | 11.9 s |
| Warm (second launch, OS file cache warm) | 8.9 s |

Both runs are single-process at this point — Playwright/Chromium is not
touched at startup; it is imported lazily only when a Playwright-backed
feature actually runs (see `utils/playwright_check.py`). Startup cost is
dominated by PyInstaller one-dir cold-start (thousands of DLL/`.pyd` loads)
and Qt/PySide6 initialization, not by the bundled browser.

## A.4 Idle RAM and process count

| Condition | Processes | Total working set |
|---|---:|---:|
| Idle, no Playwright feature ever invoked | 1 | ~242 MB |

The idle app never spawns a child process — the 242 MB is the PySide6 GUI
process alone (Qt, numpy/scipy imports used by audio-tag/loudness code,
yt-dlp's own in-process state).

## A.5 RAM and process count while a Playwright feature is active

Each measurement launches the *exact* bundled Chromium binary the way the
corresponding production code launches it, holds the page open briefly, and
sums OS-level working set across every new process in that browser's tree
(main browser process + GPU + network service + renderer(s)):

| Scenario | Launch mode | New processes | Combined RAM |
|---|---|---:|---:|
| Generic light page (baseline floor) | headless (`chrome-headless-shell`) | 4 | 184 MB |
| YouTube channel page (channel tab discovery / channel scraping) | headless | 4 | 248 MB |
| Spotify track page (Spotify track/album/playlist/artist scraping) | headless | 4 | 549 MB |
| Cookie Wizard login page | headed, persistent context (`chrome.exe`) | 9 | 650 MB |

Interpretation: a headless Playwright session adds a **floor of roughly
180-250 MB and 4 OS processes** on top of the app's own 242 MB idle baseline
regardless of which of the five headless-only features triggers it; a
JavaScript-heavy page like Spotify's web player can push a single session to
~550 MB. Cookie Wizard's headed, persistent-profile session is the heaviest
single case (~650 MB, 9 processes) because a full `chrome.exe` launches more
subprocesses (GPU, audio service, extra utility processes) than the
lightweight headless shell. These are per-session, transient costs: the
processes and the RAM are released when the Playwright browser/context
closes, which every current call site does (`with sync_playwright()`,
explicit `browser.close()` / `context.close()`).

## A.6 Disk writes

No feature writes to the portable app folder itself. Disk writes are
confined to the OS-standard per-user app-data locations already established
in earlier phases:

* Cookie Wizard's persistent Chromium profile
  (`utils.paths.get_app_browser_profile_dir()`, under the app's own AppData
  directory) accumulated **~16 MB** after a single ~10-second headed session
  (browser cache, code cache, local storage, no login actually completed) in
  this measurement. This grows slowly and boundedly with repeated use
  (Chromium's own disk-cache eviction applies); no real profile currently
  exists on this machine (Cookie Wizard has not yet been used for a real
  sign-in), so this is a lower-bound single-session figure, not a
  steady-state one.
* The five headless-only call sites use non-persistent, in-memory browser
  contexts (`chromium.launch()` with no `user_data_dir`) — Playwright cleans
  up their temporary profile directories itself on `browser.close()`, so they
  leave no residue after the process exits.
* No feature writes to the Windows registry, and no feature writes outside
  the app's own AppData tree.

## Summary for the Phase 6 architecture decision

* Chromium (both browser variants combined) is **49.6% of the installed
  footprint** (688 MB of 1,454 MB) and the single largest lever for package
  size.
* Playwright's own Python package (105 MB) and its bundled DLL-dependency
  probe tool are fixed costs of shipping *any* Chromium at all, not
  independently removable.
* The unused, Playwright-bundled screen-recording `ffmpeg-1011` (3.4 MB)
  inside `ms-playwright/` is dead weight — BananaFlow has its own separate,
  fully-featured FFmpeg (192 MB) for all real media work — but it is too
  small to matter for the package-size decision at Section D. It is
  recorded here rather than silently dropped, so it is available if the
  owner ever wants to trim it via a build-time file-exclusion rule.
* Startup time and idle RAM are unaffected by Chromium's presence — the cost
  is entirely deferred until a Playwright feature actually runs, and it is
  released again when that feature finishes.
* Every headless feature adds a comparable ~180-550 MB / 4-process
  transient cost; Cookie Wizard's headed session is the heaviest single
  case at ~650 MB / 9 processes. None of this is resident during idle use.

These numbers feed directly into `docs/architecture/browser-component-decision.md`
(Sections B-D) and HUMAN GATE 5.
