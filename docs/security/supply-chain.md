# BananaFlow software supply chain

Status: **Current / normative process description**

This document explains how third-party code and binary components enter a BananaFlow release. Exact per-release versions/licenses/hashes belong in `THIRD_PARTY_NOTICES.md`, lock/constraint files, the SBOM and release assets rather than being copied here.

## Sources

BananaFlow releases can contain:

- Python dependencies installed from declared project requirements/constraints;
- PySide6/Qt native libraries from Python wheels;
- yt-dlp and its compatible EJS support;
- a reviewed FFmpeg/ffprobe build staged for packaging;
- Playwright and its Chromium browser payload;
- a Deno runtime staged by the repository fetch script;
- the configured PO Token Provider plugin/backend staged by repository tooling;
- operating-system installer metadata/assets.

## Controls

### Dependency declaration

Runtime/dev/optional dependencies are declared in `pyproject.toml`, `requirements.txt` and release constraints as appropriate. New runtime dependencies require license review and `THIRD_PARTY_NOTICES.md` updates.

### Staged binaries/components

Large third-party binaries and generated component trees are intentionally gitignored. Repository README files describe each staging slot. Build/fetch scripts perform the staging so the process is reviewable rather than relying on an undocumented maintainer machine state.

### Integrity and provenance

Where upstream publishes checksums or a reviewed hash is available, staging verifies it. The release process records/validates the exact component actually shipped. Release outputs include SHA-256 checksum files, a CycloneDX SBOM and GitHub build attestation evidence as configured in the workflows.

A checksum fetched from the same compromised origin as an artifact is not an independent trust root; treat such cases as weaker evidence and prefer independently pinned/reviewed hashes when practical.

### Licenses/source availability

- `THIRD_PARTY_NOTICES.md` — dependency/component inventory and license handling.
- `SOURCE_OFFER.md` — corresponding-source availability.
- `LICENSES.md` — installer-facing bundle overview.
- packaging README files — staging mechanics.

Do not ship a new component until its license/source obligations are understood and documented.

### CI/security scanning

The repository uses dependency review/scanning, secret scanning and code/security workflows configured under `.github/`. These are defense in depth; a green scanner is not a release sign-off.

## Release verification

The blocking release checklist in `docs/release/RELEASING.md` verifies that the build is produced from the intended tag, tests/scans pass, staged components match documentation, artifacts smoke-test, checksums/SBOM are present and manual acceptance succeeds before publication.

## Update model

Packaged users normally receive component fixes through a full BananaFlow release. Source environments may support explicitly approved package upgrades. Do not create an independent downloaded-code updater without the authentication/hash/compatibility/atomicity/rollback/health-check requirements documented by `docs/architecture/secure-component-updater.md`.

## Change rule

Any change to a dependency source, pinning strategy, staging script, runtime component, build workflow, artifact format, checksum/SBOM/attestation behavior or signing strategy must review this document plus the release and third-party-notice documentation in the same PR.
