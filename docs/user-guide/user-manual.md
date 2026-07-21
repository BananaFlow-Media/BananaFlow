# BananaFlow — User Manual

The complete guide to installing, running, and using BananaFlow: the
desktop app for downloading, converting and tagging audio and video from
YouTube, YouTube Music and Spotify. For a quick overview, start at the
repository [README](../../README.md).

---

## Features at a Glance

| Feature | Details |
|---|---|
| **Platforms** | YouTube, YouTube Music, Spotify (tracks, albums, playlists, artists), generic HLS/DASH streams |
| **Audio formats** | MP3, M4A, FLAC, OPUS |
| **Audio quality** | Codec-aware presets: MP3 320/256/192/128 kbps, M4A/AAC, Opus, and FLAC source quality |
| **Video formats** | MP4: Auto best available, 2160p, 1440p, 1080p, 720p, 480p, 360p, or smallest file |
| **Batch downloads** | Up to 6 parallel threads, configurable |
| **GUI + CLI** | Full desktop UI + headless `bananaflow-cli` command |
| **Search** | YouTube videos, YouTube Music (songs/albums/artists/playlists), Spotify proxy |
| **History** | SQLite-backed download log with full-text search and CSV export |
| **Tag Editor** | Batch metadata editing with proposal-first edits, Review Changes, undo/redo, per-file exclusion, artwork, lyrics, ReplayGain, templates, a Problems centre, duplicate detection, MusicBrainz/Cover Art review, CSV import/export, reports, M3U playlists — see below |
| **Tag safety** | Nothing is written until Apply; every Apply is preceded by a validated backup and a durable journal, each file is written via a verified temp copy, and external changes never overwrite a pending edit |
| **Post-processing** | Lyrics embed, ReplayGain, MusicBrainz tag enrichment, square thumbnail crop |
| **Reliability** | YouTube Conservative Mode, retry-with-backoff for transient errors only, YouTube Doctor diagnostics, SponsorBlock |
| **Themes** | Dark, Light + custom accent colour |
| **Languages** | English, Hebrew (RTL layout) |
| **Auto-update** | GitHub Releases checker on startup |

---

## Tag Editor

An offline batch metadata editor for music already on disk. It never touches the
network unless you explicitly ask it to.

**Editing scope vs Apply scope.** Row selection decides what an *editing* action
(Auto-Arrange, cleanup, templates, bulk edits) operates on. Apply operates on
every file that has a real pending change — searching, filtering or navigating
never changes what gets written. The toolbar always shows how many files Apply
will affect before you press it.

**Nothing is written until Apply.** Every action produces a *proposal*. Review
Changes lists every pending change with its field, previous value, proposed
value and where it came from; undo/redo work on proposals, not on disk. A
changed file you are not ready to write can be explicitly excluded — it stays
pending, visible and reviewable, and it does not hold up the rest of the batch.

**External changes.** The active folder is monitored. A file changed outside the
app refreshes on its own if you have no pending edit for it; if you do, your
proposal is never discarded or silently overwritten — the file is blocked from
Apply until you choose Review Differences, Reload From Disk, Keep Local
Proposals (allowed only when nothing conflicts field-for-field), Remove Missing
or Locate Moved. A folder that briefly disappears (network share, USB, cloud
mount) removes nothing. Manual Refresh is always available.

**Also included:** artwork, lyrics and ReplayGain as reviewable actions; a
Problems centre; duplicate detection with confidence tiers; MusicBrainz and
Cover Art Archive lookups that propose rather than overwrite; CSV import with a
dry run; change/problem reports; M3U/M3U8 export; and action-preset transfer.

**Accessibility.** Keyboard-complete, screen-reader labelled, no state carried by
colour alone, Hebrew RTL with technical values (paths, URLs, codecs, ISRC) kept
LTR, and usable from 100% to 200% display scaling.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | 3.10 or newer |
| FFmpeg | Any recent version (must be on `PATH`) |
| yt-dlp | ≥ 2026.6.9 |
| Playwright | Latest (for Spotify / Channel scraping) |

## License And Source Availability

BananaFlow's own code is licensed under **GPL-3.0-or-later**. This is a
best-effort open-source release compliance implementation, not formal
legal advice or a lawyer-approved release sign-off.

Source releases and packaged binaries should keep the license and notice
bundle together: `LICENSE`, `NOTICE`, `SOURCE_OFFER.md`,
`THIRD_PARTY_NOTICES.md`, and the installer-facing `LICENSES.md`. See
`CONTRIBUTING.md` for the no-CLA/no-DCO contribution policy and
inbound=outbound licensing.

Third-party dependency notes are tracked in `THIRD_PARTY_NOTICES.md`.
In particular, PySide6-Fluent-Widgets is documented as GPLv3 /
commercial dual-license, with the community package described as
GPLv3/non-commercial in its published metadata/docs. That is acceptable
for the current GPLv3 BananaFlow release, but future commercial/proprietary
plans need review, a commercial license, or replacement.

Security reports, local-data behavior, network services, cookie deletion, and
lawful-use responsibilities are documented in [`SECURITY.md`](../../SECURITY.md),
[`PRIVACY.md`](../../PRIVACY.md), and
[`docs/legal/acceptable-use.md`](../legal/acceptable-use.md). Never
post credentials, cookies, or unredacted private diagnostics in a public Issue.

### Installation

1. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install browser binaries (required for Spotify / channel scraping):**

   **Windows:**
   ```bash
   install_playwright.bat
   ```

   **macOS / Linux:**
   ```bash
   python3 -m playwright install chromium
   ```

### Install FFmpeg

**Windows (via Chocolatey):**
```bash
choco install ffmpeg
```

**Windows (manual):** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to `PATH`.

**macOS (via Homebrew):**
```bash
brew install ffmpeg
```
*(The packaged macOS `.app` already bundles FFmpeg, so this is only needed when running from source.)*

---

## Building Distributable Apps

The app ships as a self-contained bundle for each OS, with FFmpeg and the
Playwright Chromium browser included — end users need **no Python install**.

