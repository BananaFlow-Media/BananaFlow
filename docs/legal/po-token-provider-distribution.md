# PO Token Provider Distribution Decision

Date: 2026-07-17

Branch: `release-readiness/phase-08-bgutil-potoken-gpl-distribution`

This document is a best-effort release-compliance and policy analysis, not
legal advice. It does not conclude that anything described here is
"legal" or "illegal," and it does not claim this project or repository is
"safe from DMCA" — those determinations are for a qualified professional
and, ultimately, for courts and platform operators, not for this document.

## Technical audit (what already exists — no hidden packaging or build steps)

* **bgutil license**: `bgutil-ytdlp-pot-provider` is **GPL v3**, verified
  against its PyPI package classifier and the live PyPI JSON API
  (`THIRD_PARTY_NOTICES.md` lines 61-97, corrected there from an earlier
  draft that incorrectly said MIT). BananaFlow itself is
  GPL-3.0-or-later, so bundling it is license-compatible.
* **Provider plugin / backend license**: same GPL v3 covers both the
  Python `yt-dlp-plugins` entry point and the upstream `server/` (Deno/
  TypeScript) backend staged alongside it — confirmed by reading the
  actual staged source tree
  (`dist/bananaflow/_internal/pot-provider-backend/bgutil-ytdlp-pot-provider/server/`).
* **Source availability**: `SOURCE_OFFER.md` already directs users to the
  upstream repository (`https://github.com/Brainicism/bgutil-ytdlp-pot-provider`)
  as corresponding source, consistent with GPL's requirement that
  corresponding source be made available — this project does not modify
  bgutil's source, only stages an unmodified copy at build time, so
  pointing at the unmodified upstream release satisfies the obligation.
* **Pinned version**: `1.3.1`, declared in two places —
  `pyproject.toml`'s `po-token` optional-dependency group and
  `packaging/stage_pot_provider.py`'s `PROVIDER_VERSION` constant. Phase 7's
  audit already found nothing cross-checks these two constants against
  each other (a real, separately-tracked gap, not re-litigated here — see
  `docs/architecture/secure-component-updater.md`).
* **Checksums**: a recorded release-baseline finding
  F-013 (already on record, not new) — the provider's source ZIP is
  downloaded from its GitHub tag archive with **no committed hash**;
  cached ZIPs are trusted. This is a real supply-chain gap, independent of
  the distribution-strategy question this phase answers.
* **Included files / corresponding source**: the staged tree includes the
  plugin's two Python files
  (`getpot_bgutil.py`/`getpot_bgutil_script.py`), the upstream `server/`
  directory (TypeScript source plus its `npm ci --omit=dev`-installed
  `node_modules`), matching `package.json`/`package-lock.json`/`deno.lock`
  manifests preserved for license/source review
  (`THIRD_PARTY_NOTICES.md` lines 89-97).
* **Build scripts**: `packaging/stage_pot_provider.py` is a normal,
  readable, committed Python script — nothing about how the provider is
  staged is hidden or obfuscated. It downloads the upstream source
  archive, extracts `server/`, and runs `npm ci` against a Deno-managed
  cache; this is the same script visible in this repository's own history.
