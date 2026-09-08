# BananaFlow architecture overview

Status: **Current / normative**

This document describes the stable architectural boundaries and data/trust flows. [`PROJECT_STRUCTURE.md`](../../PROJECT_STRUCTURE.md) is the detailed module map.

## Layers

```text
Qt UI (ui/panels, ui/components, ui/dialogs)
        ↓ signals / user intent
Controllers (ui/controllers)
        ↓ starts background work
QThread workers (ui/workers)
        ↓ plain Python calls
Core engines/services/persistence (core/)
        ↓ shared helpers
Utilities + configuration (utils/, config.py)
```

`core/` and `utils/` must not depend on Qt/PySide6 symbols. The documented `ui.i18n.t()` plain-Python lookup exception does not pull Qt objects into the backend. The CLI skips UI/controllers/workers and drives the same backend behavior directly.

## Startup boundary

The GUI displays a lightweight, localized splash immediately after `QApplication` and configuration/language setup. Heavy provider clients such as yt-dlp, ytmusicapi, HTTP search clients, and optional page widgets must not be imported before that first frame. The main download workspace is constructed first, then Settings and the Tag Editor are prepared on the GUI thread behind the visible splash before the interactive window is shown. Their lightweight navigation placeholders remain a safe fallback for embedded/test construction and recovery. This ordering preserves immediate launch feedback while ensuring first navigation cannot stall on Qt widget construction. Provider-specific clients remain lazy until their worker-backed operation requires them.

The post-show system preflight remains a background `QThread`. Shutdown requests cooperative cancellation, prevents a cancelled probe from opening a new Playwright driver connection, and joins the worker before Python interpreter teardown. Playwright availability uses the async API for both sync callers (through `asyncio.run`) and native async callers; this avoids the Playwright 1.62 sync wrapper leaving its internal `Connection.run` cancellation task pending after an executable-path-only probe on Python 3.12.

The qfluentwidgets import is performed once behind the splash with its import-time standard output redirected in memory. This contains the dependency's unconditional Pro advertisement without muting application logs or later runtime errors.

The startup update-check `QThread` follows the same shutdown ownership rule. An immediate close requests interruption between its app and component HTTP checks, hides the closing window while any current bounded request finishes, and retries `closeEvent` from the worker's `finished` signal. The window therefore never destroys a running child thread.

Lazy provider initialization stays inside the same background worker paths used for the corresponding search, metadata fetch or download. It must not move network or extraction work onto the Qt GUI thread.

## Threading boundary

Network access, scraping, downloading, conversion, metadata scans and other long operations never run on the GUI thread. Workers communicate back through Qt signals; they do not mutate widgets from worker threads. Shutdown of disk-changing operations must be bounded and event-loop safe.

## Main data flows

### URL/download

```text
URL / search result
→ classify/fetch metadata
→ queue request
→ orchestrator
→ source resolution/extraction
→ download
→ post-processing
→ output verification
→ history persistence
```

Spotify URL metadata can be obtained through the app's Spotify scraping/resolution path. Spotify **search** can use the optional configured proxy API. Spotify audio is not downloaded from Spotify servers; metadata is used to identify a separate media source.

Spotify and YouTube Music artist URLs use a staged catalog flow:

```text
artist URL
→ background category discovery
→ user category selection (skipped when only one exists)
→ background expansion with stable-release canonicalization
→ partial recovery from another selected role when needed
→ release-scoped provider metadata hydration (artwork + duration)
→ collapse exact repeated release occurrences
→ plain-Python cross-release/category duplicate grouping
→ per-occurrence user decisions
→ queue
→ lazy Spotify-to-YouTube resolution
→ batch-level target-collision guard
```

`core.artist_catalog` owns provider-neutral discovery models, YouTube Music release-ID canonicalization, exact repeated-occurrence collapse, duplicate confidence and decision application without importing Qt. `ArtistFlowController` owns the modal sequence; `ArtistCatalogDiscoveryWorker` and `ArtistCatalogScrapeWorker` keep provider/network work off the GUI thread. Spotify uses a scrape-local release registry: the first selected role owns a stable release ID, later roles are retained as `discovery_roles`, and a complete release is not expanded again. When the declared count shows the first grid was partial, another role may contribute only missing placements. Provider releases without stable IDs stay separate instead of being merged by title. Explicit provider release-type metadata takes precedence over the discovery tab. Because Spotify's artist grid does not expose cover art or duration in every row, selected stable releases are hydrated through bounded parallel public-embed requests before queue delivery; track IDs join the metadata back to rows, with release position as a same-release fallback.

Stable Spotify track IDs and YouTube video IDs produce exact recording groups. Metadata-only comparisons are intentionally conservative and are presented as probable rather than silently removed. A location includes the canonical release and track position, so duplicates inside one category remain reviewable and a deliberate repeated placement at another position is not silently removed.

Spotify matching remains lazy so removed catalog occurrences do not cause unnecessary searches. The resolver searches the ordinary `artist + title` wording first. If it yields no strict or bounded-reasonable identity candidate, the resolver tries at most three additional identity-preserving forms: punctuation-normalized artist/title, normalized title/artist order and optional album context. Every form retains both artist and title; no title-only or forced `audio` query is permitted. Candidates are deduplicated by concrete URL and pass through the same title, credited-artist, duration and recording-version scoring gates; a decisive complete result stops expansion, while ambiguous results deep-validate only the best three URLs. If all song searches miss and Spotify supplied an album, the resolver performs a bounded YouTube Music album lookup, verifies the album and primary artist, expands at most two releases and scores their tracks through the same recording gates. The release catalog is process-cached so adjacent tracks do not repeat the album request. `DownloadOrchestrator` maintains a thread-safe, batch-local claim registry for concrete YouTube video IDs. Compatible occurrences of the same recording may share a target. If distinct Spotify recording identities claim one video, the later claimant invalidates only its cached mapping and performs at most two fresh searches while excluding every URL form of the claimed video. Failure to obtain a distinct concrete match is surfaced as a per-track error; the orchestrator never silently submits the colliding target. This adds no persisted schema and uses the existing match-cache invalidation contract.

