# Third-Party Notices

Status: **Current release-compliance inventory**

Version reference: BananaFlow `1.1.1` (must match [`version.py`](version.py); CI enforces this release-compliance snapshot).

BananaFlow is licensed as GPL-3.0-or-later. This document records third-party packages, staged binaries/components, their license families, source locations and release-handling obligations. It is a best-effort open-source compliance document, not legal advice.

**Exact-version rule:** do not treat a developer's current virtual environment as the release inventory. Exact versions actually shipped belong to the **release constraints/requirements, staged binary inspection and generated CycloneDX SBOM**. This file records intentional pins when BananaFlow itself stages a component explicitly and records the license/source obligation that must remain true across versions.

`version.py` owns the application version. The version-reference line above exists because this compliance document is shipped with releases and is automatically checked for drift; do not copy a separate “latest release” claim into unrelated prose.

## 1. Explicitly staged/bundled runtime components

### FFmpeg / ffprobe

| Field | Policy/current reviewed input |
|---|---|
| Project | FFmpeg / ffprobe |
| License | Build-dependent; BananaFlow's reviewed Windows input is expected to be LGPL-compatible with the GPL application distribution |
| Source | https://ffmpeg.org/ |
| Use | yt-dlp post-processing, conversion/probing, HLS/DASH/media operations |
| Release requirement | Inspect the **actual staged binary** with `ffmpeg -version`; record configure/license status and checksums; never infer license from filename |

For the reviewed Windows release input, the staged build has been checked as an LGPLv3-effective `--enable-version3` build with no `--enable-gpl` / `--enable-nonfree`. If FFmpeg is restaged, that conclusion must be re-verified rather than copied forward.

The current recorded staged input from the 1.0.0 release preparation was:

- build `N-124549-g1572784128-20260519`;
- `ffmpeg.exe` SHA-256 `2cbdf99e2c5a4fbe3c653478e11cdf331f3345f732ee64d54b096c50ea7838a6`;
- `ffprobe.exe` SHA-256 `775d3fab1a272978c82af25ad9c50b020b963b45a93afd0fc7db03e0b6c5637f`.

These are point-in-time release inputs, not promises about future builds.

### bgutil yt-dlp PO Token Provider stack

| Field | Policy/current pin |
|---|---|
| Project | `bgutil-ytdlp-pot-provider` |
| License | GPL v3 |
| Current staged pin | `1.3.1` — source of truth for the staging step is `packaging/stage_pot_provider.py`; `pyproject.toml` exposes the matching source/venv optional dependency |
| Source | https://github.com/Brainicism/bgutil-ytdlp-pot-provider |
| Use | yt-dlp PO Token Provider plugin + matching Deno script backend |
| Release requirement | Preserve GPL notice/source availability for the plugin/backend, stage the reviewed matching source, and verify the packaged provider path with YouTube Doctor |

BananaFlow configures yt-dlp's provider mechanism and bundled `server_home`; BananaFlow application code does not manually generate/store/inject live PO-token values.

The backend staging step preserves upstream manifests/locks and installs the production npm dependency tree needed by the provider backend. That tree is part of the release review surface and must be represented by available package metadata/source/license information.

### Deno JavaScript runtime

| Field | Policy/current recorded input |
|---|---|
| Project | Deno |
| License | MIT |
| Source | https://github.com/denoland/deno |
| Use | JavaScript runtime for yt-dlp EJS/provider script paths |
| Release requirement | Stage only the intended OS/architecture, verify the fetched artifact using the repository fetch script, record exact version/checksum in release evidence |

The 1.0.0 release-preparation snapshot recorded Deno `2.9.1` (`x86_64-pc-windows-msvc`) with SHA-256 `3819117e301d48a6931f9a1a4fb5e4a10c464163189bcff8fce5d75025d6f2a0`. Re-verify whenever the runtime is restaged.

### Playwright / Chromium

