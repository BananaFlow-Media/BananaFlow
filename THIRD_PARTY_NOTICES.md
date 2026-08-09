# Third-Party Notices

BananaFlow is licensed as GPL-3.0-or-later. This file lists the
third-party packages, binaries, and staged release inputs that BananaFlow
uses or may bundle, along with their license status and release handling
notes.

This file is a best-effort open-source release compliance document. It
is not legal advice and does not imply that a lawyer has approved a
release.

Version reference: BananaFlow `1.0.0` (see [`version.py`](version.py)).

---

## Bundled binaries (shipped inside the Windows installer)

### FFmpeg / ffprobe — LGPL v2.1 or later

Used by yt-dlp (as a post-processor for format conversion, audio
extraction, thumbnail embedding, SponsorBlock chapter removal) and by
`core/hls_downloader.py` (direct HLS / DASH download).

The Windows EXE bundles the **LGPL-licensed** FFmpeg build only.
The release build script (`scripts/build_windows.ps1 -RequireBundledFfmpeg`)
fails unless the maintainer has staged `ffmpeg.exe` and `ffprobe.exe`
manually. It does not auto-download FFmpeg, so a wrong license cannot
ship by accident.

| Field | Value |
|---|---|
| Project | FFmpeg |
| License | LGPL v2.1 or later (some optional components are GPL — they are NOT included in the LGPL build) |
| **License of the build actually staged** | **LGPL v3** — the staged build is configured `--enable-version3`, which upgrades the effective licence to version 3. This is within "v2.1 **or later**", and LGPL v3 is compatible with BananaFlow's GPL-3.0-or-later. Convey the binaries under LGPL v3 terms. |
| Source | https://ffmpeg.org/ |
| LGPL compliance | The full LGPL text is reproduced below. The bundled binaries are unmodified; users can replace them with their own LGPL build by overwriting `ffmpeg.exe` / `ffprobe.exe` in the install folder. |
| Action required | Distribute the reviewed build, ship the relevant LGPL/GPL text and source/build link, and keep `LICENSES.md` plus this notices file in the installer. |

**Build staged for this release — verified in Phase 15 against the binary itself, not asserted:**

| Field | Value |
|---|---|
| Build | `N-124549-g1572784128-20260519` (`ffmpeg -version`) |
| Effective licence | **LGPL v3** (`--enable-version3`; **no `--enable-gpl`, no `--enable-nonfree`**) |
| GPL-only libraries | **All disabled** — `--disable-libx264 --disable-libx265 --disable-libxvid --disable-libfdk-aac --disable-libvidstab --disable-avisynth --disable-libdavs2 --disable-libxavs2 --disable-librubberband` |
| `ffmpeg.exe` SHA256 | `2cbdf99e2c5a4fbe3c653478e11cdf331f3345f732ee64d54b096c50ea7838a6` |
| `ffprobe.exe` SHA256 | `775d3fab1a272978c82af25ad9c50b020b963b45a93afd0fc7db03e0b6c5637f` |

Re-verify with `packaging/ffmpeg/ffmpeg.exe -version` whenever FFmpeg is re-staged: the absence of
`--enable-gpl` / `--enable-nonfree` is the load-bearing check that a GPL build has not been staged by
accident (`docs/release/RELEASING.md`, pre-flight).

Get the LGPL build from a trusted mirror, e.g. the BtbN
[FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) project (look
for a `*-lgpl*-shared` artifact if the release expects LGPL-only
handling). If a GPL FFmpeg build is used instead, document that choice
and satisfy the GPL source/license obligations for the release.

---

### PO Token Provider stack — GPL v3

The packaged Windows build stages a yt-dlp PO Token Provider plugin plus
the matching upstream Deno script backend, so normal users do not need
pip, Deno, Node, Docker, or manual provider setup. A PO Token is an
anti-bot attestation, not DRM circumvention; using a provider plugin is
the officially documented yt-dlp reliability path. BananaFlow itself never
generates, scrapes, stores, or injects PO Tokens — it bundles the
provider stack and passes yt-dlp the provider's official `server_home`
extractor argument.

