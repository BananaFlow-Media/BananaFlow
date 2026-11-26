# Secure component updater — design record

Status: **Implemented / normative**

BananaFlow can update the fast-moving `yt-dlp` / `yt-dlp-ejs` pair independently of the installed application. Source environments use an explicitly approved pip update. Packaged builds use the verified per-user overlay implemented by `core/component_overlay.py`; the installed program directory is never rewritten.

## Why a partial updater is not acceptable

An updater is a code-execution supply-chain path. Downloading a “latest” binary/script and replacing a live component without authenticated metadata, exact integrity verification, compatibility checks, atomic activation and rollback would create a new security surface worse than the current full-release model.

## Required design properties

The implementation provides the following as one coherent design:

### Authenticated manifest and trust root

The control plane is the `component-channel-v1` pre-release in the official `BananaFlow-Media/BananaFlow` GitHub repository. The client requests that exact repository/tag through GitHub's HTTPS Releases API. It accepts exactly one named manifest asset and verifies the asset size and GitHub-provided SHA-256 digest before parsing it. The manifest then identifies the exact bundle asset, size/hash, BananaFlow compatibility range, component versions and emergency-disable/superseded state. Manifest-supplied download URLs are not used: the matching official release asset API record is the download authority.

### Exact integrity and transport

- HTTPS with normal certificate validation;
- exact SHA-256 (or stronger reviewed digest) checked before activation;
- expected size / bounded download behavior;
- no `nocheckcertificate`-style bypass;
- no execution of a partially downloaded artifact.

### Compatibility

Manifest metadata declares compatible BananaFlow versions/component combinations. The compatibility range is stored with each prepared bundle and is checked again before every activation, so an application upgrade cannot activate an old incompatible overlay.

Component publication explicitly retains the reviewed minimum application
version in the channel build command. Tagging a newer app does not advance
that floor. Change it when component compatibility requires a newer app.

The packaged update check uses the same authenticated manifest, eligibility rules and official bundle-asset metadata as installation. It compares the eligible package versions with the components loaded in the current process. Checking reads only release metadata and the manifest. Only approved installation downloads the ZIP. An older channel downloader cannot be offered as an EJS-only update.

### App-data overlay, not live in-place mutation

Downloaded components install under `components/downloader/bundles/<bundle-id>/site-packages` in BananaFlow's per-user app-data directory. Activation changes only `active.json`, and startup prepends the selected valid overlay before the first `yt_dlp` import. A separately authenticated, cached `control.json` records the channel's disabled/revoked-bundle state; it is refreshed from the official channel at most once per 24 hours after a component update has been approved.

When the packaged downloader is newer than the selected verified overlay, startup uses the packaged pair. It compares metadata within the two specific package locations before importing either downloader.

Every activation still recomputes the exact stored tree digest before any overlay code can be imported. To avoid serial per-file antivirus latency on packages containing thousands of small files, bytes are read with a bounded worker pool and fed into the existing SHA-256 calculation in the same deterministic path order. This is a performance implementation detail only: the expected digest, covered names/bytes and fail-closed decision are unchanged.

### Atomic install and rollback

- download to temporary state;
- verify hash/size;
- unpack/prepare safely;
- run component-specific health check;
- atomically activate only after success;
- retain the previous active bundle as the last-known-good selection;
- fall back atomically to that previous bundle when the active bundle/marker is missing or invalid.

### Concurrency and shutdown

The worker can prepare an update while the application is running, but the current process keeps its already selected implementation. Only the next launch reads the new pointer, so active operations are never swapped. Preparation uses a private staging directory; failure does not change `active.json`, and abandoned staging data is safe to remove without affecting the active bundle. The isolated frozen health-check child bypasses normal overlay activation and staging cleanup, because its explicit validation target is inside that staging directory.

### User control

Checking for an update and installing one are separate concepts. Automatic installation, if ever offered, is an explicit opt-in. A manual verified update path remains available.

### Safe logging

Update logs use centralized redaction and never contain credentials. Public update artifacts/manifests should not require bearer secrets to download.

### Emergency response

The authenticated control plane can disable the whole channel or revoke individual bundle IDs without requiring the client to run them first. When the cached control record is older than 24 hours, startup refreshes only the signed manifest before considering an overlay. If that refresh cannot complete, BananaFlow fails closed for the optional overlay and uses its bundled components; it does not block the rest of the application. A revoked active bundle falls back to a still-valid, non-revoked previous bundle when available.

## Build and publication behavior

- `.github/workflows/component-channel.yml` detects upstream drift on a schedule but does not create an unreviewed/bot-authored dependency change.
- When an exact component pin is reviewed and merged to `main`, CI builds the overlay from the installed distributions, runs updater tests, produces provenance evidence and publishes the bundle before replacing the channel manifest.
- The Windows and macOS release workflows also build full Actions release candidates for a reviewed pin change. They do not create or publish an application release without the existing tag/human release process.
- Installation remains an explicit user action. A successful update requires a restart. The success UI offers an explicit restart or later choice, and component updates remain independently accessible when a full application release is also available. Restart must respect active work and shutdown ownership; it must not bypass the normal safety checks.

## Native runtime packages

FFmpeg and FFprobe ship together. Deno has its own package. Native runtime
packages use the official `runtime-channel-v<version>-r<maintenance>`
prerelease and an exact platform, tool and revision in each asset name.
Both discovery and preparation verify the official asset metadata. Preparation
streams the archive to a private staging directory, checks size and digest,
accepts only the expected executable names and license files, preserves
Internet origin markers and runs the tools' version checks. Maintainers
exercise the relevant conversion and download workflows on native systems
before publishing the package.

The runtime directory under `components/` is scoped to the exact application
version, maintenance revision and platform. Its atomic per-tool record holds
the current and prior selections. A new process verifies executable size and
digest and consults a cached record of the official asset names and digests.
The cache expires after 24 hours. A withdrawn asset, changed digest, corrupt
executable or unavailable expired record causes fallback to an authorized
previous selection or to the bundled runtime. GUI and CLI resolve both
tools from their process-lifetime selection. GUI startup verifies native
tools on a worker while the splash remains responsive. GUI and CLI also
activate the downloader overlay through the existing v1 channel.

## Change rule

Any change to this updater is security-sensitive. It requires threat-model, privacy/network, supply-chain, release, user-guide and test review in the same change. Do not weaken repository identity, API asset-digest, compatibility, revocation/control freshness, bounded-download, safe-unpack, isolated-health-check, atomic-selection or rollback checks.
