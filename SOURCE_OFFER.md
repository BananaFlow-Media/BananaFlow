# Source Availability

BananaFlow is licensed as GPL-3.0-or-later. This file explains how
source code is made available for source releases and packaged binary
releases.

This document is a best-effort release compliance note, not legal
advice.

## BananaFlow source

The preferred source for BananaFlow is the project repository:

https://github.com/BananaFlow-Media/BananaFlow

For a tagged release, the corresponding source is the matching Git tag
(`v<version>`, e.g. `v1.0.0-beta.1`) or the source archive GitHub
generates for that tag. It includes the application source, tests,
packaging scripts, the PyInstaller spec, the installer configuration and
the documentation needed to build, install, run and modify that release.

## Bundled third-party components

Packaged BananaFlow binaries bundle third-party components that carry
their own licenses and their own source obligations:

| Component | License | Source availability |
|---|---|---|
| FFmpeg / ffprobe | LGPL-2.1-or-later (build-dependent) | Upstream: https://ffmpeg.org — the exact build bundled with a release is recorded in `THIRD_PARTY_NOTICES.md` for that release |
| PO Token Provider (bgutil-ytdlp-pot-provider) | GPL-3.0 | Upstream: https://github.com/Brainicism/bgutil-ytdlp-pot-provider — the exact version staged into a release is pinned by content hash in `packaging/stage_pot_provider.py` and recorded in `THIRD_PARTY_NOTICES.md` |
| Deno JavaScript runtime | MIT | Upstream: https://deno.land — version recorded in `THIRD_PARTY_NOTICES.md` |
| Python dependencies (yt-dlp, PySide6, mutagen, httpx, Playwright, …) | Various (see notices) | Pinned in `requirements.txt` / `constraints-windows-py312.txt`; the per-release SBOM (`sbom.cyclonedx.json`, attached to each release) records the exact set |

## Requesting source

If you received a BananaFlow binary and cannot obtain the corresponding
source from the repository or the release page, open an issue at
https://github.com/BananaFlow-Media/BananaFlow/issues (or use the
contact routes in `SUPPORT.md`) and the maintainer will provide the
corresponding source for that release.

## Keeping this offer accurate

Release engineering keeps this bundle together in every source and
binary release: `LICENSE`, `NOTICE`, `SOURCE_OFFER.md`,
`THIRD_PARTY_NOTICES.md` and the installer-facing `LICENSES.md`. The
release checklist in `docs/release/RELEASING.md` verifies, per release,
that the versions named in `THIRD_PARTY_NOTICES.md` match what the
build actually staged.
