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

A reviewed downloader-pin update must confirm that the selected release is not yanked, inspect its Python/EJS dependency metadata, install it in a clean environment, pass `pip check`, and run the focused component/downloader plus real-network gates against that exact candidate. The full platform matrix then installs the reviewed pin from scratch before it can enter a tagged build.

### Staged binaries/components

Large third-party binaries and generated component trees are intentionally gitignored. Repository README files describe each staging slot. Build/fetch scripts perform the staging so the process is reviewable rather than relying on an undocumented maintainer machine state.

### Integrity and provenance

Where upstream publishes checksums or a reviewed hash is available, staging verifies it. The release process records/validates the exact component actually shipped. Release outputs include SHA-256 checksum files, a CycloneDX SBOM and GitHub build attestation evidence as configured in the workflows.

A checksum fetched from the same compromised origin as an artifact is not an independent trust root; treat such cases as weaker evidence and prefer independently pinned/reviewed hashes when practical.

The native macOS FFmpeg recipe applies one bounded libvorbis compatibility
adjustment only after archive verification and safe extraction: it removes
the three obsolete `-force_cpusubtype_ALL` flags from the reviewed generated
Darwin `configure` assignments. Unexpected version/content fails closed.
Build evidence records the original and adjusted file hashes, and the source
bundle retains unchanged upstream archives plus the exact adjustment recipe.
The normal libvorbis build and its `lib` self-test remain mandatory, followed
by FFmpeg architecture, linkage, license and audio-roundtrip gates.

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

Source environments can run an explicitly approved package upgrade. Packaged builds can install the reviewed `yt-dlp` / `yt-dlp-ejs` pair from the official GitHub component channel into a per-user versioned overlay. GitHub's official release-asset size/SHA-256 metadata authenticates the manifest and bundle provenance; the client independently enforces manifest/bundle hash agreement, compatibility, a periodically refreshed authenticated disabled/revoked-bundle control record, download/expanded-size bounds, safe archive paths, an isolated health check, atomic next-launch selection and last-known-good fallback. The Windows installer also removes only the previous packaged downloader module/metadata trees before copying a new full build, preventing obsolete one-folder PyInstaller files from surviving an in-place upgrade. See `docs/architecture/secure-component-updater.md`.

A scheduled workflow reports a newer non-yanked candidate as a warning and review checklist, without authoring a dependency change. Expected upstream drift is not a build failure; registry/metadata errors and a missing or fully yanked reviewed pin remain failures. Version comparison follows PEP 440, including development releases. A successful advisory check is not approval of the candidate. A human-reviewed pin merged to `main` automatically builds/publishes the component channel and triggers Windows/macOS Actions release candidates. Public application releases still require a version tag, draft verification and deliberate maintainer publication.

The explicit full-application updater uses exact official-repository release assets and verified size/SHA-256, preserving installed/portable/macOS layout distinctions. It does not replace platform publisher signatures or independently verify attestations merely because the build published them. Its lifecycle and residual trust contract are documented in [`../architecture/application-updater.md`](../architecture/application-updater.md). PR package validation must remain separate from channel/release publication.

## Change rule

Any change to a dependency source, pinning strategy, staging script, runtime component, build workflow, artifact format, checksum/SBOM/attestation behavior or signing strategy must review this document plus the release and third-party-notice documentation in the same PR.