Filename numbering is provider-neutral and lives in `core.filename_numbering`. Controllers and the CLI supply the provider's original collection position and source/release context; GUI queue order is never a fallback. The policy produces separate filename and metadata-track decisions so, for example, a compilation can retain an authoritative embedded track number without receiving an `NN - ` filename prefix. A persisted `DownloadRequest` stores both decisions; older paused requests that contain only `forced_index` restore that value as their filename prefix to preserve the destination already chosen before the split.

Physical folder and filename-body decisions live in `core.output_layout`. `TrackMeta.collection_title` carries the identity of the source playlist/album/release separately from the member track's `album` tag. This prevents Spotify/YTM extraction variants from routing the same direct playlist by track artist or underlying album. The policy also owns artist/category/release hierarchy, collision-safe `Artist - Title` bodies for direct songs and compilations, and per-disc subfolders based on provider `disc_number`/`disc_total` metadata or the complete selected release. The GUI only localizes semantic category/disc segments; it does not contain a second folder policy. Queue persistence carries the collection/disc context, while paused requests persist the already-rendered destination folder and filename-body decision.

Download recovery is process-wide and Qt-free in `core.download_recovery`. Admission has separate rate-limit, systemic-failure and conservative-cadence gates. Ordinary terminal track failures never close process-wide admission: the controller snapshots each failed request at a clean retry boundary and aggregates equivalent errors into a live UI incident. Authentication/cookie, bot-challenge and connectivity scopes close only at three consecutive terminal failures; a successful completed track resets a scope that has not yet closed. Missing local prerequisites close immediately. Resolving an incident resets its scope, and Retry creates fresh requests for every collected key with current cookie settings and rebuilt lazy Spotify resolvers.

A zero-result incident also supports explicit source repair. The UI serializes one failed Spotify row at a time into the existing YouTube search surface. Selecting a track validates that it is a single YouTube/YouTube Music item, replaces only the request URL and disables automatic re-resolution for that retry. Selected rows are staged and then submitted through one bounded retry worker instead of starting an independent worker per click. Forced Spotify title/artist/album, numbering and output-routing fields remain unchanged. Stopping source selection submits the choices already made and returns to the unresolved aggregate incident; weak candidates are never accepted without the user's action.

Explicit YouTube throttling bypasses the ordinary incident threshold. The first definite 429/rate-limit response records the exact upstream message, advertised duration and a bounded safety margin, closes all YouTube admission and assigns the observing request as the sole post-deadline canary. In-flight peers may settle but cannot create repeated notices; queued peers remain blocked until the canary succeeds. The Qt layer renders the coordinator snapshot as one non-modal countdown dialog and footer state, avoiding nested dialog event loops and worker/UI stack recursion.

### Tag Editor

```text
scan local files
→ immutable original state + proposed changes
→ review/exclusion
→ Apply preflight
→ validated backup + durable journal
→ per-file temp-copy write
→ read-back verification
→ atomic replace
→ rename graph execution
→ result/recovery/undo-applied path
```

Selection controls editing scope; pending proposals control Apply scope. Disk safety is specified in [`tag-editor-safety.md`](tag-editor-safety.md).

## Persistence

Persistent application state is stored under the per-user BananaFlow app-data directory resolved by `utils.paths`. Examples include configuration, history, update state, logs, protected/minimized sign-in data, the dedicated browser profile, verified versioned downloader-component overlays, Tag Editor drafts/backups/recovery state and feature caches.

Persisted schema/path/meaning changes require forward migration behavior and tests. See [`../migrations/README.md`](../migrations/README.md).

## External services / trust boundaries

Depending on selected features, the application can communicate with YouTube/YouTube Music, Spotify, a user-configured Spotify proxy, GitHub Releases/API, PyPI, MusicBrainz, Cover Art Archive, lyrics providers, SponsorBlock and arbitrary user-selected sites handled by yt-dlp/generic extraction. The exact current privacy inventory lives in [`../../PRIVACY.md`](../../PRIVACY.md).

Credentials/cookies cross a stricter trust boundary than ordinary URLs/search terms. They must be minimized, protected locally where supported and never emitted to logs/diagnostics. See [`../security/threat-model.md`](../security/threat-model.md).

## Runtime components and supply chain

Packaged builds may include third-party executables/browser/runtime/plugin components. They are staged at build time, not committed as binaries. Versions, licenses, sources and release verification are tracked by `THIRD_PARTY_NOTICES.md`, `SOURCE_OFFER.md`, packaging README files, SBOM generation and the release checklist. The independently updateable `yt-dlp` / `yt-dlp-ejs` overlay is prepared in versioned app data and selected before the first downloader import; its trust, integrity, compatibility, atomicity and rollback contract is defined in [`secure-component-updater.md`](secure-component-updater.md). See also [`../security/supply-chain.md`](../security/supply-chain.md).

## Architecture change rule

A change that alters a layer boundary, trust boundary, persistence format, external service, threading model or safety invariant must update this overview and/or the relevant focused architecture document in the same PR.
