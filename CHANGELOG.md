# Changelog

All notable changes to BananaFlow are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions
follow [Semantic Versioning](https://semver.org/).

## [1.0.0-beta.1] — unreleased

First public BananaFlow release.

### Added

* **Download engine** for YouTube, YouTube Music and Spotify: playlists,
  albums, artist discographies and channels; parallel downloads (1–6);
  retry policy with rate-limit politeness; a conservative YouTube
  reliability mode; and a bundled PO Token Provider + JavaScript runtime
  in packaged builds so downloads work out of the box.
* **Search** across YouTube Music, YouTube and Spotify.
* **Batch Tag Editor**: proposal-first editing with preview and guarded
  apply, undo/redo, JSON backups with restore and a backup manager,
  MusicBrainz enrichment, ReplayGain, artwork handling, CSV
  import/export, playlists export and action presets.
* **Format Converter** with per-format quality presets and output
  verification.
* **Cookie Wizard**: YouTube sign-in through an isolated Playwright
  browser profile — the app never reads your real browser's cookie
  store.
* **YouTube Doctor** diagnostics (GUI panel and `bananaflow-cli
  --doctor`).
* **Download History** with search and per-item actions.
* **Bilingual UI**: English and Hebrew with complete RTL mirroring,
  number/date isolation and a Fluent design system (light/dark themes,
  accent colors).
* **CLI** (`bananaflow-cli`): headless downloading, track listing and
  diagnostics.
* **Packaging**: Windows installer (Inno Setup) and portable ZIP; macOS
  arm64 DMG; SHA-256 checksums; CycloneDX SBOM; GitHub build
  attestations.
* **Update check** against the BananaFlow GitHub Releases feed.
* **Touch screen support**: drag to scroll any list, table or panel with
  flick-to-coast; press and hold in place of a right-click for context
  menus, and in place of a hover to read a control's tooltip without
  activating it. The download queue's remove button no longer requires a
  hover to appear, and queue reordering — previously drag-only — is
  available from a card's menu. In the Tag Editor's file table a drag
  scrolls rather than selecting, and a tap chooses a row only when the
  finger stayed put. Several rows are selected by flicking each one
  sideways — the cross-slide gesture Windows itself defines for a list
  that scrolls in one direction. The Tag Editor's file list zooms by
  pinching — on a touch screen, on a precision touchpad, or with Ctrl +
  mouse wheel. An optional **Touch-Friendly Sizing**
  setting enlarges controls, scrollbars and menu rows to finger-sized
  targets.

### Known limitations

* Binaries are unsigned: Windows SmartScreen warns on first run; macOS
  requires right-click → Open. Verify downloads against the published
  checksums (see `SECURITY.md`).
* macOS support is experimental; Linux is source-install only.
* This beta starts with a fresh application-data directory. It does not
  read or migrate configuration, download history, tag backups, presets
  or cookies created by any software previously installed on the
  machine.