| Component | License handling | Release requirement |
|---|---|---|
| Playwright Python/driver package | Apache-2.0 upstream | Preserve package notice/source information when bundled |
| Chromium browser payload | Chromium BSD-3-Clause plus many third-party component licenses | Preserve/generated browser notices required by the exact browser payload; review the exact Playwright revision bundled |
| Playwright support binaries | Component-specific | Include their license metadata/notices as required by the selected Playwright bundle |

Chromium is intentionally a large packaged dependency because browser-backed Spotify/generic/Cookie-Wizard paths need real DOM/JavaScript execution. The architecture rationale is in [`docs/architecture/browser-component-decision.md`](docs/architecture/browser-component-decision.md).

## 2. Python dependency license inventory

**Version sources:**

- Source-install compatibility ranges → `pyproject.toml`.
- Reproducible application/release install → `requirements.txt` and release constraints.
- Exact packages actually frozen → release SBOM.

The important yt-dlp distinction is intentional:

- project/source compatibility floor: **`yt-dlp[default]>=2026.7.4`**;
- reviewed reproducible CI/release install: currently **`yt-dlp[default]==2026.8.19`** in `requirements.txt`/the matching release path.

A newer reviewed exact release pin does not change the minimum compatibility floor, and the floor must not be inferred from an old developer environment.

| Dependency | License | GPL-3.0-or-later compatibility / release note |
|---|---|---|
| `yt-dlp[default]` | Unlicense | Compatible; `[default]` also selects the matching EJS support expected by yt-dlp |
| `yt-dlp-ejs` | Unlicense + permissive JS component licenses (including MIT/ISC as applicable) | Preserve solver/package notices included by the exact dependency set |
| `ffmpeg-python` | Apache-2.0 | Pure-Python binding; FFmpeg binary obligations are separate |
| `pyloudnorm` | MIT | Preserve notice when bundled |
| `soundfile` | BSD-3-Clause | Preserve notice/native-library notices included by wheels |
| `mutagen` | GPL-2.0-or-later | Compatible with BananaFlow's GPL-3.0-or-later distribution; relevant to any future proprietary/relicensing plan |
| `syncedlyrics` | MIT | Optional feature; preserve notice when bundled |
| `Send2Trash` | BSD-3-Clause | Preserve notice |
| `PySide6`, `PySide6_Essentials`, `PySide6_Addons`, `shiboken6` | Qt for Python LGPL/GPL alternatives (package-specific metadata) | Current GPL application distribution is compatible; preserve Qt/PySide notices and dynamic-library/user-replaceability obligations where applicable |
| `PySide6-Fluent-Widgets` | GPL v3 / commercial dual-license | Compatible with current GPL BananaFlow; future closed/proprietary distribution needs separate rights/replacement review |
| `PySideSix-Frameless-Window` | LGPL v3 | Transitive Qt-adjacent dependency; preserve notice/replaceability treatment |
| `requests` | Apache-2.0 | Preserve notice |
| `httpx` | BSD-3-Clause | Preserve notice |
| `beautifulsoup4` | MIT | Preserve notice |
| `lxml` | BSD-3-Clause | Wheels can include libxml2/libxslt-style notices; preserve bundled license data |
| `ytmusicapi` | MIT | Preserve notice |
| `Pillow` | HPND/MIT-style | Preserve notice |
| `python-dotenv` | BSD-3-Clause | Preserve notice |
| `certifi` | MPL-2.0 | Preserve MPL notice for the unmodified CA bundle/file |
| `keyboard` | MIT | Windows-only optional runtime dependency |
| `playwright` | Apache-2.0 | Browser payload obligations are separate |
| `pytest` | MIT | Development/test dependency; normally not part of the application runtime bundle |
| `pytest-mock` | MIT | Development/test dependency; normally not part of the application runtime bundle |

The release SBOM can contain additional **transitive** packages (for example HTTP stacks, image/scientific/native libraries, packaging helpers and platform bindings). Their exact names/versions/licenses must be reviewed from the SBOM/wheel metadata for the release rather than copied from a developer venv into this evergreen table.

## 3. Build and packaging tools

These tools are normally used to produce BananaFlow rather than imported as application runtime dependencies, but their licenses can matter when generated bootloaders/files are distributed.

