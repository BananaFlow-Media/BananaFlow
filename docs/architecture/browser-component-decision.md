# Browser Component Decision — Playwright/Chromium Packaging

Date: 2026-07-17

Branch: `release-readiness/phase-06-playwright-chromium-performance`

Feeds: HUMAN GATE 5. No architecture change is made in this document — per
the plan, architecture changes require test coverage, a reliability
comparison, a package-size comparison and owner approval, none of which
exist yet for any option except the status quo (which already has years of
production use across Phases 0-5). This document supplies the impact data
the owner needs to choose, per `docs/performance/PACKAGE_AND_RUNTIME_PROFILE.md`
(Section A).

## B. Feature dependency map

Every Playwright call site in the current codebase, found by tracing every
`playwright`/`chromium`/`headless` reference in `core/` and the docstring
contract already maintained in `utils/playwright_check.py`:

| Feature | Module (entry point) | Launch mode | yt-dlp can replace? | Official API can replace? | Plain HTTP client can replace? | DOM/JS execution needed? | Interactive browser needed? | Reliability | Legal/policy notes |
|---|---|---|---|---|---|---|---|---|---|
| Spotify track | `core/scraper.py` (~L379) | headless | No — yt-dlp has no Spotify extractor | Partial — Spotify's Web API exists but needs an OAuth app registration + client secret, not safely embeddable in an open-source downloader | No — the page's internal endpoints need a signed/rotating token only the web player's own JS produces | Yes | No | High — stable DOM | Playwright scrapes *metadata only*, then resolves to YouTube for the actual audio; same posture as Phases 1-2's existing scraping/ToS treatment, nothing new |
| Spotify album | `core/scraper.py` (~L394) | headless | No | Partial (same OAuth gap) | No | Yes | No | High | same |
| Spotify playlist | `core/scraper.py` (~L411) | headless | No | Partial | No | Yes | No | High | same |
| Spotify artist discography | `core/scraper.py` (~L479, `--disable-blink-features=AutomationControlled`) | headless | No | Partial | No | Yes | No | Medium — the anti-bot-detection launch arg implies Spotify actively pushes back here | same |
| YouTube channel tab discovery | `core/channel_tab_discoverer.py` | headless | **Partial** — yt-dlp can already list a channel's flat playlist, but does not expose *which tabs exist* (Videos/Shorts/Live/Playlists/Releases/Podcasts); a per-tab-suffix yt-dlp probe could approximate this | No — needs a Google Cloud API key + quota for the official Data API | No | Yes | No | Real-measured: HTTP probe ~6-7 s; Playwright fallback ~35 s and its DOM selectors are currently stale (see Section E) — the module's original "~2-3 s" docstring claim was wrong for both paths and has been corrected | none new |
| Cookie Wizard (login/cookie capture) | `core/cookie_wizard.py` | **headed**, persistent profile | No — this is interactive login, not extraction | No | No | Yes — a real login UI, including 2FA | **Yes — fundamentally interactive by design** | High — a real, current Chromium is what makes Google/Spotify's login flows work at all | None new; the code already isolates this from the user's real Chrome profile by design (own persistent profile under app AppData, never DPAPI-protected Chrome cookies) |
| Generic stream interception (universal extractor) | `core/universal_extractor.py` | headless (async) | No — this *is* yt-dlp's own fallback, only reached when yt-dlp's extractor fails | No — no generic official API exists for arbitrary sites | No | Yes — must execute page JS to trigger the real HLS/DASH/media request | No | Medium — by definition the fallback-of-last-resort, so reliability is site-dependent | none new |
| Generic paginated listing scraper | `core/listing_scraper.py` | headless | No — same rationale, arbitrary sites | No | No | Yes | No | Medium | none new |
| Screenshot/debug uses | — | — | n/a | n/a | n/a | n/a | n/a | n/a | **Not used anywhere in the current codebase** (`grep` for `.screenshot(` across all `.py` files returns zero matches) — nothing to preserve or replace here |

Confirms the plan's own framing: browser cookies and login both route through
Cookie Wizard only; JavaScript-page execution is needed by 7 of the 8 mapped
items; only YouTube channel tab discovery has a plausible non-browser
replacement path today.

## C. Architecture options, compared against the real measured impact

Baseline figures are from `PACKAGE_AND_RUNTIME_PROFILE.md`: bundled Chromium
is 721,898,219 bytes (49.6% of the 1,454 MB `_internal` payload); a headless
session costs ~184-549 MB / 4 processes; Cookie Wizard's headed session costs
~650 MB / 9 processes; none of this is resident at idle (242 MB / 1 process).

### Option 1 — Keep bundled Chromium (status quo)

