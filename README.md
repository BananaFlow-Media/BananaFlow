# BananaFlow

[![Website](https://img.shields.io/badge/website-bananaflow.bananaflow--media.workers.dev-1F1F1F.svg)](https://bananaflow.bananaflow-media.workers.dev/)
[![Tests](https://github.com/BananaFlow-Media/BananaFlow/actions/workflows/tests.yml/badge.svg)](https://github.com/BananaFlow-Media/BananaFlow/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Status: Stable](https://img.shields.io/badge/status-Stable-success.svg)](CHANGELOG.md)

**Download, convert, tag — YouTube, YouTube Music, and Spotify in one app.**

**Official website: <https://bananaflow.bananaflow-media.workers.dev/>** —
downloads, help, FAQ and support, in
[English](https://bananaflow.bananaflow-media.workers.dev/en/) and
[Hebrew](https://bananaflow.bananaflow-media.workers.dev/he/). This
repository is the application's source; the website is BananaFlow's
public front door and is maintained as a separate project.

BananaFlow is a desktop application for downloading audio and video from
**YouTube**, **YouTube Music**, and **Spotify**, with a full batch
**Tag Editor** and a real format **Converter** built in — Windows-first,
with full **Hebrew (RTL)** support alongside English.

Most downloaders stop at "download a file." BananaFlow adds what happens
*after* that: a proposal-first batch Tag Editor (undo/redo, MusicBrainz
lookup, ReplayGain, artwork), a Converter that is more than a wrapper
around one `ffmpeg` call, Spotify→YouTube matching with no Spotify API
key required, and a UI that is genuinely bilingual — Hebrew is a
first-class, fully mirrored RTL layout, not a translated afterthought.

## Download

The official download page is
**<https://bananaflow.bananaflow-media.workers.dev/en/download/>**
([Hebrew](https://bananaflow.bananaflow-media.workers.dev/he/download/)) —
it picks the right package for your platform and links the matching
checksum.

The files themselves are the GitHub Release assets: Windows installer,
Windows portable ZIP and a macOS DMG are published on the
**[Releases page](https://github.com/BananaFlow-Media/BananaFlow/releases)**
with SHA-256 checksums and an SBOM.

> **Note:** the binaries are currently **unsigned**. Windows SmartScreen
> will warn on first run, and macOS Gatekeeper requires
> right-click → Open the first time. [`SECURITY.md`](SECURITY.md)
> explains how to verify a download against its published checksum.

## Features at a glance

* **Download engine** — yt-dlp-based, with retries, rate-limit
  politeness, parallel downloads (1–6), playlist/album/discography and
  channel support, and a conservative YouTube reliability mode.
* **Search** — YouTube Music, YouTube and Spotify search built in.
* **Tag Editor** — batch metadata editing with previews, undo/redo,
  guarded apply, JSON backups and restore, MusicBrainz enrichment,
  ReplayGain, artwork handling, CSV import/export and action presets.
* **Converter** — audio/video format conversion with per-format quality
  presets and a verification pass on every output.
* **Cookie Wizard** — sign in to YouTube through an isolated Playwright
  browser profile; the app never touches your real browser's cookies.
* **YouTube Doctor** — one-click diagnostics for the whole download
  stack (`bananaflow-cli --doctor` from the command line).
* **History** — searchable download history with per-item actions.
* **Bilingual UI** — English and Hebrew with complete RTL mirroring.
* **CLI** — `bananaflow-cli` covers headless downloading, listing and
  diagnostics.

The complete guide — installation, every panel, settings, file
organization, authentication, troubleshooting — is the
**[User Manual](docs/user-guide/user-manual.md)**. A Hebrew user guide is
available at [docs/user-guide/user-guide-he.md](docs/user-guide/user-guide-he.md).

## Requirements (source install)

| Requirement | Version |
|---|---|
| Python | 3.10 or newer |
| FFmpeg | Any recent version (on `PATH`) |
| yt-dlp | ≥ 2026.6.9 (installed via `requirements.txt`) |
| Playwright | Latest (for Spotify / channel scraping) |

```bash
pip install -r requirements.txt
python -m playwright install chromium   # or scripts/install_playwright.ps1
python main.py                          # GUI
python cli.py --help                    # CLI
```

## Building distributable packages

* **Windows** — `scripts/build_windows.ps1` produces `dist/bananaflow/`
  (portable folder), a portable ZIP and checksums; then
  `iscc packaging\bananaflow.iss` builds the installer.
* **macOS** — `scripts/build_macos.sh` produces `dist/BananaFlow.app`
  and a DMG (arm64).

Details, including how bundled components (FFmpeg, Deno runtime,
PO Token Provider) are staged, are in the
[User Manual](docs/user-guide/user-manual.md) and
[docs/release/RELEASING.md](docs/release/RELEASING.md).

## Running tests

```bash
python scripts/run_isolated_tests.py    # fresh process per test file
```

`pytest tests/` in a single process is not the supported entry point on
Windows — see [CONTRIBUTING.md](CONTRIBUTING.md) for why, and for the
full development workflow.

## Documentation

| Document | Purpose |
|---|---|
| [Official website](https://bananaflow.bananaflow-media.workers.dev/) | Downloads, help, FAQ, support, privacy and terms |
| [Website help](https://bananaflow.bananaflow-media.workers.dev/en/help/) · [FAQ](https://bananaflow.bananaflow-media.workers.dev/en/faq/) | End-user help pages (English / [Hebrew](https://bananaflow.bananaflow-media.workers.dev/he/help/)) |
| [User Manual](docs/user-guide/user-manual.md) | Complete usage guide |
| [Hebrew user guide](docs/user-guide/user-guide-he.md) | מדריך למשתמש בעברית |
| [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) | Code layout for contributors |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and policies |
| [SECURITY.md](SECURITY.md) | Security policy and download verification |
| [PRIVACY.md](PRIVACY.md) | What the app does and doesn't send |
| [docs/legal/acceptable-use.md](docs/legal/acceptable-use.md) | Lawful-use responsibilities |
| [docs/architecture/](docs/architecture/) | Design decisions and safety invariants |
| [docs/release/RELEASING.md](docs/release/RELEASING.md) | Release process |

## Supported platforms

| Platform | Status |
|---|---|
| **Windows 10/11 (x64)** | Primary target — installer + portable ZIP |
| **macOS (Apple Silicon)** | Experimental — DMG, unsigned/un-notarized |
| **Linux** | Source install only, unsupported |

## License and source availability

BananaFlow is licensed under **GPL-3.0-or-later** — see
[LICENSE](LICENSE). Binary packages bundle third-party components
(FFmpeg, a Deno runtime, a PO Token Provider, Chromium via Playwright);
their licenses and the corresponding source availability are documented
in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[SOURCE_OFFER.md](SOURCE_OFFER.md).

Downloading content you do not have the right to download may violate
the law and the terms of the services involved. You are responsible for
your own use — see [docs/legal/acceptable-use.md](docs/legal/acceptable-use.md).

> **Disclaimer**: *BananaFlow is an independent open-source desktop application. It is not affiliated with, authorized, or endorsed by Spotify or Google/YouTube. Spotify link resolution operates by searching and downloading matching public audio streams from YouTube; BananaFlow does not download audio files directly from Spotify servers or circumvent Spotify DRM. All trademarks belong to their respective owners.*

## Contributing and support

* Using the app → the website's
  [Help](https://bananaflow.bananaflow-media.workers.dev/en/help/) and
  [FAQ](https://bananaflow.bananaflow-media.workers.dev/en/faq/) pages
* Bugs and feature requests → [Issues](https://github.com/BananaFlow-Media/BananaFlow/issues)
* Questions and help → [SUPPORT.md](SUPPORT.md) and the website's
  [Support page](https://bananaflow.bananaflow-media.workers.dev/en/support/)
* Security reports → [SECURITY.md](SECURITY.md) (never open a public issue for a vulnerability)
* Development workflow → [CONTRIBUTING.md](CONTRIBUTING.md); community
  standards → [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md);
  project governance → [GOVERNANCE.md](GOVERNANCE.md)