| Tool | License/handling |
|---|---|
| setuptools | MIT build backend/tooling |
| wheel | MIT build tooling |
| PyInstaller | GPLv2-or-later with bootloader exception; preserve applicable bootloader notices |
| pyinstaller-hooks-contrib | Apache-2.0 / GPLv2-style package metadata depending component; preserve included notices when relevant |
| Inno Setup | External installer compiler under its own permissive license; compiler itself is not bundled as the app |

Exact build-tool versions belong to the release environment/workflow evidence, not this evergreen license table.

## 4. Runtime external services

Network services are not “bundled dependencies”, but maintainers must review their privacy/terms impact when integration changes. The canonical desktop-app data-flow inventory is [`PRIVACY.md`](PRIVACY.md); user responsibilities are in [`docs/legal/acceptable-use.md`](docs/legal/acceptable-use.md).

Current categories include:

- GitHub Releases/update metadata via `https://api.github.com/repos/BananaFlow-Media/BananaFlow/releases/latest`;
- PyPI version checks;
- YouTube/YouTube Music;
- Spotify web pages;
- the **user-configured, self-hosted** Spotify search proxy;
- MusicBrainz and Cover Art Archive;
- optional lyrics/SponsorBlock services; and
- user-selected sites handled by yt-dlp/generic extraction.

There is currently no BananaFlow-operated public Spotify Search Proxy. If a future BananaFlow service replaces the self-hosted setting, Privacy/Security/threat-model/API documentation must be updated as part of that service launch.

## 5. Source availability and license texts

Corresponding-source handling is described by [`SOURCE_OFFER.md`](SOURCE_OFFER.md). The Windows/package license bundle includes the project license/source/notice files required by [`LICENSES.md`](LICENSES.md) and the release checklist.

Canonical license texts/sources include:

- GPL v3: https://www.gnu.org/licenses/gpl-3.0.txt
- GPL v2: https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt
- LGPL v3: https://www.gnu.org/licenses/lgpl-3.0.txt
- LGPL v2.1: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt
- Apache-2.0: https://www.apache.org/licenses/LICENSE-2.0.txt
- MIT: https://opensource.org/license/mit
- BSD-3-Clause: https://opensource.org/license/bsd-3-clause
- MPL-2.0: https://www.mozilla.org/MPL/2.0/
- HPND: https://opensource.org/license/hpnd
- Unlicense: https://unlicense.org/

Release-specific bundled components can require additional upstream notice bundles; preserve those with the artifact when required rather than assuming this summary replaces them.

## 6. Blocking release-compliance checklist

Before publishing a binary release:

- [ ] `LICENSE`, `LICENSES.md`, `NOTICE`, `SOURCE_OFFER.md` and this file are included where the packaging policy requires them.
- [ ] The release points to the matching source tag/archive.
- [ ] The generated SBOM reflects the exact frozen Python/component set.
- [ ] `requirements.txt` / release constraints and the built environment agree on the reviewed yt-dlp/EJS combination.
- [ ] The yt-dlp compatibility floor in `pyproject.toml`, YouTube Doctor and README has not drifted.
- [ ] FFmpeg/ffprobe are inspected after every restage; expected license/configuration and SHA-256 values are recorded.
- [ ] Deno version/architecture/checksum are verified after restage.
- [ ] PO Token Provider plugin/backend version matches `packaging/stage_pot_provider.py`, the source/venv optional dependency and packaging README; source-tree integrity pin is current.
- [ ] Chromium/Playwright release notices for the exact browser payload are preserved.
- [ ] GPL/LGPL/MPL/other bundled dependency obligations remain compatible with BananaFlow's GPL-3.0-or-later distribution.
- [ ] A future proprietary/closed distribution receives a separate license review; do not infer that the current GPL compatibility analysis applies.
- [ ] `bananaflow-cli --doctor` and packaged smoke tests verify the staged runtime/provider path before claiming readiness.
- [ ] `PRIVACY.md` and the threat/supply-chain docs reflect any new external service, credential, downloaded-code or staged-runtime behavior.

See [`docs/release/RELEASING.md`](docs/release/RELEASING.md) for the full release procedure.