* **Release workflow**: `.github/workflows/release-windows.yml` invokes
  the staging script as a normal, visible CI step (already SHA-pinned per
  Phase 4's Action-pinning work); nothing about the release pipeline
  itself is undisclosed.
* **SBOM**: Phase 4 added a CycloneDX SBOM
  (`scripts/generate_sbom.py`) wired into the Windows build; the provider
  package appears in it like any other bundled component.
* **Doctor behavior**: `core/youtube_doctor.py` implements a genuinely
  granular health-check state machine for the provider (missing plugin →
  missing JS runtime → backend incomplete → health-check failed → module
  not detected → fully ready), each with a distinct, honest user-facing
  message — verified by reading the module directly, not assumed.
* **Actual token handling**: confirmed by reading both the module
  docstrings and the actual staged backend source: BananaFlow **never**
  generates, scrapes, stores, or injects a PO Token itself. It configures
  yt-dlp with the provider's own official `server_home` extractor
  argument (`utils/yt_dlp_opts.py`); the provider's Deno *script*
  (not a persistent listening server — `core/runtime_components.py`'s own
  docstring: "does not open a port or manage a long-running provider
  process") is invoked by yt-dlp on demand, only when a specific YouTube
  request actually needs a token.
* **Logs**: `utils/security.py`'s existing redaction patterns already
  match generic `token`-named fields; nothing in this project's own code
  logs a PO Token value. (What the upstream bgutil process itself logs to
  its own stdout/stderr, if invoked verbosely, is upstream's concern, not
  this project's code — not separately audited here since it is out of
  this project's control surface.)
* **Local health check**: YouTube Doctor's provider checks run entirely
  locally (module presence + a local Deno script invocation) — no network
  call is required merely to *check* readiness, only to actually *use* a
  token during a real extraction.
* **Network behavior — the one substantive new finding this phase makes**:
  reading the actual staged backend source
  (`server/src/session_manager.ts`) directly, the provider's BotGuard
  attestation flow makes real network calls to
  `https://www.youtube.com/youtubei/v1/att/get` and, critically,
  **fetches a JavaScript "interpreter" script from a URL that YouTube's
  own server supplies as part of the challenge response, then executes
  it via `new Function(interpreterJavascript)()`**
  (`session_manager.ts` lines 267-307, quoted verbatim from the staged
  build). This is Google's own official BotGuard VM interpreter — the
  same client-side code a real Chrome browser fetches and runs when it
  loads youtube.com — not third-party or attacker-controlled code, and
  it is fetched directly from YouTube's own servers, not a bgutil-operated
  or unrelated host. But stated plainly and without spin: **yes, at
  runtime, the bundled provider does download a script from a remote
  server and execute it** — this is the definitive, directly-verified
  answer Phase 5's `MICROSOFT_STORE_FEASIBILITY.md` (§10.2.2) asked this
  phase to provide, and it is a real consideration for that specific
  distribution channel (Microsoft Store investigation is separately
  paused per HUMAN GATE 4, so this does not block anything today, but the
  fact is now confirmed rather than left as an open question).
* **YouTube/yt-dlp documentation**: yt-dlp's own official wiki
  (`PO-Token-Guide`, fetched directly for this phase) documents PO Token
  Provider plugins as the **recommended** approach, explicitly names
  `bgutil-ytdlp-pot-provider`, and states it is "maintained by a yt-dlp
  maintainer" — i.e., this is yt-dlp's own documented, endorsed
  integration path, not an unofficial or unsanctioned hack layered on top
  of yt-dlp by this project.

## A. Legal and policy research

Sources fetched directly for this section (current as of 2026-07-17):
yt-dlp's official PO Token Guide wiki page, the
`bgutil-ytdlp-pot-provider` project's own README, YouTube's Terms of
Service, and GitHub's DMCA takedown policy page.

### yt-dlp's PO Token framework

yt-dlp's own wiki documents Proof-of-Origin Tokens as YouTube's mechanism
to verify a request originates from a genuine client, states that
"manually extracting PO Tokens is no longer recommended" because tokens
are now bound per-video, and recommends installing a **PO Token Provider
plugin** instead — naming `bgutil-ytdlp-pot-provider` specifically and
noting it is maintained by a yt-dlp core maintainer. yt-dlp itself
provides the extension points (`po_token` extractor argument, the
provider-plugin framework) that make this integration possible; this
project uses only that officially documented mechanism.

### bgutil's own stated purpose

bgutil's README states its purpose directly: generating tokens to help
yt-dlp users get past YouTube's "Sign in to confirm you're not a bot"
bot-detection challenge, using LuanRT's BotGuard-interfacing library. It
makes **no claim of official partnership with Google, YouTube, or
yt-dlp**. It carries a cautionary note that providing a token "does not
guarantee bypassing 403 errors or bot checks" — it improves reliability,
it does not defeat every anti-abuse measure. It states no legal
conclusion of its own about permissibility; this project does the same.

### YouTube's Terms of Service

The relevant clauses, read directly from YouTube's current Terms of
Service: automated access ("robots, botnets, or scrapers") is restricted
except via public search engines respecting `robots.txt`, with YouTube's
written permission, or as otherwise legally permitted; the terms also
prohibit attempting to "circumvent, disable, fraudulently engage with, or
otherwise interfere with" security features and content-protection
functions, including those that prevent copying or restrict use of the
service; and content may be viewed for personal, non-commercial use but
not downloaded, redistributed, or transmitted outside approved methods
without written permission or legal authorization.

**A genuine, unresolved interpretive tension, stated factually rather
than resolved**: this project's own `docs/legal/acceptable-use.md`
already tells users not to use BananaFlow to "evade access controls,
paywalls, geographic restrictions, or technical protection measures." A
PO Token is an anti-bot/anti-abuse attestation about the *client*, not a
content-encryption or DRM mechanism gating access to *copyrighted media
itself* — no video becomes decryptable or otherwise accessible because of
a PO Token that would not already be accessible via a real, unauthenticated
browser session. Whether a bot-detection challenge specifically counts as
one of the "security features" or "content protection functions" the
Terms describe is not resolved by the Terms' own text, and no authoritative
interpretation was found in public sources. This project takes no
position on which reading is correct; it is disclosed here as a real,
open question the owner should weigh, not resolved by asserting either
answer.

### GitHub's DMCA process

Read directly from GitHub's current DMCA policy: GitHub does **not**
disable a repository immediately on receiving a notice — the affected
user gets roughly one business day to remove or modify the flagged
content before any disabling, a public counter-notice process exists
(content is reinstated unless the claimant files suit within 10-14 days),
and GitHub explicitly offers free legal-referral support (the "Developer
Defense Fund") for anti-circumvention (DMCA §1201) claims specifically,
which require GitHub to conduct a more extensive technical-and-legal
review before acting — not an automatic takedown. This describes GitHub's
*process*, not an outcome guarantee for this or any repository; it is not
a basis for a "safe from DMCA" claim, and none is made here.

### GPL obligations

Already correctly implemented per the technical audit above:
license-notice preservation (`THIRD_PARTY_NOTICES.md`), corresponding
source availability (`SOURCE_OFFER.md` pointing at the unmodified upstream
release), and version/attribution tracking. No GPL obligation was found
to be unmet for any of the currently-considered distribution options
(Bundled, External, or a separate compatibility pack) since none of them
propose modifying or relicensing bgutil.

### SignPath implications

Already flagged by Phase 5's `SIGNPATH_APPLICATION.md`: bgutil's GPL v3
license itself creates no conflict with SignPath's "no proprietary
component" rule. The open question SignPath's own document deferred to
this phase — whether reviewers might view PO-token generation as
adjacent to "circumventing" bot-detection, a discretionary concern absent
a written rule — is not resolved by this phase's research (no public
SignPath precedent addressing PO-token providers specifically was found);
the recommendation stands as Phase 5 already stated: disclose this
proactively in any future SignPath application rather than let a reviewer
discover it.

### Store implications

Directly relevant confirmed fact (see the Network behavior finding
above): the bundled provider does fetch and execute a remote script at
runtime, from YouTube's own servers. Microsoft Store policy 10.2.2 ("no
dynamic code inclusion... should not download a remote script and
subsequently execute it") is written broadly enough that this technique
falls within its literal text, regardless of the script's actual origin
or trustworthiness. This is a real, now-confirmed consideration for a
future Microsoft Store submission — moot for the moment since that
investigation is separately paused (HUMAN GATE 4), but no longer an open
question if that investigation resumes.

## B. Compare transparent strategies

### Option 1 — Bundled (current state)

* Best out-of-box UX: works immediately on a clean machine, no extra
  install step, no user awareness of "PO Token" required.
* Frozen, compatible, reproducible set: the plugin/backend/Deno trio is
  staged and tested together at build time, avoiding a mismatched
  combination a user might otherwise assemble themselves.
* Larger package: contributes to the ~84 MB `pot-provider-backend` +
  ~95 MB Deno runtime measured in Phase 6's
  `PACKAGE_AND_RUNTIME_PROFILE.md` (not the dominant size driver —
  Chromium is — but not free either).
* Legal/policy review needed: this is that review; nothing found above
  rules out bundling, but the open interpretive questions (ToS
  "circumvention" framing, SignPath discretionary review, Store 10.2.2)
  remain genuinely open rather than closed by this research.

### Option 2 — User-installed external provider

* BananaFlow would only *detect* an already-installed provider (as it already
  does for source/venv installs where a user manually
  `pip install`s the plugin) rather than bundling one in the frozen EXE.
* User installs separately: a real UX regression for packaged-EXE users —
  today's frozen build works without any provider setup; this option
  would remove that for the primary distribution channel.
* Less bundled code: removes the ~84 MB backend + reduces (but does not
  eliminate — Deno is also used by yt-dlp-ejs) the Deno footprint from
  the *default* install.
* No automatic protection from legal claims: moving distribution
  responsibility onto the user doesn't change whether the technique
  itself is contested — it changes *who* is distributing the GPL v3
  component, which may itself matter for a rights-holder's choice of
  target, but is not a resolved legal shield.

### Option 3 — Separate open-source compatibility pack

* Transparent repo/build: a dedicated, clearly-labeled companion
  repository (e.g. `bananaflow-pot-provider-pack`) that stages exactly what
  `packaging/stage_pot_provider.py` already stages today, published and
  versioned independently.
* Optional install, separate release: users who want zero-friction
  YouTube reliability install the pack; users who want the smallest/most
  conservative footprint don't.
* More maintenance: a second repository, a second release cadence, a
  second place version/checksum drift (already a known gap per Phase 7)
  could occur.
* Still needs legal review: the same open questions from Section A apply
  regardless of which repository stages the same unmodified upstream
  code — splitting the repo does not resolve the ToS/Store/SignPath
  questions, only relocates where the bundled code physically lives.

### Option 4 — No provider

* Simplest legal posture: removes the one component whose stated purpose
  is specifically to help defeat a bot-detection challenge, which is the
  narrowest reading of "reduce exposure to the open questions in Section
  A" — but simplest is not the same as "resolved" or "risk-free," since
  yt-dlp itself (unmodified) still performs YouTube extraction regardless
  of PO Token availability, and the ToS's broader automated-access
  restriction applies to the extraction itself, not only to the token
  mechanism.
* Reduced YouTube reliability: YouTube Doctor's own existing state
  machine already documents what happens without a provider — "some
  YouTube videos may fail with a PO Token error" — a real, already-known
  functional regression, not a hypothetical one.
* More support failures: every current PO-Token-related failure mode
  Doctor already detects (missing plugin, missing runtime, incomplete
  backend, failed health check) would collapse into a single "no
  provider, videos may fail" state for every packaged user, not just
  those who skipped an optional install.

## C. Required transparency (already satisfied by the current implementation, verified directly — not asserted)

* No hidden packaging: `packaging/stage_pot_provider.py` is a plain,
  committed, readable script.
* No secret build scripts: same script; also invoked as a visible,
  SHA-pinned CI step.
* Version pin: `1.3.1` (both locations, cross-check gap tracked
  separately in Phase 7's follow-up work, not fixed here).
* Checksum: **not currently met** — F-013's no-committed-hash gap is real
  and applies regardless of which distribution option is chosen.
* License notice: present and correct (`THIRD_PARTY_NOTICES.md`).
* Source link: present (`SOURCE_OFFER.md`,
  `THIRD_PARTY_NOTICES.md`, both pointing at the unmodified upstream
  repository).
* SBOM: present (Phase 4's CycloneDX generator includes it).
* Clear user disclosure: `README.md` and `THIRD_PARTY_NOTICES.md` both
  describe the provider stack; Doctor surfaces its state directly to the
  user in-app.
* No logging token values: confirmed — this project's own code never logs
  a token value.
* No manual PO Token scraping by BananaFlow: confirmed — BananaFlow only passes
  yt-dlp the provider's official `server_home` argument; it does not
  itself generate, scrape, store, or inject a token.

The one transparency item genuinely unmet today, independent of which
option the owner picks, is the missing committed checksum for the
provider's staged source download (F-013) — worth closing regardless of
the HUMAN GATE 7 answer.

## HUMAN GATE 7 — Provider Distribution Decision

See the accompanying chat message for the four options and this
document's analysis above. No packaging change is made until the owner
answers.

### Owner answer (2026-07-17)

**Option 1 — Bundled (current state)**, the recommended choice. No
packaging change is made. The owner explicitly accepted the plan's
recommendation to seek legal counsel before any large-scale public
distribution, independent of this choice, given the genuine open
questions this document discloses (the ToS "circumvention" framing
tension, SignPath's discretionary-review question, and the confirmed
Microsoft Store 10.2.2 consideration — moot today since that
investigation is paused). The known checksum gap (F-013) remains open,
independent of this decision, and is not fixed in this phase.
