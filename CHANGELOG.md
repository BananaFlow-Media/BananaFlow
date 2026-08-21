# Changelog

All notable changes to BananaFlow are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] — 2026-08-21

### Added

- Added an independently verified packaged-build component update channel for `yt-dlp` and `yt-dlp-ejs`. It installs only after explicit approval, verifies official release metadata and exact hashes, prepares the bundle safely in per-user app data, and activates only after restart.
- Added an authenticated component-control record: a disabled or revoked downloaded bundle, an expired unverifiable control record, or an incompatible application version now prevents that optional overlay from loading and leaves the bundled downloader available.

### Fixed

- Fixed component-channel release builds invoked by path in CI so they load the canonical application version before publishing a bundle.
- Improved YouTube download reliability by selecting the appropriate yt-dlp player clients for audio and video, while keeping the conservative request policy.
- Added thumbnail fallback and cache handling for unavailable YouTube thumbnail variants.
- Refresh Download History when returning to its panel, so newly completed downloads appear without restarting the application.

### Changed

- Updated the reviewed, reproducible `yt-dlp[default]` release pin to `2026.8.19`; its matching EJS support remains installed through yt-dlp's `default` extra.
- Reorganized repository documentation around explicit sources of truth, current-vs-historical lifecycle and a Code → Documentation impact map.
- Added canonical AI-agent context (`AGENTS.md`, `docs/AI_CONTEXT.md`) plus thin Claude/Gemini/Copilot adapters and path-specific subsystem instructions.
- Added a Documentation GitHub Actions gate covering internal Markdown references, stale Stable/Beta language, yt-dlp/provider version consistency, AI-adapter integrity, current platform-status wording and pull-request documentation impact.
- Made the local documentation gate ignore untracked development artifacts and enforce that every tracked document under `docs/` is reachable from the documentation map.
- Replaced inactive GitHub Discussions routes with the currently available website-support and GitHub Issue paths.
- Reconciled Security, Privacy, Support, English/Hebrew user references and the Spotify Search Proxy contract with the current Stable product behavior.
- Separated current architecture/policy from historical browser, updater, PO-token and performance evidence without discarding the engineering record.
- Promoted **macOS Apple Silicon** from the 1.0.0 release's experimental status to a currently supported packaged target after maintainer validation that the packaged app opens and runs; signing/notarization limitations remain separately documented.
- Clarified **Linux** as a supported source-install platform: the application is expected to work normally when dependencies are installed, although no official Linux installer/package is published yet.
- Refocused the English/Hebrew user manuals on ordinary non-programmer users; development/build/architecture/testing material now lives in the dedicated contributor documentation.

## [1.0.0] — 2026-08-09

First public BananaFlow Stable release.

### Added

* **Download engine** for YouTube, YouTube Music and Spotify metadata/resolution workflows: playlists, albums, artist discographies and channels; parallel downloads (1–6); retry policy with rate-limit politeness; a conservative YouTube reliability mode; and a bundled PO Token Provider + JavaScript runtime in packaged builds so downloads work out of the box.
* **Search** across YouTube Music, YouTube and optional proxy-backed Spotify text search.
* **Batch Tag Editor**: proposal-first editing with preview and guarded Apply, undo/redo, JSON backups with restore and a backup manager, MusicBrainz enrichment, ReplayGain, artwork handling, CSV import/export, playlists export and action presets/workflows.
* **Format Converter** with per-format quality presets and output verification.
* **Cookie Wizard**: YouTube sign-in through an isolated Playwright browser profile rather than silently reusing a normal browser profile.
* **YouTube Doctor** diagnostics in the GUI and through `bananaflow-cli --doctor`.
* **Download History** with search and per-item actions.
* **Bilingual UI**: English and Hebrew with complete RTL mirroring, number/date isolation and a Fluent design system with light/dark themes and accent colors.
* **CLI** (`bananaflow-cli`): headless downloading, track listing and diagnostics.
* **Packaging**: Windows installer and portable package; experimental macOS arm64 package; SHA-256 checksums; CycloneDX SBOM; GitHub build-attestation evidence.
* **Update checks** against the BananaFlow GitHub Releases feed, plus advanced component-version checks for supported workflows.
* **Touch-screen support**: drag-to-scroll with flick/coast behavior; press-and-hold equivalents for context menus/tooltips; touch-friendly queue reordering; Tag Editor touch selection; pinch/touchpad/Ctrl+wheel zoom for the Tag Editor file list; and optional Touch-Friendly Sizing for larger controls/scrollbars/menu rows.
* **Official bilingual website** at <https://bananaflow.bananaflow-media.workers.dev/> with downloads, Help, FAQ, support and legal pages. Application/CLI/installer metadata link to the official site through the repository's central website URL helper.

### Known limitations at 1.0.0 release time

* **Windows binaries were unsigned** with Authenticode, so SmartScreen could warn on first run. Verify official release provenance and the published SHA-256 values.
* **macOS packaged support was experimental** at the time 1.0.0 shipped and had limited signing/notarization; current support policy is tracked above in `[Unreleased]` and the current README/user documentation.
* **Linux was documented as source/developer-oriented** at the time 1.0.0 shipped; current source-support policy is tracked above in `[Unreleased]` and current documentation.
* **Third-party sites change independently of BananaFlow**. Generic-site extraction and remote-service compatibility are best effort rather than permanent guarantees.
* This release uses BananaFlow's own application-data namespace and does not assume that state from unrelated/older software should be migrated automatically.
