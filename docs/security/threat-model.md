# BananaFlow threat model

Status: **Current / normative**

This threat model focuses on security properties of the desktop application and its release process. It is not a claim that third-party services are secure and does not authorize testing against them.

## Assets to protect

- authentication cookies/session material and optional API/proxy credentials;
- user media libraries and metadata, including protection against unintended overwrite/delete;
- local paths, history, logs, drafts/backups and other potentially private metadata;
- integrity of downloaded application releases, bundled runtime components and per-user component overlays;
- integrity of configuration and persisted migration state;
- user expectations about what network services receive which data.

## Trust boundaries

1. **User ↔ application UI/CLI** — untrusted URLs, filenames, CSV/import data and user choices enter the process.
2. **Application ↔ local filesystem** — media files, app-data, temp files, logs, browser profiles and backups.
3. **Application ↔ third-party network services** — YouTube, Spotify, MusicBrainz, GitHub, PyPI and optional providers/proxies.
4. **Application ↔ browser/runtime subprocesses** — Playwright Chromium, FFmpeg, Deno and provider/plugin execution.
5. **Source/release pipeline ↔ third-party dependencies and staged binaries** — package registries, upstream release archives and GitHub Actions.

## Primary attacker/failure classes

- malicious or malformed remote content/metadata/URLs;
- compromised or misconfigured user-selected proxy/service;
- malicious local file names/metadata/import files;
- another local user or process attempting to read BananaFlow data;
- same-user malware (out of scope for strong local confidentiality; it can generally access the user's process/files);
- compromised upstream dependency/artifact or release infrastructure;
- accidental destructive behavior caused by crashes, races, path mistakes or stale state.

## Security properties

### Credentials and cookies

- No live credential belongs in source, logs, diagnostics, issue templates or test fixtures.
- The isolated sign-in browser profile is separate from normal browser profiles.
- BananaFlow-owned cookie material is minimized and protected to the current user where supported; Windows DPAPI is used for the protected store.
- Diagnostics describe readiness without exposing cookie values.
- Deletion controls remove BananaFlow-owned sign-in state without modifying a user's normal browser profile.

### Filesystem integrity

- User-controlled paths and names are validated before destructive operations.
- Tag Editor Apply writes through same-filesystem temporary copies, verifies intended changes before atomic replacement and journals recoverable state.
- Rename graphs are preflighted for collisions/cycles/platform hazards/root escape.
- External modifications are not silently overwritten by pending Tag Editor proposals.
- Tests for destructive paths use disposable fixtures.

### Network behavior

- TLS verification is not disabled for security-sensitive update/component flows.
- Retries are bounded and distinguish transient from permanent/authentication failures.
- New external endpoints or transmitted data require `PRIVACY.md` review.
- BananaFlow does not promise that a user-selected third-party proxy is trustworthy.

### Update and supply chain

- The packaged-app update path directs users to versioned releases and does not silently replace application code.
- Release artifacts publish checksums/SBOM/attestation evidence as configured by the release pipeline.
- Bundled components are staged through reviewed scripts and documented with source/license/version information.
- Independently downloaded `yt-dlp` / `yt-dlp-ejs` updates are accepted only from the exact official GitHub component-channel release, after GitHub asset digest/size verification, manifest/bundle agreement, compatibility and bounded safe-unpack checks. A fresh authenticated control record can disable the channel or revoke a bundle; an expired record must refresh successfully before an overlay is reused.
- A prepared overlay is health-checked in a separate process and atomically selected only for the next launch; the previous valid selection is retained for fallback and a failed/interrupted preparation cannot change the active pointer.

## Important residual risks

- Unsigned Windows binaries cannot provide publisher identity through Authenticode; checksums/attestations help integrity/provenance but are not a substitute for code signing.
- Browser automation and third-party extraction operate against changing remote services and may expose ordinary network metadata.
- Cookies remain sensitive even when protected at rest; same-user malware is not defeated by DPAPI/file permissions.
- Third-party dependencies and upstream services can change or be compromised; release review and automated scanning reduce but do not eliminate supply-chain risk.
- Generic-site extraction processes untrusted remote pages and must remain isolated from credentials not required for that operation.

## Review triggers

Update this model when a PR adds or materially changes authentication, persisted secrets, network services, update/downloaded-code behavior, browser/runtime execution, destructive filesystem operations, privilege requirements, sandboxing, release provenance or supply-chain controls.