### Windows (`dist/bananaflow/` + installer)
```powershell
pwsh scripts/build_windows.ps1            # portable folder + ZIP
iscc packaging/bananaflow.iss                 # optional Inno Setup installer
```

### macOS (`dist/BananaFlow.app` + DMG)
Must be run **on macOS** (Apple Silicon / arm64):
```bash
chmod +x scripts/build_macos.sh
./scripts/build_macos.sh                  # → dist/BananaFlow.app + dist/BananaFlow-v<ver>-macos-arm64.dmg
```
Both builds are also produced automatically by GitHub Actions on a `vX.Y.Z`
tag (`.github/workflows/release-windows.yml` and `release-macos.yml`).

> **macOS Gatekeeper (unsigned build):** This project is not notarized
> (no paid Apple Developer account), so the first launch is blocked by
> Gatekeeper. To open it:
> 1. **Right-click** (or Control-click) `BananaFlow.app` → **Open**.
> 2. Click **Open** again in the dialog.
> macOS remembers the choice; subsequent launches work normally. If the
> app was downloaded via a browser you can also clear the quarantine flag:
> `xattr -dr com.apple.quarantine /Applications/BananaFlow.app`.

---

## Portable vs. Installer (Windows)

Both are built from the exact same `dist/bananaflow/` output — there is no
feature difference between them.

| | Portable ZIP | Installer (`.exe`) |
|---|---|---|
| Setup | Extract anywhere, run `bananaflow.exe` | Standard Windows install wizard |
| Start Menu / desktop shortcuts | No (create your own) | Yes, optional |
| Uninstaller | No — just delete the folder | Yes, via Windows "Apps" settings |
| Writes outside its own folder | No | Program Files entry + uninstall registry key |
| Good for | USB drives, no-admin environments, trying it out | Normal desktop installs |