* **Package size**: no change (installer stays ~430 MB, portable ZIP ~666 MB).
* **Spotify / channels / generic sites**: full functionality, unchanged, for
  100% of users, offline-capable immediately after install.
* **Cookie Wizard**: unchanged — a pinned, known-good Chromium build is
  exactly what its isolation design already assumes.
* **Support burden**: unchanged from today; the existing obligation (already
  surfaced in Phase 5, `MICROSOFT_STORE_FEASIBILITY.md`) to keep the bundled
  Chromium revision current is the only ongoing cost, and it is independent
  of which packaging option is chosen — Options 2-5 do not remove it, since
  Cookie Wizard's headed browser still needs *some* pinned Chromium in every
  option except Option 2.

### Option 2 — Installed Edge/Chrome first, bundled fallback

* **Package size**: potentially much smaller if the bundled Chromium fallback
  is dropped entirely; if a fallback is kept for users without a usable Edge,
  the size saving only applies to the subset of users who never hit the
  fallback path. No rebuild without Chromium exists yet, so an exact new
  installer size cannot be claimed — only that removing 688 MB of largely
  incompressible browser binaries would meaningfully shrink both the
  portable ZIP and the installer.
* **Availability**: Windows 10/11 ship Edge (Chromium-based) by default, so
  most Windows users have *a* usable browser already — but not all: Edge can
  be removed, policy-disabled, or version-pinned on managed/enterprise
  machines, which is exactly the population the plan's own bullet
  ("enterprise restrictions") flags.
* **Cookie risk**: avoidable but not automatic. Playwright can launch a real
  installed Edge via `channel="msedge"` with an explicit, isolated
  `user_data_dir` — this does **not** touch the user's real Edge
  profile/cookies, mirroring the isolation Cookie Wizard's own docstring
  already commits to for Chromium. The risk is an implementation-discipline
  risk (getting the isolated-profile launch right everywhere, always), not
  an inherent property of the option.
* **Reliability**: real regression risk specifically for Cookie Wizard —
  Google/Spotify login pages are the most anti-automation-sensitive pages in
  the whole feature map, and an arbitrary user's already-fingerprinted,
  policy-modified Edge install (extra extensions, enterprise policy flags) is
  a less controlled surface than a single pinned Chromium revision tested
  once per release. The five metadata/extraction call sites are lower-risk
  here since they don't need to defeat anti-bot detection to the same
  degree.
* **Version variance**: the app currently pins an exact, tested Chromium
  revision (`chromium-1228`); an installed Edge's version is whatever
  Microsoft shipped that user that week — DOM-dependent scrapers (Spotify,
  channel discovery, generic listing) would be running against an untested
  surface per user.

### Option 3 — Optional Browser Pack (download on first use)

* **Package size**: smallest *initial* install of the browser-affecting
  options — Chromium moves from "bundled" to "downloaded on demand."
* **Integrity**: lower new-engineering cost than it first appears — Microsoft
  already operates `playwright install chromium`'s own HTTPS download +
  integrity-checked CDN; BananaFlow would be invoking that existing mechanism,
  not building a new one from scratch (`scripts/install_playwright.ps1`
  already does exactly this for source installs today).
* **UX complexity**: real new work — none of the 6 affected UI flows (4
  Spotify content types, channel scraping, Cookie Wizard, generic sites) has
  a "downloading browser component" state today; each needs new
  download/progress/failure/retry UI.
* **Offline regression**: real regression — today, a fresh offline install
  can use every feature immediately. This option makes first use of 6 of the
  8 mapped call sites require a live network connection and a ~270-690 MB
  download (headless-only vs. full-Chromium, respectively) before they work
  even once.

### Option 4 — Partial HTTP replacements (keep Playwright as fallback)

* **Best-fit candidate from the map**: YouTube channel tab discovery is the
  one call site with a plausible non-Playwright path — probing yt-dlp/HTTP
  against each tab's URL suffix (`/videos`, `/shorts`, `/streams`,
  `/playlists`, `/releases`, `/podcasts`, matching `channel_tab_discoverer.py`'s
  own `_TAB_MAP`) and treating a non-empty result as "tab exists." It doesn't
  need real page-JS execution the way Spotify's client-rendered pages or the
  generic-site interceptors do.
* **No feature loss**: by construction — Playwright stays as the fallback for
  every call site, so nothing currently working stops working.
* **Package size**: **does not move the needle by itself** — Cookie Wizard
  and the four Spotify call sites still need bundled Chromium regardless, so
  the 688 MB Chromium payload is unaffected unless combined with another
  option. This is a reliability/speed optimization for one call site, not a
  package-size lever.
