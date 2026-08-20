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

Persistent application state is stored under the per-user BananaFlow app-data directory resolved by `utils.paths`. Examples include configuration, history, update state, logs, protected/minimized sign-in data, the dedicated browser profile, Tag Editor drafts/backups/recovery state and feature caches.

Persisted schema/path/meaning changes require forward migration behavior and tests. See [`../migrations/README.md`](../migrations/README.md).

## External services / trust boundaries

Depending on selected features, the application can communicate with YouTube/YouTube Music, Spotify, a user-configured Spotify proxy, GitHub Releases/API, PyPI, MusicBrainz, Cover Art Archive, lyrics providers, SponsorBlock and arbitrary user-selected sites handled by yt-dlp/generic extraction. The exact current privacy inventory lives in [`../../PRIVACY.md`](../../PRIVACY.md).

Credentials/cookies cross a stricter trust boundary than ordinary URLs/search terms. They must be minimized, protected locally where supported and never emitted to logs/diagnostics. See [`../security/threat-model.md`](../security/threat-model.md).

## Runtime components and supply chain

Packaged builds may include third-party executables/browser/runtime/plugin components. They are staged at build time, not committed as binaries. Versions, licenses, sources and release verification are tracked by `THIRD_PARTY_NOTICES.md`, `SOURCE_OFFER.md`, packaging README files, SBOM generation and the release checklist. See [`../security/supply-chain.md`](../security/supply-chain.md).

## Architecture change rule

A change that alters a layer boundary, trust boundary, persistence format, external service, threading model or safety invariant must update this overview and/or the relevant focused architecture document in the same PR.
