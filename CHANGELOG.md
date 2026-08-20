# Changelog

All notable changes to BananaFlow are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Reorganized repository documentation around explicit sources of truth, current-vs-historical lifecycle and a Code → Documentation impact map.
- Added canonical AI-agent context (`AGENTS.md`, `docs/AI_CONTEXT.md`) plus thin Claude/Gemini/Copilot adapters and path-specific subsystem instructions.
- Added a Documentation GitHub Actions gate covering internal Markdown references, stale Stable/Beta language, yt-dlp/provider version consistency, AI-adapter integrity and pull-request documentation impact.
- Reconciled Security, Privacy, Support, English/Hebrew user references and the Spotify Search Proxy contract with the current Stable product behavior.
- Separated current architecture/policy from historical browser, updater, PO-token and performance evidence without discarding the engineering record.

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

### Known limitations

* **Windows binaries are unsigned** with Authenticode, so SmartScreen can warn on first run. Verify official release provenance and the published SHA-256 values.
* **macOS packaged support is experimental** and may require the platform's documented first-run override while signing/notarization remains limited; Linux remains source/developer-oriented unless a release says otherwise.
* **Third-party sites change independently of BananaFlow**. Generic-site extraction and remote-service compatibility are best effort rather than permanent guarantees.
* This release uses BananaFlow's own application-data namespace and does not assume that state from unrelated/older software should be migrated automatically.