* **What's still missing before this could ship**: a real side-by-side
  reliability comparison (this document's Section A measured *resource cost*,
  not extraction *correctness*, for the HTTP-probe alternative — that
  comparison does not exist yet) and test coverage for the new probe path.

### Option 5 — Separate Full and Lite editions

* **Lite (no browser)**: 6 of the 8 mapped feature call sites (all 4 Spotify
  content types, Cookie Wizard, generic-site interception/listing) would be
  absent or degraded — a substantial capability gap, not a small trim.
  Only plain yt-dlp-supported downloads and the partial YouTube-channel HTTP
  probe from Option 4 would remain fully intact.
* **Full**: identical to Option 1.
* **Maintenance and support burden**: doubles the release matrix — two
  installers × the existing 3-Python-version Windows CI matrix × the
  existing macOS build, doubled documentation, doubled support-triage
  surface ("which edition are you running?"). `RELEASE_STRATEGY.md` (Phase
  1) does not currently take a position on multi-edition releases one way or
  the other — this would be a new commitment, not a continuation of an
  existing one.

## D. Recommendation

Recommend **Option 1 (keep bundled Chromium) as the shipping baseline**,
combined with pursuing **Option 4 (YouTube-channel HTTP probe) as a
non-exclusive, low-risk follow-up** that does not require giving up any of
Option 1's guarantees:

* Six of the eight mapped feature call sites genuinely need DOM/JS execution
  with no official-API or plain-HTTP substitute (the four Spotify content
  types plus the two generic-site fallbacks) — Options 2/3/5 all reduce
  package size precisely by weakening or removing capability or reliability
  for at least a meaningful subset of users, and none of them is free:
  Option 2 trades size for untested-version risk exactly where anti-bot
  detection matters most (Cookie Wizard); Option 3 trades size for a new
  offline-first-run regression across 6 of 8 call sites; Option 5 trades
  size for a doubled release/support matrix and a Lite edition missing most
  of the app's differentiated functionality.
* Package size (49.6% of `_internal`) is a real, measured cost, but it is
  **not** a runtime-performance cost: startup time and idle RAM are
  unaffected by Chromium's presence (Section A.3-A.4), and every
  Playwright-driven RAM/process cost is transient and released on completion
  (Section A.5). The actual pain this phase's "package size and performance"
  mandate is meant to address is disk/download footprint, not sluggishness —
  and Option 1 has no sluggishness problem to fix.
* Option 4 is compatible with Option 1 (or any other option) and directly
  uses this phase's own feature map (Section B) to target the one call site
  where a substitute is plausible, without touching Cookie Wizard or the
  four Spotify call sites that most need a real browser.
* This mirrors the pattern already approved at HUMAN GATE 4: where an
  alternative was investigated (Nuitka) and failed to clear a real technical
  bar, the owner chose to remain with the proven status quo rather than
  adopt an unproven alternative for its own sake. The same logic applies
  here — no alternative in Options 2/3/5 has yet cleared the plan's own
  required bar (test coverage + reliability comparison + package-size
  comparison), while Option 1 has years of production use across every
  merged phase so far.

Explicitly **not recommended without further work first**: Options 2, 3 and
5 as full replacements for Option 1 — each is a legitimate future direction
if the owner wants to invest in it, but none has the reliability comparison,
test coverage, or (for Option 3) the new UX work built yet, all of which the
plan requires before any architecture change.

## HUMAN GATE 5 — Browser Packaging Decision

This is a substantive decision only the owner can make (rule 2 of this run's
standing instructions) — the plan requires stopping here, not deferring it
to the end-of-run manual pass.

### Owner answers (2026-07-17)

1. **Browser packaging architecture**: keep bundled Chromium (Option 1),
   the recommended baseline — no change to what ships; Spotify, Cookie
   Wizard and the generic-site fallbacks continue to work exactly as
   before, for 100% of users, offline-capable.
2. **Option 4 (HTTP-probe follow-up for YouTube channel tab discovery)**:
   approved, to be pursued as follow-up work in this same phase (not
   deferred) — implemented below.

### Option 4 implementation

`core/channel_tab_discoverer.py`'s `discover_tabs()` now tries
`_probe_tabs_via_ytdlp()` first — an HTTP/yt-dlp-only probe (no Playwright,
no Chromium launch) that requests each known tab-suffix URL
(`_TAB_MAP`'s `/videos`, `/shorts`, `/streams`, `/playlists`, `/releases`,
`/podcasts`) with `extract_flat` and a 1-item cap, and treats a successful
extraction as "tab exists." It falls back to the original
`_discover_tabs_via_playwright()` (kept verbatim, unchanged) only if the
base channel itself doesn't resolve via yt-dlp, or if every single tab
probe fails despite the base resolving. The public `discover_tabs(url)`
contract (return type, error semantics) is unchanged, so both UI call sites
(`ui/dialogs/tab_select_dialog.py`, `ui/workers/channel_scrape_worker.py`)
needed no changes.