The reference provider is
[`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
(pinned/staged as `1.3.1` for the Windows package). It is **GPL v3**, verified
against both its installed package metadata classifier and the live
PyPI JSON API (an earlier draft of this file incorrectly said MIT).

**Note on BananaFlow's own licensing:** BananaFlow itself is GPL-3.0-or-later,
so bundling a GPL v3 plugin is license-compatible for the GPL release
when the notices and source-availability information are preserved.

| Field | Value |
|---|---|
| Project | bgutil-ytdlp-pot-provider |
| License | **GPL v3** |
| Source | https://github.com/Brainicism/bgutil-ytdlp-pot-provider |
| Release handling | Preserve the GPL v3 notice, identify the staged provider version, and make corresponding source available for both the Python plugin and upstream `server/` backend. Before release, verify `bananaflow-cli --doctor` reports the full provider stack ready. |

The staged backend also includes the provider's npm dependency tree under
`server/node_modules/`, installed at build time with `npm ci --omit=dev`
from upstream `package-lock.json` by `packaging/stage_pot_provider.py`.
Treat this tree as part of the bgutil backend bundle for release review:
preserve upstream lock/manifests and record that npm dependency
notices/source metadata are available from the staged `package.json`,
`package-lock.json`, `deno.lock`, and package metadata. Normal packaged
users do not need Node or npm; Deno remains the bundled runtime used by
the provider script.

### JavaScript runtime (Deno) — staged for Windows package

The Windows package stages Deno so yt-dlp-ejs and the bgutil script
backend work on a clean machine without user-installed Deno or Node.

| Field | Value |
|---|---|
| Project | Deno |
| License | MIT |
| Source | https://github.com/denoland/deno |
| Release handling | Preserve the runtime's license notice, record the exact version/checksum in the release checklist, and confirm the binary is unmodified and the correct OS/arch for the build. |

**Runtime staged for this release — verified in Phase 15 by running the binary:**

| Field | Value |
|---|---|
| Version | **deno 2.9.1** (stable, release, `x86_64-pc-windows-msvc`) — v8 14.9.207.2, TypeScript 6.0.3 |
| `deno.exe` SHA256 | `3819117e301d48a6931f9a1a4fb5e4a10c464163189bcff8fce5d75025d6f2a0` |
| OS / arch | Windows x86_64 — matches the `ArchitecturesAllowed=x64compatible` package |

Re-verify with `packaging/runtime/deno.exe --version` whenever the runtime is re-staged
(`scripts/fetch_deno_runtime.ps1`).

---

## Dependency license inventory

Version references combine declared project requirements and the versions
observed in the local release-prep virtual environment during this
review. Frozen builds may include a subset of these packages depending
on PyInstaller analysis.

| Dependency | Version reference | License | Source of license info | GPLv3 compatible | Notice / release handling |
|---|---:|---|---|---|---|
| yt-dlp[default] | declared >=2026.6.9; observed 2026.7.4 | Unlicense | package metadata / upstream repository | yes | Preserve upstream notice; yt-dlp release binaries have their own GPL notes, but the Python package/wheel is Unlicense. |
| yt-dlp-ejs | observed 0.8.0 | Unlicense AND MIT AND ISC | package metadata / upstream repository | yes | Preserve Unlicense/MIT/ISC notices for the bundled JS solver data. |
| bgutil-ytdlp-pot-provider | pinned/staged 1.3.1 for Windows package | GPL v3 | PyPI classifier / upstream repository | yes | Preserve GPL notice/source link for both plugin and upstream `server/` backend; Doctor must verify bundled script backend health. |
| bgutil backend npm dependency tree | staged from upstream provider locks/manifests | mixed permissive/GPL-compatible package metadata; review before release | staged `package.json`, `package-lock.json`, `deno.lock`, package metadata | expected yes for GPL release, verify during release review | Packaged under `pot-provider-backend/.../server/node_modules`; preserve upstream manifests/locks and review any npm audit/license changes before publishing. |
| Deno | staged 2.9.1 | MIT | upstream license / staged binary version | yes | Required for the packaged bgutil script backend; record version and checksum before release. |
| FFmpeg / ffprobe | staged build when present | LGPL 2.1-or-later or GPL depending on configure flags | `ffmpeg -version` / upstream legal docs | yes | Bundle only a license-reviewed build; preserve license text and source/build link. |
| ffmpeg-python | declared >=0.2.0; observed 0.2.0 | Apache-2.0 | package classifier | yes | Pure-Python binding; FFmpeg binary obligations are separate. |
| pyloudnorm | declared >=0.1.1; observed 0.2.0 | MIT | upstream/package metadata | yes | Preserve copyright/license notice. |
| soundfile | declared >=0.12.1; observed 0.14.0 | BSD-3-Clause | package metadata | yes | Preserve copyright/license notice; includes native audio-library considerations through wheels. |
| mutagen | declared >=1.47.0; observed 1.48.1 | GPL-2.0-or-later | package metadata / upstream COPYING | yes | Compatible with a GPL-3.0-or-later BananaFlow release. Future proprietary distribution would need review, replacement, or another rights path. |
| syncedlyrics | declared >=0.4.0; observed 1.0.1 | MIT | package metadata | yes | Disabled by default; preserve notice if bundled. |
| send2trash / Send2Trash | declared >=1.8.0; observed 2.1.0 | BSD-3-Clause | package metadata | yes | Preserve copyright/license notice. |
| PySide6 | declared >=6.8.0; observed 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | package metadata / Qt for Python docs | yes | Preserve LGPL/GPL notices and user replaceability of installed shared libraries. |
| PySide6_Addons | observed 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | package metadata | yes | Transitive PySide6 package; same Qt notice handling. |
| PySide6_Essentials | observed 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | package metadata | yes | Transitive PySide6 package; same Qt notice handling. |
| shiboken6 | observed 6.11.1 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | package metadata | yes | Transitive PySide6 package; same Qt notice handling. |
| PySide6-Fluent-Widgets | declared >=1.11.0; observed 1.11.2 | GPL v3 / commercial dual license | package metadata / upstream docs | yes for GPL release | Community package is GPLv3/non-commercial per published metadata/docs. Future commercial/proprietary use may require a commercial license or replacement. |
| PySideSix-Frameless-Window | observed 0.8.1 | LGPL v3 | package metadata | yes | Transitive through Fluent Widgets; preserve LGPL notice. |
| requests | declared >=2.31.0; observed 2.34.2 | Apache-2.0 | package metadata | yes | Preserve license notice. |
| httpx | declared >=0.27.0; observed 0.28.1 | BSD-3-Clause | package metadata | yes | Preserve license notice. |
| beautifulsoup4 | declared >=4.12.0; observed 4.15.0 | MIT | package metadata | yes | Preserve license notice. |
| lxml | declared >=5.2.0; observed 6.1.1 | BSD-3-Clause | package metadata | yes | Preserve license notice; includes libxml2/libxslt-style notices in wheels. |
| ytmusicapi | declared >=1.7.0; observed 1.12.1 | MIT | package metadata / upstream repository | yes | Preserve license notice. |
| Pillow | declared >=10.3.0; observed 11.3.0 | MIT-CMU / HPND-style | package metadata / upstream LICENSE | yes | Preserve license notice. |
| python-dotenv | declared >=1.0.1; observed 1.2.2 | BSD-3-Clause | package metadata | yes | Preserve license notice. |
| certifi | declared >=2025.1.1; observed 2026.6.17 | MPL-2.0 | package metadata / upstream LICENSE | yes | Preserve MPL notice; unmodified CA bundle. |
| keyboard | declared >=0.13.5 on Windows; observed 0.13.5 | MIT | package metadata | yes | Optional Windows hotkey dependency; preserve notice if bundled. |
| playwright | declared >=1.42.0; observed 1.61.0 | Apache-2.0 | package metadata / upstream LICENSE | yes | Python bindings only; Chromium browser license inventory is separate. |
| pytest | dev >=8.0; observed 9.0.3 | MIT | package metadata | yes | Dev/test only; not intended for release bundle. |
| pytest-mock | dev >=3.12; observed 3.15.1 | MIT | package metadata | yes | Dev/test only; not intended for release bundle. |

### Additional transitive packages observed in the development venv

These packages are pulled by runtime dependencies, build tooling, or
tests. Frozen builds should preserve their wheel license files where
PyInstaller includes them.

| Package | Observed version | License |
|---|---:|---|
| altgraph | 0.17.5 | MIT |
| anyio | 4.13.0 | MIT |
| brotli | 1.2.0 | MIT |
| cffi | 2.0.0 | MIT |
| charset-normalizer | 3.4.7 | MIT |
| colorama | 0.4.6 | BSD |
| darkdetect | 0.8.0 | BSD-3-Clause |
| decorator | 5.3.1 | BSD-2-Clause |
| future | 1.0.0 | MIT |
| greenlet | 3.5.1 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| idna | 3.17 | BSD-3-Clause |
| ImageIO / imageio | 2.37.3 | BSD-2-Clause |
| imageio-ffmpeg | 0.6.0 | BSD-2-Clause |
| iniconfig | 2.3.0 | MIT |
| numpy | 2.4.6 | BSD-3-Clause and bundled permissive notices |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| pefile | 2024.8.26 | MIT |
| pip | 26.1.2 | MIT |
| pluggy | 1.6.0 | MIT |
| proglog | 0.1.12 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pycryptodomex | 3.23.0 | BSD / public domain |
| pyee | 13.0.1 | MIT |
| Pygments | 2.20.0 | BSD-2-Clause |
| pywin32 | 311 | PSF |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| RapidFuzz | 3.14.5 | MIT |
| scipy | 1.17.1 | BSD-3-Clause plus bundled OpenBLAS/LAPACK/GCC runtime exception notices |
| soupsieve | 2.8.4 | MIT |
| tqdm | 4.67.3 | MPL-2.0 AND MIT |
| typing_extensions | 4.15.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |
| websockets | 16.0 | BSD-3-Clause |

### Build and packaging tools

| Tool | Observed version | License | Release handling |
|---|---:|---|---|
| setuptools | 82.0.1 | MIT | Build backend; not a runtime requirement except package metadata handling. |
| wheel | declared build dependency | MIT | Build tool; preserve only if bundled, which is not intended. |
| PyInstaller | 6.20.0 | GPLv2-or-later with bootloader exception | Build tool; bootloader exception permits bundling. Preserve PyInstaller notices if the bootloader is shipped. |
| pyinstaller-hooks-contrib | 2026.5 | Apache-2.0 / GPLv2 | Build helper; preserve notices if any hook code is bundled. |
| Inno Setup | 6.x external tool | custom permissive license | Installer compiler, not bundled into the app. |
| Playwright Chromium browsers | release-specific | BSD-3-Clause (Chromium) plus many component licenses | If bundled, include Chromium's license inventory or generated browser notices with the release artifact. |

---

## Runtime Python dependencies summary

This shorter table is retained as a quick packaging summary. The fuller
inventory above is the source of truth for versions, source references,
and GPLv3 compatibility notes.

| Dependency | License | Bundled by EXE | Note |
|---|---|---|---|
| yt-dlp[default] | Unlicense (public domain) | yes | Permissive, no obligations. |
| yt-dlp-ejs | Unlicense | yes | Same. |
| ffmpeg-python | Apache 2.0 | yes | Pure-Python bindings; only the bundled FFmpeg binary has the LGPL/GPL concern. |
| pyloudnorm | MIT | yes | |
| soundfile | BSD-3-Clause | yes | |
| **mutagen** | **GPL v2.0 or later** | yes | Compatible with a GPL-3.0-or-later BananaFlow release; future proprietary distribution would need review, replacement, or another rights path. |
| syncedlyrics | MIT | yes | Disabled by default. |
| **PySide6** | **LGPL v3** (with Qt commercial alternative) | yes | LGPL is satisfied by Qt's dynamic-link model + the LGPL v3 text in this file. PySide6 wheels are distributed under LGPL v3. No commercial Qt license is required for this usage. |
| **PySide6-Fluent-Widgets** | **GPL v3 / Commercial dual-license** | yes | Compatible with the GPL BananaFlow release. Published metadata/docs describe the community package as GPLv3/non-commercial; future commercial/proprietary use may require a commercial license or replacement. |
| PySideSix-Frameless-Window | LGPL v3 | yes, transitively via PySide6-Fluent-Widgets | Transitive dependency of Fluent Widgets; not imported directly by this app. Treat like other LGPL v3 Qt-adjacent components: ship the LGPL v3 text and preserve user replaceability of the installed files. |
| requests | Apache 2.0 | yes | |
| httpx | BSD-3-Clause | yes | |
| beautifulsoup4 | MIT | yes | |
| lxml | BSD-3-Clause | yes | Includes libxml2 / libxslt (MIT / similar). |
| ytmusicapi | MIT | yes | |
| Pillow | HPND (MIT-style) | yes | |
| python-dotenv | BSD-3-Clause | yes | |
| certifi | MPL 2.0 | yes | File-level copyleft; satisfied by shipping unmodified. |
| keyboard | MIT | yes | Optional — graceful fallback when missing. |
| playwright | Apache 2.0 | yes | Python bindings only. |
| Playwright Chromium browsers | BSD-3-Clause (Chromium) + several others | yes | Chromium, FFmpeg, and WinLDD browser binaries are bundled directly with the application (~300-400 MB) for full offline capabilities. |

---

## Public-API endpoints used at runtime

These are network services queried by the app. Their terms of service
apply to the end-user, not the BananaFlow binary, but maintainers should
be aware:

| Endpoint | Purpose | Notes |
|---|---|---|
| `https://api.github.com/repos/BananaFlow-Media/BananaFlow/releases/latest` | Update checker | Rate-limited to 60 req/hour for unauthenticated clients; called once at startup. |
| `https://musicbrainz.org/ws/2/...` | MusicBrainz tag enrichment | Public API; the User-Agent identifies the app per MB policy. Throttled to 1 req/sec by the client. |
| YouTube web + ytmusic web | Search / playlist / channel scraping | Subject to YouTube's Terms of Service. The user is responsible for downloading only content they have the rights to access. |
| `https://open.spotify.com/...` + optional self-hosted proxy | Spotify metadata resolution | Spotify metadata is fetched read-only; downloads use yt-dlp against the matched YouTube source, not the Spotify DRM stream. The user must comply with Spotify's Terms of Service for any data they collect. |

---

## License-text appendix

The full text of the licenses referenced above is available from the
canonical sources:

- **LGPL v2.1**: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt
- **LGPL v3**:   https://www.gnu.org/licenses/lgpl-3.0.txt
- **GPL v2**:    https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt
- **GPL v3**:    https://www.gnu.org/licenses/gpl-3.0.txt
- **Apache 2.0**: https://www.apache.org/licenses/LICENSE-2.0.txt
- **MIT**:        https://opensource.org/licenses/MIT
- **BSD-3-Clause**: https://opensource.org/licenses/BSD-3-Clause
- **MPL 2.0**:    https://www.mozilla.org/en-US/MPL/2.0/
- **HPND**:       https://opensource.org/licenses/HPND
- **Unlicense**:  https://unlicense.org/

The Windows installer shows `LICENSES.md` as its license page and also
installs this file, `LICENSE`, `NOTICE`, and `SOURCE_OFFER.md`. For a
binary release, keep this appendix aligned with the bundled components
and include any release-specific license bundles generated by FFmpeg,
Chromium/Playwright, Deno, or other staged binaries.

---

## Pre-release checklist for the project maintainer

Before publishing a binary release, confirm each release-compliance item:

- [ ] `LICENSE`, `LICENSES.md`, `NOTICE`, `SOURCE_OFFER.md`, and this file are included with the installer/portable build.
- [ ] The release page links to the matching source tag or source archive.
- [ ] FFmpeg binaries bundled are confirmed to match the intended LGPL/GPL status and are not accidentally a GPL build when the checklist expects LGPL.
- [ ] LGPL/GPL license texts and source links for FFmpeg/ffprobe are accessible to end users.
- [ ] Mutagen GPL-2.0-or-later and PySide6-Fluent-Widgets GPLv3 status are acceptable because BananaFlow itself is GPL-3.0-or-later.
- [ ] Future proprietary or closed-source commercial distribution has a separate review path for Mutagen, PySide6-Fluent-Widgets, Qt/PySide6, and any GPL staged provider.
- [ ] The bgutil-ytdlp-pot-provider plugin, matching Deno script backend, and bundled Deno runtime versions are recorded, and source availability points to the matching upstream provider version.
- [ ] Do not claim PO Token Provider readiness unless YouTube Doctor and a packaged-build smoke test confirm the bundled provider path is healthy.
- [ ] Privacy notice covers clipboard monitor, cookies file reading, local history database, and update checks.