Neither is currently Authenticode-signed — see [Security](#security)
below and `SECURITY.md` for what that means for the SmartScreen warning
you'll see on first run.

---

## Running

### GUI (Desktop App)
```bash
python main.py
python main.py --debug    # verbose console logging
```

### CLI (Headless)
```bash
# Single track
python cli.py "https://www.youtube.com/watch?v=TESTVIDEOAAA"

# Playlist (all tracks)
python cli.py "https://www.youtube.com/playlist?list=PLxxxxx"

# Spotify album → YouTube match → download
python cli.py "https://open.spotify.com/album/TESTALBUMID00001"

# Options
python cli.py URL --media-type video --quality video_1080 --output ~/Music
python cli.py URL --audio-format flac --parallel 4 --cookies cookies.txt

# List tracks without downloading
python cli.py URL --list
```

**CLI Options:**

| Flag | Default | Description |
|---|---|---|
| `-o / --output` | `~/Downloads/BananaFlow` | Output directory |
| `-f / --media-type` | `audio` | `audio` or `video` |
| `--audio-format` | `mp3` | `mp3`, `m4a`, `flac`, `opus` (audio mode only) |
| `--quality` | media-type default | Stable quality preset ID, e.g. `audio_mp3_320`, `audio_m4a_256`, `video_1080`, `video_best`, or `video_smallest` |
| `-j / --parallel` | `3` | Concurrent downloads (1–6) |
| `--cookies` | _(none)_ | Path to Netscape cookies.txt |
| `-l / --list` | — | List tracks; stdout is tab-separated for piping |
| `-q / --quiet` | — | Suppress progress output |
| `--debug` | — | Enable verbose logging |

After `pip install -e .`, the CLI is also available as:
```bash
bananaflow-cli URL [options]
```

---

## Supported Platforms (Operating System)

| OS | Status |
|---|---|
| **Windows 10/11 x64** | Officially supported — the target Beta platform; the only platform with a blocking CI gate. |
| **macOS (Apple Silicon)** | Experimental — builds successfully and runs, but is unsigned/un-notarized (Gatekeeper warning, see above) and has not gone through the same manual acceptance testing as Windows. |
| **Linux** | Source install only, unsupported — no packaged build exists, and CI has a known non-blocking test-flakiness pair unrelated to this project's own code. |

Do not treat macOS or Linux as fully supported equivalents of Windows —
this table exists specifically so that doesn't happen by omission.

---

## Supported Sites & URL Types

### Spotify (Playwright-based, headless Chromium)

| URL type | Action |
|---|---|
| `open.spotify.com/track/…` | Fetch single track metadata |
| `open.spotify.com/album/…` | Fetch all album tracks |
| `open.spotify.com/playlist/…` | Fetch all playlist tracks |
| `open.spotify.com/artist/…` | Fetch full discography (albums + singles/EPs) |

Spotify downloads work by scraping the track list headlessly, then running a `ytsearch1:Artist Title audio` YouTube query for each track via **yt-dlp**. **No Spotify API key is required.**

### YouTube Music (yt-dlp + ytmusicapi)

| URL type | Action |
|---|---|
| `music.youtube.com/watch?v=…` | Single track |
| `music.youtube.com/playlist?list=…` | Playlist |
| `music.youtube.com/browse/MPRE…` or `MPSP…` | Album |
| `music.youtube.com/browse/UC…` | Artist discography |

### YouTube (yt-dlp + Playwright for channels)

| URL type | Action |
|---|---|
| `youtube.com/watch?v=…` or `youtu.be/…` | Single video |
| `youtube.com/playlist?list=…` | Playlist |
| `youtube.com/@channel` or `/c/channel` | Full channel (Videos / Shorts / Releases / Playlists) |

### Generic / Other Sites

Any `http/https` URL is first attempted via yt-dlp's Generic extractor. If that finds nothing, the app falls back to a **Playwright page-interception** pass that captures live HLS/DASH/MP4 streams from the page's network traffic.

---

## File Organization — Output Folder Hierarchy

All files are saved under your configured **Output Directory** (default: `~/Downloads/BananaFlow`).

### Solo Download (exactly 1 song/video)
```
[Output Directory]/
    Song Name.mp3
```
No subfolder. No track-number prefix. No artist in filename.

### Playlist or Album (multi-track)
```
[Output Directory]/
    [Playlist or Album Name]/
        01 - Artist Name - Song Name.mp3
        02 - Artist Name - Song Name.mp3
```

### Spotify Artist Discography
```
[Output Directory]/
    [Artist Name]/
        אלבומים/
            [Album Name]/
                01 - Song.mp3
        סינגלים ו-EP/
            Single Name.mp3
            [EP Name]/          ← only if EP has >1 track
                01 - Song.mp3
```

### YouTube Music Artist Discography
```
[Output Directory]/
    [Artist Name]/
        אלבומים/
            [Album Name]/
                01 - Song.mp3
        סינגלים וגרסאות EP/
            Single Name.mp3
        הופעות חיות/
            [Live Album]/
                01 - Song.mp3
```

### YouTube Channel
```
[Output Directory]/
    [Channel Name]/
        סרטונים/
            Video Title.mp4
        קצרים/
            Short Title.mp4
        פריטי תוכן/
            Release.mp3
        פלייליסטים/
            (playlist entries)
```

---

## Filename Convention

| Scenario | Format |
|---|---|
| Solo download | `Song Name.mp3` |
| Multi-artist playlist | `Song Name.mp3` (title only) |
| Album / Artist discography | `01 - Artist Name - Song Name.mp3` |
| Video | `Artist Name - Title.mp4` |

**Automatic filename cleaning** is applied to all titles:
- Removes parenthetical noise: `(Official Video)`, `(Lyrics)`, `(4K)`, `(Prod. by …)`
- Strips promotional suffixes: `- Official`, `- Club Edit`, `- Original Mix`
- **Preserves** meaningful variants: `(Remix)`, `(Acoustic)`, `(Live)`, `(feat. …)`

---

## Search

The built-in Search panel supports three platforms:

### YouTube Music (`ytmusicapi`)
- Songs, Albums, Artists, Playlists — all in one combined results view
- Results arrive incrementally (section by section) while searching
- No authentication or API key needed

### YouTube (yt-dlp)
- Videos, Playlists, Channels
- Uses direct YouTube search-results page with type-filter tokens for accurate playlist/channel results

### Spotify
- Requires a self-hosted Spotify search proxy (see `SPOTIFY_PROXY_API.md`) **or** Spotify API credentials in Settings

Results show thumbnail, duration, view count, artist, and platform badge. Click to load directly into the download queue.

---

## Batch Import

Paste multiple URLs directly into the URL bar, or load a `.txt` file:

**Text file format:**
```
# My download batch – 2025-06-01
https://www.youtube.com/watch?v=TESTVIDEOAAA
https://open.spotify.com/album/TESTALBUMID00001
https://music.youtube.com/playlist?list=RDTESTPLAYLIST
```

- Lines starting with `#` are comments
- URLs can be mixed (YouTube + Spotify + YouTube Music) in any order
- Duplicates are automatically removed

The **Clipboard Monitor** (optional) watches your clipboard continuously and auto-adds any recognised URL it detects.

---

## Post-Processing Pipeline

Each step runs sequentially after yt-dlp finishes. All steps are individually guarded by a config flag and are **non-fatal** — a failure in one step is logged as a warning without cancelling the batch.

| Step | Config Flag | Default | Module |
|---|---|---|---|
| Custom thumbnail embed (Spotify hi-res art) | `embed_thumbnail` | **on** | `core/thumbnail_cropper.py` |
| Square thumbnail crop (1:1) | `square_thumbnails` | **on** | `core/thumbnail_cropper.py` |
| Metadata embedding (ID3/MP4 tags) | `embed_metadata` | **on** | yt-dlp FFmpegMetadata |
| MusicBrainz tag enrichment | `musicbrainz_enabled` | **on** | `core/musicbrainz_enricher.py` |
| Lyrics embedding | `lyrics_enabled` | off | `core/lyrics_embedder.py` |
| ReplayGain loudness analysis | `replay_gain_enabled` | off | `core/replay_gain.py` |
| SponsorBlock segment removal | `sponsorblock_enabled` | off | yt-dlp SponsorBlock PP |

---

## Authentication — YouTube Cookie Wizard

To authenticate downloads of age-restricted or members-only content:

1. Go to **Settings → Basic → Sign in for restricted downloads** and click **Sign in…**.
2. A browser window opens. Log into YouTube normally.
3. Close the browser. Cookies are saved into the app-data directory as
   `app_cookies.txt` (`%APPDATA%\.bananaflow\` on Windows,
   `~/Library/Application Support/BananaFlow/` on macOS).
4. The app picks up cookies automatically on the next download — no restart required.

> **Note:** Cookies expire after browser session rotation. If downloads fail with "Sign in" errors, re-run the wizard.

**Alternative:** Use the **Get cookies.txt LOCALLY** Chrome extension to export a `cookies.txt` file manually, then set it in Settings → Expert & Diagnostics → Authentication.

---

## Download Engine & Reliability

### Retry Policy
- yt-dlp is kept current — `requirements.txt` pins a minimum version (see YouTube Doctor below), and `yt-dlp[default]` bundles `yt-dlp-ejs` for player/signature solving.
- yt-dlp's own default client/extractor selection is used as-is — this app does **not** override `player_client` or force a specific client priority (e.g. `android_vr`, `web_safari`, `tv_downgraded`).
- Automatic retries only for genuinely transient failures — HTTP 429/502/503, timeouts, DNS failures, Windows file-lock errors — up to 3 retries with exponential backoff (1s → 2s → 4s).
- Failures that a retry can't fix are never blindly retried: PO Token required, expired/invalid cookies, missing JS runtime, HTTP 403/forbidden, sign-in required, private/deleted video, geo-block, copyright.

### YouTube Conservative Reliability Mode
Active by default (`youtube_reliability_mode: "conservative"`). The "fast" opt-in is a Settings toggle — **Settings → Expert & Diagnostics → YouTube Fast Mode** — off by default and explicitly described as raising the risk of 403s, rate-limiting, and sign-in/PO Token challenges. Applies only to YouTube/YouTube Music URLs — Spotify-matched tracks are included, since they resolve to a real YouTube watch URL before download; other sites are unaffected.
- Multiple YouTube downloads in the same batch are serialized (never more than 1 at a time), with a randomized 5–10 second cooldown between them.
- `concurrent_fragment_downloads` is capped at 1 for YouTube URLs.
- Non-YouTube downloads keep the general parallelism/delay settings below.

### Rate-limit & politeness defaults (non-YouTube-specific)
| Measure | Default |
|---|---|
| Random delay between downloads | 1.5–4.0 seconds (configurable) |
| Staggered batch start | Yes (avoids burst request patterns) |
| Max parallel downloads | 3 (configurable 1–6) |

### Filtered Log Noise
Terminal warnings like `Signature solving failed` and `n challenge solving failed` are **suppressed by design** — they are internal yt-dlp retry messages that do not indicate actual failures. Warnings that explain *why* a download is actually failing (missing PO Token, expired cookies, no JS runtime, 403/rate-limit) are never suppressed.

### YouTube Doctor
An offline, local-only readiness check — run it from **Settings → Expert & Diagnostics → YouTube Doctor**, or from the command line with `python cli.py --doctor`. It never contacts the network and never reads or displays cookie *values*, only whether cookies are present and which domain/name they belong to.

| Check | What it means |
|---|---|
| **yt-dlp version** | Installed version vs. the minimum this app expects |
| **yt-dlp-ejs** | Whether the YouTube JS player-logic plugin is installed |
| **JavaScript runtime** | Which of Deno / Node 22+ / QuickJS was selected (Bun is never auto-selected) |
| **Cookies** | Whether a cookies file or browser is configured, and whether it looks like a signed-in YouTube session — never a guarantee the cookies are still valid |
| **PO Token Provider** | Whether the provider is actually ready: plugin available, JS runtime selected, bgutil Deno script backend present, and backend health check passing. BananaFlow configures yt-dlp's official provider path for bundled builds, but never generates, scrapes, injects, or stores PO Tokens itself |
| **YouTube reliability mode** | Whether Conservative Mode (see "YouTube Conservative Reliability Mode" above) is currently active |

Each check reports **PASS**, **WARN**, or **FAIL** with a plain-language recommendation (e.g. "Install Deno or Node 22+"). When a download fails with a recognised YouTube-specific error, the same local checks are consulted to add a concrete note to the error message shown in the app (e.g. confirming that the bundled PO Token Provider stack is not ready).

Only configure cookies for videos that actually require sign-in (age-restricted, private, or members-only content) — using your main account's cookies for large automated batches carries some risk of the account being flagged. Use a dedicated account if you download YouTube content regularly with cookies enabled.

### PO Token Provider

YouTube can require a **PO Token** (Proof-of-Origin Token) for some requests before it will serve video/audio data. A few things worth knowing:

- **BananaFlow never generates, scrapes, injects, or stores a PO Token itself.** yt-dlp obtains PO Tokens only through its official **PO Token Provider** mechanism.
- The packaged Windows build stages the GPLv3-compatible `bgutil-ytdlp-pot-provider` plugin, the upstream bgutil Deno script backend, and a bundled Deno runtime. BananaFlow passes the provider's official `server_home` extractor arg to yt-dlp so normal users do not need to install pip packages, Deno, Node, Docker, or provider scripts manually.
- YouTube Doctor reports **PO Token Provider ready** only when the plugin is available, the JS runtime is usable, backend files are present, and `generate_once.ts --version` succeeds through bundled Deno. That health check verifies the local provider path; it does not print or expose PO Token values.
- **PO Tokens are unrelated to cookies.** Cookies authenticate *your account* for content that requires sign-in (age-restricted, private, members-only). A PO Token instead proves the *request itself* is legitimate to YouTube, regardless of whether you're signed in. Only configure cookies when a video actually requires login — they don't help with a PO Token requirement, and a PO Token Provider doesn't help with a sign-in requirement.
- **Public videos generally still work** without cookies. Some videos may require a working PO Token Provider; YouTube Doctor exists to make that readiness visible instead of surfacing a confusing raw yt-dlp error.

---

## Settings

In the app, Settings is organised into three pages: **Basic** (appearance,
language, accessibility, download toggles, sign-in help), **Advanced**
(playlist behaviour, clipboard/updates/browser cookies, system integration,
audio processing, search & proxies), and **Expert & Diagnostics** (YouTube
Doctor, manual update checks, cookie-file management, About).

All settings are stored in:
- **Windows**: `%APPDATA%\.bananaflow\config.json`
- **macOS**: `~/Library/Application Support/BananaFlow/config.json`
- **Linux**: `$XDG_CONFIG_HOME/bananaflow/config.json` (falls back to `~/.bananaflow/config.json`)

### General Settings

| Setting | Default | Description |
|---|---|---|
| `output_dir` | `~/Downloads/BananaFlow` | Root save directory |
| `media_type` | `audio` | Media type (`audio` / `video`) |
| `audio_quality` | `audio_mp3_320` | Stable audio preset ID; MP3 bitrate labels request exact output bitrates |
| `video_quality` | `video_1080` | Stable video preset ID; fixed-resolution presets are upper limits |
| `audio_format` | `mp3` | Audio output format (`mp3`, `m4a`, `flac`, `opus`) |
| `video_format` | `mp4` | Video output format — `mp4` is the only supported value today |
| `audio_quality_by_codec` | `{}` | Per-format remembered audio preset IDs |
| `embed_thumbnail` | `true` | Embed cover art into audio/video file |
| `embed_metadata` | `true` | Embed ID3/MP4 metadata tags |
| `playlist_subfolders` | `true` | Organise playlist downloads into named subfolders |
| `playlist_index_prefix` | `true` | Prefix filenames with `01 -`, `02 -`, etc. |
| `duplicate_action` | `warn` | On duplicate file: `skip`, `warn`, or `overwrite` |
| `language` | `en` | UI language: `en` (English) or `he` (Hebrew / RTL) |
| `theme` | `dark` | UI theme: `dark` or `light` |
| `accent_color` | `#F5A623` | Custom accent hex colour |

`media_type`, `audio_quality`, `video_quality`, `audio_format`, and
`video_format` follow the Type/Format/Quality pickers in the main toolbar —
changes there persist across restarts.

Audio quality labels describe the output conversion step. A higher bitrate
creates a larger file, but it cannot improve a source that was already lower
quality. FLAC avoids another lossy compression step; it does not restore
quality lost in the original source.

`video_best` is unrestricted Auto best available and may download 4K, 8K, or
higher when offered. Presets such as `video_2160` and `video_1080` are explicit
maximum-resolution caps. `video_smallest` uses yt-dlp format sorting to prefer
the smallest practical downloadable video file.

### Authentication Settings

| Setting | Default | Description |
|---|---|---|
| `cookies_file` | _(empty)_ | Path to a Netscape-format `cookies.txt` |
| `cookies_browser` | _(empty)_ | Live browser to extract cookies from (e.g. `chrome`) |

### Advanced / Post-Processing Settings

| Setting | Default | Description |
|---|---|---|
| `square_thumbnails` | `true` | Crop embedded art to 1:1 square |
| `musicbrainz_enabled` | `true` | Enrich tags via MusicBrainz API |
| `lyrics_enabled` | `false` | Auto-fetch and embed lyrics after download |
| `replay_gain_enabled` | `false` | Run ReplayGain loudness normalisation |
| `sponsorblock_enabled` | `false` | Remove non-music YouTube segments |
| `sponsorblock_categories` | _(see below)_ | Categories to remove |

Default SponsorBlock categories: `music_offtopic`, `sponsor`, `intro`, `outro`, `selfpromo`.

### Network & Performance Settings

| Setting | Default | Description |
|---|---|---|
| `max_parallel_downloads` | `3` | Concurrent download threads (1–6); YouTube URLs are further serialized by Conservative Mode (see above) regardless of this setting |
| `download_delay_range` | `[1.5, 4.0]` | Random sleep range between downloads (seconds); non-YouTube URLs only |
| `youtube_reliability_mode` | `"conservative"` | YouTube-only download behavior: `"conservative"` (serialized, 5–10s cooldown) or `"fast"` — toggle at Settings → Expert & Diagnostics → YouTube Fast Mode |
| `youtube_proxy_url` | _(empty)_ | HTTP/SOCKS proxy for yt-dlp |
| `proxy_server_url` | `http://localhost:8000` | Self-hosted Spotify search proxy |
| `check_updates` | `true` | Check GitHub for new releases on startup |

### Clipboard & System Tray Settings

| Setting | Default | Description |
|---|---|---|
| `clipboard_monitor` | `false` | Watch clipboard and auto-add recognised URLs |
| `tray_on_close` | `false` | Minimise to system tray instead of quitting |
| `global_hotkeys_enabled` | `false` | Register OS-level keyboard shortcuts |

---

## Download History

Every completed download is recorded in the SQLite database inside the app-data
directory (`%APPDATA%\.bananaflow\downloads.db` on Windows,
`~/Library/Application Support/BananaFlow/downloads.db` on macOS).

**History Panel features:**
- View the last 500 downloads (newest first)
- **Full-text search** by title or artist (FTS5 index)
- Delete individual records
- **Export to CSV** (UTF-8 with BOM for Excel compatibility)
- Open the output file directly from the history row

The database includes automatic integrity checking on startup. If corruption is detected, the file is renamed with a timestamp backup and a fresh database is created.

---

## Update System

**Updating BananaFlow is the primary update path.** Like any consumer app, each BananaFlow release ships the tested versions of everything it depends on — yt-dlp, yt-dlp-ejs, any bundled PO Token Provider / JS runtime, FFmpeg, and packaging fixes. A normal user only ever has to do one thing: update BananaFlow. Component updates exist as *advanced* support for source installs and emergencies, not as the normal experience.

On startup (when *Check for Updates on Launch* is enabled), a background thread runs two checks:

- **App updates** — queries the GitHub Releases API (`api.github.com/repos/BananaFlow-Media/BananaFlow/releases/latest`) and compares against the installed version using **semantic versioning** (SemVer).
- **Component updates** — queries PyPI for the latest **yt-dlp** / **yt-dlp-ejs** releases and compares against the installed versions. Outdated yt-dlp is the most common cause of broken YouTube downloads.

When something newer is found, an **update prompt** shows **one clear main recommendation** plus *Remind me later* (next launch / 1 day / 3 days / 1 week) and *Skip this version* (never nag about that exact version again; a newer future version notifies normally). Choices persist in `update_state.json` in the app-data directory. The main recommendation depends on what was found:

| Situation | Main recommendation |
|---|---|
| New BananaFlow release (with or without outdated components) | **Open Download Page** — the app update includes the updated components; no second "update components" button is shown |
| Components only, **source/venv** install | **Update Components** — runs `pip install --upgrade yt-dlp[default]` after the explicit click (developer workflow; restart required afterward) |
| Components only, **packaged EXE** | **Open Download Page** — components are bundled and refreshed by BananaFlow releases; the prompt explains that a release including them may not exist yet |

- Nothing is ever downloaded or installed without explicit approval
- Both checks can also be run on demand from **Settings → Expert & Diagnostics → Updates**: *Check for App Updates* (the recommended path) and *Check for Component Updates (Advanced)*, with explicit "up to date" / "check failed" feedback
- Pre-releases are skipped by default
- Network failures during the startup check are silently absorbed — a failed update check never crashes or shows an error
- The checks run in a background thread and do not block startup

**Future in-app updater (planned, not in this release).** The current "Open Download Page" flow is the deliberately safe first version. The planned evolution keeps the same approval gate and adds, after the user clicks: download the official installer from GitHub Releases → verify it against the release's published SHA-256 checksum → launch the installer → exit BananaFlow and clean up the temporary download. No step of that flow will ever run without the user's explicit approval, and the release checklist gains a signed-installer verification step before it ships.

---

## Bundled Downloader Components

The packaged EXE can ship important YouTube-reliability components
*inside the app* instead of requiring the user to install anything
separately. Two components can be bundled:

- **A JavaScript runtime** (Deno preferred, or Node; **MIT**) — fully out of the box once staged. `scripts/fetch_deno_runtime.ps1` downloads and SHA-256-verifies it into `packaging/runtime/` (gitignored — not committed, fetched fresh per build like FFmpeg), copied next to the EXE, and prepended to `PATH` at startup. yt-dlp-ejs needs a JS runtime to run YouTube's player logic, so bundling one means a clean machine with no Node/Deno installed still works — verified end-to-end with a real staged Deno binary during development.
- **A PO Token Provider stack** ([`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider), **GPL v3** — see `THIRD_PARTY_NOTICES.md` for the license notice and source notes). `scripts/build_windows.ps1` installs the pinned provider plugin, `packaging/stage_pot_provider.py` stages the plugin plus the upstream Deno script backend under `packaging/pot-provider-backend/`, and PyInstaller bundles those files with the Deno runtime. BananaFlow configures yt-dlp with the provider's official `youtubepot-bgutilscript:server_home=...` extractor arg. BananaFlow never generates, scrapes, stores, or injects PO Tokens itself.

These are required build inputs for the public Windows package. `core/runtime_components.py` locates and activates them, and **YouTube Doctor** reports PO readiness only after the backend health check passes. The component-update system still runs: it checks yt-dlp / yt-dlp-ejs against PyPI, and in the packaged EXE it guides the user to update BananaFlow itself (which ships the newer bundled components) rather than pip-upgrading in place.

---

## Architecture

### Layer Diagram

```
UI (app_window.py, panels/, components/)
    ↓  Qt Signals
Workers (ui/workers/)
    ↓  Python calls
Controllers (ui/controllers/)
    ↓  Python calls
Core (downloader.py, scraper.py, search_engine.py, …)
    ↓  imports
Utils (yt_dlp_opts.py, spotify_resolver.py, logger.py, …)
```

**Layering rule:** no layer imports Qt/PySide6 from above it. `core/` and
`utils/` import **no Qt/PySide6** and run headlessly (the CLI uses the same
core). The one exception is `ui.i18n`: it is a plain-Python translation
lookup (it defers all Qt imports), and a couple of core/utils modules
(`core/downloader.py`, `utils/cookie_validator.py`) import its `t()` helper
so the user-facing text they produce is localized. That keeps error/message
text in one place without pulling Qt into the backend.

### Core Modules

| Module | Responsibility |
|---|---|
| `core/downloader.py` | yt-dlp download engine — `DownloadRequest` / `DownloadProgress` / `DownloadEngine` |
| `core/playlist_parser.py` | URL classifier + metadata extractor (no download) → `TrackMeta` / `ParseResult` |
| `core/scraper.py` | Platform-isolated Playwright scrapers for Spotify, YTM, YouTube channels |
| `core/download_orchestrator.py` | Thread-pool batch manager — parallelism, cancellation, progress aggregation |
| `core/search_engine.py` | Unified search: YTM (ytmusicapi), YouTube (yt-dlp), Spotify proxy |
| `core/history_db.py` | SQLite download history — insert, search (FTS5), export CSV |
| `core/batch_importer.py` | Parse URLs from text files or pasted multi-line text |
| `core/retry_policy.py` | Exponential-backoff retry wrapper for transient download failures |
| `core/update_checker.py` | GitHub Releases API version checker (SemVer comparison) |
| `core/lyrics_embedder.py` | Fetch and embed lyrics using `syncedlyrics` |
| `core/musicbrainz_enricher.py` | Enrich ID3/MP4 tags via MusicBrainz API |
| `core/replay_gain.py` | ReplayGain loudness analysis and embedding |
| `core/thumbnail_cropper.py` | Download hi-res cover art, optionally crop to 1:1 square (Pillow) |
| `core/hls_downloader.py` | FFmpeg-based downloader for HLS/DASH/direct-stream URLs |
| `core/universal_extractor.py` | Playwright network-interception fallback for generic sites |
| `core/duplicate_checker.py` | Check for already-downloaded files before queueing |
| `core/queue_persistence.py` | Serialise/restore the download queue across restarts |
| `core/offline_monitor.py` | Background thread that watches network availability |
| `core/cookie_wizard.py` | Playwright browser login wizard (runs in QThread) |
| `core/services.py` | `ServiceContainer` — DI container for shared backend singletons |

### UI Modules

| Module | Responsibility |
|---|---|
| `ui/app_window.py` | Main `AppWindow` — navigation, error dialogs, wizard launch |
| `ui/theme_manager.py` | Dark/Light themes + custom accent colour |
| `ui/i18n.py` | Translations (English / Hebrew) + RTL layout switching |
| `ui/panels/url_bar.py` | URL input bar with batch-paste detection |
| `ui/panels/options_bar.py` | Format / quality selector above the queue |
| `ui/panels/queue_panel.py` | Download queue with progress cards |
| `ui/panels/search_panel.py` | Multi-platform search UI |
| `ui/panels/history_panel.py` | SQLite download history viewer |
| `ui/panels/settings_panel.py` | All user-configurable settings |
| `ui/panels/converter_panel.py` | Local audio file format converter |
| `ui/panels/status_bar.py` | Bottom status bar (speed, ETA, messages) |
| `ui/components/track_card.py` | One card per queued track (thumbnail, progress bar, controls) |
| `ui/components/search_result_card.py` | One card per search result |
| `ui/components/history_row.py` | One row per history record |
| `ui/components/offline_banner.py` | Network-offline warning banner |
| `ui/dialogs/update_prompt_dialog.py` | Update prompt (Install / Remind later / Skip version) |
| `ui/controllers/download_controller.py` | Orchestrates batch dispatch, folder routing, error handling |
| `ui/controllers/fetch_controller.py` | Controls URL fetching / playlist parsing |
| `ui/controllers/search_controller.py` | Controls search lifecycle and result delivery |
| `ui/workers/download_worker.py` | QThread wrapper — calls orchestrator, emits Qt signals |
| `ui/workers/fetch_worker.py` | QThread wrapper for playlist parsing |
| `ui/workers/search_worker.py` | QThread wrapper for search engine |
| `ui/workers/clipboard_worker.py` | Background clipboard monitor |
| `ui/workers/thumbnail_worker.py` | Async thumbnail loader for cards |
| `ui/workers/update_worker.py` | Background app-release + component update checker |
| `ui/workers/component_install_worker.py` | User-approved pip upgrade of yt-dlp components |
| `ui/workers/scraper_worker.py` | QThread wrapper for Playwright scraping |
| `ui/workers/offline_monitor.py` | QThread wrapper for network monitor |

### Utils Modules

| Module | Responsibility |
|---|---|
| `utils/yt_dlp_opts.py` | Shared yt-dlp option builders (base, parse, download, search) |
| `utils/spotify_resolver.py` | Resolve Spotify track URLs → YouTube search matches |
| `utils/ytm_scraper.py` | YouTube Music artist discography fetcher (ytmusicapi) |
| `utils/logging_config.py` | Structured logging setup (file + console handlers) |
| `utils/logger.py` | `SilentLogger` class for yt-dlp (suppresses noise) |
| `utils/cookie_validator.py` | Detect expired or malformed cookies.txt files |
| `utils/artwork_cleaner.py` | Normalise and upgrade thumbnail/cover URLs per platform |
| `utils/paths.py` | Cross-platform app-data dir + bundled-FFmpeg discovery (single source of truth) |
| `utils/time_format.py` | `seconds_to_str()` — convert raw seconds to `"M:SS"` / `"H:MM:SS"` |
| `utils/network_probe.py` | Lightweight connectivity check |

---

## Project File Layout

```
bananaflow-main/
├── main.py                         # GUI entry point
├── cli.py                          # Headless CLI entry point
├── config.py                       # AppConfig dataclass + JSON persistence (v3.1)
├── config_migrate.py               # Schema migration for older config.json versions
├── error_handler.py                # Error classification and ErrorInfo dataclass
├── pyproject.toml                  # Package metadata + entry points (bananaflow / bananaflow-cli)
├── requirements.txt                # Python dependencies
│
├── core/                           # Backend — no Qt/PySide6 (runs headless)
│   ├── downloader.py               # yt-dlp download engine
│   ├── playlist_parser.py          # URL classifier + metadata extractor
│   ├── scraper.py                  # Playwright scrapers (Spotify, YTM, YouTube)
│   ├── download_orchestrator.py    # Thread-pool batch manager
│   ├── search_engine.py            # Multi-platform search engine
│   ├── history_db.py               # SQLite download history
│   ├── batch_importer.py           # Multi-URL text/file importer
│   ├── retry_policy.py             # Retry-with-backoff logic
│   ├── update_checker.py           # GitHub Releases version checker
│   ├── component_updates.py        # yt-dlp / yt-dlp-ejs staleness checker (PyPI)
│   ├── update_state.py             # Remind-later / skip-version persistence
│   ├── runtime_components.py       # Bundled PO Token Provider + JS runtime detect/activate
│   ├── lyrics_embedder.py          # Lyrics fetch + embed
│   ├── musicbrainz_enricher.py     # MusicBrainz tag enrichment
│   ├── replay_gain.py              # ReplayGain analysis + embedding
│   ├── thumbnail_cropper.py        # Cover art download + 1:1 crop
│   ├── hls_downloader.py           # HLS/DASH/direct-stream via FFmpeg
│   ├── universal_extractor.py      # Playwright network-interception fallback
│   ├── duplicate_checker.py        # Pre-queue duplicate detection
│   ├── queue_persistence.py        # Queue save/restore across restarts
│   ├── offline_monitor.py          # Network availability monitor
│   ├── cookie_wizard.py            # Playwright login wizard
│   └── services.py                 # ServiceContainer (DI)
│
├── ui/                             # Frontend — Qt/PySide6
│   ├── app_window.py               # Main AppWindow
│   ├── theme_manager.py            # Dark/Light + accent colour
│   ├── i18n.py                     # Translations + RTL support
│   ├── panels/                     # Full-screen panels
│   │   ├── url_bar.py
│   │   ├── options_bar.py
│   │   ├── queue_panel.py
│   │   ├── search_panel.py
│   │   ├── history_panel.py
│   │   ├── settings_panel.py
│   │   ├── converter_panel.py
│   │   └── status_bar.py
│   ├── components/                 # Reusable widgets
│   │   ├── track_card.py
│   │   ├── search_result_card.py
│   │   ├── history_row.py
│   │   └── offline_banner.py
│   ├── controllers/                # Business-logic layer between UI and core
│   │   ├── download_controller.py
│   │   ├── fetch_controller.py
│   │   └── search_controller.py
│   └── workers/                    # QThread background workers
│       ├── download_worker.py
│       ├── fetch_worker.py
│       ├── search_worker.py
│       ├── clipboard_worker.py
│       ├── thumbnail_worker.py
│       ├── update_worker.py
│       ├── component_install_worker.py
│       ├── scraper_worker.py
│       └── offline_monitor.py
│
├── utils/                          # Shared helpers — no Qt/PySide6
│   ├── yt_dlp_opts.py
│   ├── spotify_resolver.py
│   ├── ytm_scraper.py
│   ├── logging_config.py
│   ├── logger.py
│   ├── cookie_validator.py
│   ├── artwork_cleaner.py
│   ├── paths.py
│   ├── time_format.py
│   └── network_probe.py
│
└── tests/                          # Pytest test suite
    ├── test_core.py
    ├── test_p0_gates.py
    ├── test_orchestrator.py
    ├── test_history_db_resilience.py
    ├── test_queue_persistence.py
    ├── test_retry_policy.py
    └── test_spotify_match_scorer.py
```

---

## Running Tests

```bash
pip install -e ".[dev]"
QT_QPA_PLATFORM=offscreen pytest tests/ -q
```

`QT_QPA_PLATFORM=offscreen` is required, not optional — without it, every
PySide6-based test tries to open a real window; on a machine with no
display (CI, SSH, most container images) that crashes instead of running,
and even on a normal desktop it pops up real windows during the run.

### On Windows, use the isolated runner

```bash
python scripts/run_isolated_tests.py
```

The single-process command above **cannot complete on Windows**. Qt state
accumulates across the GUI test files until the process faults at
`0xC0000005` partway through, with no summary printed. It is not a broken
test: the crash relocates when the blamed file is excluded. This is a
known, pre-existing platform condition, measured since the Phase 0
baseline.

`scripts/run_isolated_tests.py` runs every tracked test file in its own
interpreter, so nothing accumulates, and reads each child's real exit
code. It fails on a logical failure, a collection error, a timeout, a
missing result, or a native exit it has no reviewed classification for —
the two classified conditions are recorded with their evidence in
`scripts/isolated_test_baseline.json`. It writes per-file JUnit XML, logs
and a JSON aggregate to `test-evidence/`.

This is the gate `scripts/build_windows.ps1` and **every** CI leg runs, so
a green run locally and a green CI run check the same thing. The plain
`pytest tests/` command remains handy for running a single file or a
subset during development on either platform — it just isn't the gate,
because the full suite in one process is unreliable: Windows faults in Qt
teardown, and on Linux a single Windows-only test can crash the whole
session.

Test order is not meaningful — tests do not depend on running before or
after any other test — so `pytest -p no:randomly` or a fixed `-p xdist`
worker count are both safe if you add either plugin locally.

---

## Troubleshooting

**FFmpeg not found**  
Install FFmpeg and ensure it is on `PATH`. The app shows a warning on startup.

**YouTube "Sign in" errors**  
Run the sign-in helper: Settings → Basic → Sign in for restricted downloads → Sign in…. Cookies may have expired after a browser session rotation.

**Spotify not downloading**  
Spotify downloads route through YouTube search. If a song title or artist name is uncommon, the match may fail. Download the YouTube URL directly for guaranteed results.

**Download stops after 1–2 tracks**  
Usually a YouTube rate-limit (HTTP 429). The app has built-in retries and configurable sleep delays. Re-running the Cookie Wizard often resolves persistent cases.

**Signature / n-challenge errors in terminal**  
These are suppressed internal yt-dlp retry messages — not real failures. They appear only with `--debug`. Install `pip install quickjs` if they appear as actual errors.

**Chrome cookie lock error**  
Close Chrome completely before extracting cookies. If the problem persists, use the Cookie Wizard, which uses Playwright to read cookies through a separate browser session (no DPAPI decryption).

**Generic / external site not downloading**  
The app automatically falls back to Playwright network interception. If that also fails, the site is likely DRM-protected or requires credentials the app does not have — only content you have rights to access can be downloaded.

---

## Known Limitations

Real, current limitations — not hidden here:

* **No Stable release yet.** This is pre-release Beta-track software;
  the first Stable release will be 1.0.0, after the beta series.
* **Windows binaries are unsigned.** SmartScreen will warn on first run
  (`SECURITY.md`).
* **macOS is experimental**, unsigned/un-notarized; **Linux is
  source-install-only**, with a known non-blocking CI test-flakiness pair.
* **Generic-site downloading is best-effort.** yt-dlp's Generic extractor
  and the Playwright interception fallback cover a lot, but not
  everything — DRM-protected or credential-gated content is out of scope
  by design (`docs/legal/acceptable-use.md`).
* **No in-app auto-updater yet** — "Check for Updates" opens the GitHub
  Releases page; it does not download/install automatically (see "Update
  System" above). A secure per-component updater was deliberately not
  built yet — see `docs/architecture/secure-component-updater.md`.
* **Manual acceptance testing** of each packaged build is a release
  gate carried out by the maintainer before publishing — see
  `docs/release/RELEASING.md`.

## Roadmap

* **Shipped**: everything described in this manual — download engine,
  Search, Queue, History, Converter, Tag Editor, Cookie Wizard, YouTube
  Doctor, PySide6/Fluent UI, Hebrew RTL, CLI.
* **In progress**: the road from the current beta series to the first
  Stable (1.0.0) release.
* **Planned / good first issues**: see the
  [open Issues](https://github.com/BananaFlow-Media/BananaFlow/issues) —
  labels [`good first issue`](https://github.com/BananaFlow-Media/BananaFlow/labels/good%20first%20issue)
  and [`help wanted`](https://github.com/BananaFlow-Media/BananaFlow/labels/help%20wanted)
  are the best entry points.
* **Exploring, not promised**: a project website, Winget/Chocolatey/
  Homebrew distribution, Microsoft Store distribution, a secure
  per-component updater.

## Security

Full policy in [`SECURITY.md`](../../SECURITY.md). Summary: report vulnerabilities
via GitHub private vulnerability reporting (never a public Issue); Windows
binaries are currently unsigned (verify the published SHA-256 before
running); supported versions are the latest public release and current
`main`.

## Privacy

Full policy in [`PRIVACY.md`](../../PRIVACY.md). Summary: BananaFlow itself does not
phone home or run analytics; third-party services (YouTube, Spotify,
MusicBrainz, etc.) receive requests directly from your device under their
own terms; Settings has a **Delete stored sign-in data** action.

## Legal / Acceptable Use

Full policy in [`docs/legal/acceptable-use.md`](../legal/acceptable-use.md).
Summary: use BananaFlow only for material you're legally entitled to access,
download, and store; it is not presented as a DRM-bypass tool.

## License

GPL-3.0-or-later — see [`LICENSE`](../../LICENSE). Contributions are accepted
under the same license (inbound = outbound); see
[`CONTRIBUTING.md`](../../CONTRIBUTING.md).

---

## Additional Documentation

| File | Description |
|---|---|
| `LICENSE` | GPL-3.0-or-later license text for BananaFlow's own code |
| `NOTICE` | Product copyright, warranty, and notice pointers |
| `LICENSES.md` | Combined installer-facing license/source/notice bundle |
| `SOURCE_OFFER.md` | Source availability notes for source and binary releases |
| `THIRD_PARTY_NOTICES.md` | Dependency, binary, and staged-component license notices |
| `CONTRIBUTING.md` | No-CLA/no-DCO contribution policy; inbound=outbound licensing |
| `SPOTIFY_PROXY_API.md` | API contract for the self-hosted Spotify search proxy server |
| `PROJECT_STRUCTURE.md` | Auto-generated architecture snapshot (may be slightly stale) |
| `user_guide_hebrew.md` | Full Hebrew user guide (מדריך משתמש בעברית) |
