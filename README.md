# BananaFlow

[![Website](https://img.shields.io/badge/website-bananaflow.bananaflow--media.workers.dev-1F1F1F.svg)](https://bananaflow.bananaflow-media.workers.dev/)
[![Tests](https://github.com/BananaFlow-Media/BananaFlow/actions/workflows/tests.yml/badge.svg)](https://github.com/BananaFlow-Media/BananaFlow/actions/workflows/tests.yml)
[![Documentation](https://github.com/BananaFlow-Media/BananaFlow/actions/workflows/documentation.yml/badge.svg)](https://github.com/BananaFlow-Media/BananaFlow/actions/workflows/documentation.yml)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Status: Stable](https://img.shields.io/badge/status-Stable-success.svg)](CHANGELOG.md)

**Download, convert, tag — YouTube, YouTube Music, and Spotify workflows in one app.**

**Official website: <https://bananaflow.bananaflow-media.workers.dev/>** — downloads, Help, FAQ and support in [English](https://bananaflow.bananaflow-media.workers.dev/en/) and [Hebrew](https://bananaflow.bananaflow-media.workers.dev/he/). This repository is the application source; the website is maintained separately.

BananaFlow is a Windows-first desktop application for downloading audio/video from **YouTube** and **YouTube Music**, resolving **Spotify metadata to separate downloadable sources**, converting local media, and managing music metadata with a full batch **Tag Editor**. Hebrew is a first-class RTL interface alongside English.

## Download

Use the official [download page](https://bananaflow.bananaflow-media.workers.dev/en/download/) ([Hebrew](https://bananaflow.bananaflow-media.workers.dev/he/download/)) or this repository's [GitHub Releases](https://github.com/BananaFlow-Media/BananaFlow/releases). Release assets include published SHA-256 checksums and an SBOM.

> **Signing:** current Windows binaries are not Authenticode-signed. SmartScreen may warn on first run. macOS packaged support is experimental and its current signing/notarization status is stated in release notes. See [`SECURITY.md`](SECURITY.md) before running an artifact from an unfamiliar source.

## Features

- **Download engine** — yt-dlp-based URL/collection handling with bounded retries, duplicate handling, batch progress and a conservative YouTube reliability mode.
- **Search** — YouTube Music and YouTube; optional proxy-backed Spotify **text search**.
- **Spotify URL workflows** — pasted track/album/playlist/artist URLs are read through BananaFlow's metadata scraper/resolver; the optional search proxy is not required for URL import.
- **Tag Editor** — proposal-first batch editing, Review, Undo/Redo, verified backup/journal/recovery, artwork, lyrics, ReplayGain, MusicBrainz/Cover Art, duplicates, actions/templates/workflows, CSV/reports/playlists and safe file operations.
- **Converter** — local audio/video conversion with format-aware presets and output verification.
- **Cookie Wizard** — isolated BananaFlow-owned browser profile for YouTube sign-in when authenticated access is genuinely required.
- **YouTube Doctor** — local diagnostics for yt-dlp/runtime/cookie/provider/reliability readiness (`bananaflow-cli --doctor`).
- **History** — searchable SQLite-backed completed-download history.
- **Bilingual / accessible UI** — English + Hebrew RTL, keyboard/screen-reader/high-DPI/touch-aware behavior.
- **CLI** — headless download/list/diagnostic access to the shared backend.

Complete user references:

- [English User Manual](docs/user-guide/user-manual.md)
- [מדריך משתמש בעברית](docs/user-guide/user-guide-he.md)
- [CLI Reference](docs/user-guide/cli.md)

## Source-development requirements

| Requirement | Policy |
|---|---|
| Python | 3.10 or newer |
| FFmpeg | Recent version on `PATH` for source media operations |
| yt-dlp | Compatibility floor **≥ 2026.7.4**; reproducible release/test installs use the reviewed exact version from `requirements.txt` / release constraints |
| Playwright Chromium | Required for source workflows that use browser-backed Spotify/channel/generic features |

Recommended editable development setup:

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
python main.py
python cli.py --help
```

`pyproject.toml` owns the source-install compatibility floor. `requirements.txt` and release constraints can intentionally pin a newer reviewed yt-dlp build for reproducible CI/release packaging; those are different jobs and must not be forced to the same version string.

## Building distributable packages

- **Windows:** `scripts/build_windows.ps1` produces the application folder/portable package and release inputs; `packaging/bananaflow.iss` defines the Inno Setup installer.
- **macOS:** `scripts/build_macos.sh` produces the current Apple Silicon app/DMG path.

Bundled component staging, license/source review, SBOM/checksums and manual acceptance are defined by [`docs/release/RELEASING.md`](docs/release/RELEASING.md) and [`docs/security/supply-chain.md`](docs/security/supply-chain.md).

## Testing

Focused tests are useful while iterating, but the supported full-suite gate runs each test file in a fresh interpreter:

```bash
python scripts/run_isolated_tests.py
```

Every repository change also has a documentation consistency gate:

```bash
python scripts/check_documentation.py
```

See [`docs/testing/TESTING.md`](docs/testing/TESTING.md) for deterministic tests, real-network checks, manual QA and platform notes.

## Documentation and AI contributors

[`docs/README.md`](docs/README.md) is the documentation map. [`docs/DOCUMENTATION_POLICY.md`](docs/DOCUMENTATION_POLICY.md) defines sources of truth and the **Code → Documentation impact map** so behavior changes update the relevant Markdown in the same PR.

AI coding agents start at [`AGENTS.md`](AGENTS.md), which routes them through [`docs/AI_CONTEXT.md`](docs/AI_CONTEXT.md) and subsystem-specific instructions before they edit code. Claude/Gemini/Copilot adapter files deliberately point to that same canonical context rather than maintaining competing copies.

Key contributor references:

| Document | Purpose |
|---|---|
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development workflow and contribution rules |
| [`docs/testing/TESTING.md`](docs/testing/TESTING.md) | Test strategy and supported gates |
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | Current architecture/trust/data-flow overview |
| [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) | Detailed module map |
| [`SECURITY.md`](SECURITY.md) | Security support/reporting/distribution guidance |
| [`PRIVACY.md`](PRIVACY.md) | Desktop-app network/local-data behavior |
| [`docs/security/threat-model.md`](docs/security/threat-model.md) | Security assets/trust boundaries/residual risk |
| [`docs/release/RELEASING.md`](docs/release/RELEASING.md) | Maintainer release procedure |

## Supported platforms

| Platform | Status |
|---|---|
| **Windows 10/11 x64** | Primary supported packaged target — installer + portable package |
| **macOS Apple Silicon** | Experimental packaged support; see current release notes |
| **Linux** | Source/developer use unless a release explicitly says otherwise |

## License, third parties and brand

BananaFlow source code is **GPL-3.0-or-later** — see [`LICENSE`](LICENSE). Third-party dependencies/binaries and corresponding-source handling are documented in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md), [`SOURCE_OFFER.md`](SOURCE_OFFER.md) and the per-release SBOM.

The source-code license does not by itself make an unrelated product an official BananaFlow release. See [`TRADEMARKS.md`](TRADEMARKS.md) and [`packaging/BRAND_ASSETS.md`](packaging/BRAND_ASSETS.md) for the project-brand distinction.

Use BananaFlow only for material you are entitled to access/download/store. See [`docs/legal/acceptable-use.md`](docs/legal/acceptable-use.md). BananaFlow is independent of Google/YouTube, Spotify and other supported third-party services; their trademarks and service terms remain their own.

## Support and contributing

- Usage: official website [Help](https://bananaflow.bananaflow-media.workers.dev/en/help/) / [FAQ](https://bananaflow.bananaflow-media.workers.dev/en/faq/)
- Bugs/features: [Issues](https://github.com/BananaFlow-Media/BananaFlow/issues)
- Questions: [`SUPPORT.md`](SUPPORT.md)
- Security vulnerabilities: [`SECURITY.md`](SECURITY.md) — never disclose an unpatched vulnerability in a public issue
- Community standards: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- Governance: [`GOVERNANCE.md`](GOVERNANCE.md) / [`MAINTAINERS.md`](MAINTAINERS.md)