**Real-data verification** (not simulated): ran both the new probe and the
existing Playwright path against two real public channels
(`@YouTube`, `@lexfridman`):

| Channel | HTTP probe (this change) | Playwright DOM (existing code, unchanged) |
|---|---|---|
| `@YouTube` | 6.5 s — videos, shorts, streams, playlists, podcasts; channel name "YouTube" | 36.2 s — videos, shorts, playlists only (defaults); channel name "Unknown Channel" |
| `@lexfridman` | 5.8 s — videos, shorts, playlists, podcasts; channel name "Lex Fridman" | 34.6 s — videos, shorts, playlists only (defaults); channel name "Unknown Channel" |

**Important finding, not introduced by this change**: the existing
Playwright DOM selectors (`yt-tab-group-component yt-tab-renderer`,
`yt-tab-shape`, and the channel-name selectors) no longer match YouTube's
current DOM at all — every live-page check returned zero matches for five
different selector variants tried. This means the Playwright fallback path
was **already silently broken in production** before this phase started:
it logs `"No tabs found via DOM — using defaults"` and returns a
hardcoded 3-tab guess (`videos`/`shorts`/`playlists`) plus `"Unknown
Channel"` instead of erroring — a pre-existing latent bug, unrelated to
Phase 6, that this measurement happened to surface because it required
running the Playwright path for a real side-by-side comparison. It is
**not fixed here** (reverse-engineering YouTube's current custom-element
structure is a separate, open-ended task, not what HUMAN GATE 5 approved),
but is flagged as a recommended follow-up bug fix. Practical effect: the
new HTTP probe is not merely a package-size optimization — it is currently
the *only* correct tab-discovery path in production; the Playwright
fallback still protects against total failure (a real error is better than
silently wrong tabs) but currently cannot recover correct-and-complete tab
data on its own.

**Test coverage**: `tests/test_channel_tab_discoverer_phase6.py` (9 tests,
all passing) — mocked `yt_dlp.YoutubeDL` (matching this suite's existing
convention), covering: partial tab resolution, `live`/`streams`
deduplication, an unreachable base channel, a falsy `extract_info` result,
every-tab-fails-despite-base-resolving, channel-name fallback chain, and
the `discover_tabs` dispatch itself (probe success never touches
Playwright; probe failure falls back to it with the tab suffix already
stripped).

No production code beyond this one call site changed. Cookie Wizard and
the four Spotify call sites are untouched, per Option 1's confirmation
above.

### Independent review and correction

A separate read-only `claude-opus-4-8` context reviewed this section's diff
(scoped to `core/channel_tab_discoverer.py`, the new test file, and this
document). Verdict: **PASS WITH NOTES**. The one Major finding worth
recording: the review correctly identified that `_probe_tabs_via_ytdlp`'s
core assumption — a non-raising `extract_info` call means "tab exists" —
was not proven, only assumed, and asked whether YouTube might silently
redirect a nonexistent tab to an existing one instead of erroring (which
would make the probe over-report tabs), and separately noted the mocked
tests encoded that same unproven assumption rather than testing against it.

This was resolved with real data, not by further reasoning: `extract_info`
was run directly against three real, verified-absent tabs
(`@lexfridman/streams`, `@lexfridman/releases`, `@YouTube/releases`). All
three **raised** `DownloadError` with the exact text `"[youtube:tab]
<channel>: This channel does not have a <tab> tab"` — yt-dlp's
`youtube:tab` extractor does not silently redirect a missing tab to an
existing one. The finding is closed on real evidence, not assumption; the
9 tests were updated to use this exact verified error-message format
instead of a placeholder. A second, lower-severity finding (the
`live`/`streams` dedup could probe the same URL twice if the first attempt
failed) was also fixed: `seen_types` is now marked before the network call,
not after a successful one, so a failed shared-URL probe isn't retried
under the alias key. The docstring's stale "~2-3 seconds" timing claim
(flagged as self-contradicting the very numbers this phase measured) was
corrected in both the code and this document (Section B, above). One
Informational note was accepted without change: `releases`/`podcasts` tabs
returned by the probe are collection-like, and the existing
`ChannelScrapeWorker._scrape_flat_tab` (unchanged, pre-existing code) may
scrape them to 0 items rather than crash — a pre-existing worker
limitation newly exposed by the probe finding more real tabs than the
broken Playwright defaults ever did, logged as a recommended follow-up,
not a Phase 6 blocker.
