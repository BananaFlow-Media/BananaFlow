# Changelog

All notable BananaFlow changes are recorded here. The format follows Keep a Changelog principles and versions follow Semantic Versioning.

## [Unreleased]

### Changed

- Documentation was reorganized around explicit sources of truth, current-vs-historical status and a Code → Documentation impact map.
- Added repository AI-agent entry points (`AGENTS.md`, Claude/Gemini/Copilot adapters and path-specific instructions) so coding agents load architecture, safety, test and documentation context before editing.
- Added a documentation CI gate for broken internal Markdown references, stale release-status wording, provider-version drift and PR documentation impact.
- Reconciled Security, Privacy, English/Hebrew user manuals and the Spotify Search Proxy contract with the current Stable product behavior.

## [1.0.0] — 2026-08-09

First public BananaFlow stable release.

### Added

- Download engine for YouTube, YouTube Music and Spotify metadata/resolution workflows, including playlists/albums/discographies/channels, parallel execution and YouTube-specific reliability policy.
- Search across YouTube Music, YouTube and optional proxy-backed Spotify text search.
- Batch Tag Editor with proposal-first editing, Review/Undo/Redo, safe Apply/backup/journal/recovery, artwork/lyrics/ReplayGain, MusicBrainz, duplicate tools, CSV/report/playlist/workflow features.
- Format Converter with per-format quality presets and output verification.
- Isolated Cookie Wizard and YouTube Doctor diagnostics.
- Searchable download History and CLI (`bananaflow-cli`).
- English/Hebrew UI with complete RTL mirroring, accessibility/high-DPI/touch-oriented behavior.
- Windows installer + portable package, experimental macOS package, SHA-256 checksums, CycloneDX SBOM and GitHub build-attestation evidence.
- Application/component update checking with packaged users directed to full BananaFlow releases.
- Official bilingual BananaFlow website for downloads, Help, FAQ, support and legal information.

### Known limitations

- Windows packages are currently unsigned with Authenticode, so SmartScreen can warn on first run; verify official release provenance/checksums.
- macOS packaged support is experimental and Linux remains source/developer-oriented unless a release says otherwise.
- Third-party sites can change independently of BananaFlow; generic extraction remains best effort.
