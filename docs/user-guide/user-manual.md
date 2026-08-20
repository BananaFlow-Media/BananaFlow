# BananaFlow User Manual

Status: **Current user reference**

BananaFlow is a Windows-first desktop application for downloading, converting and organizing audio/video from YouTube, YouTube Music and Spotify metadata workflows, with a full batch Tag Editor, searchable history and a headless CLI.

Official website: <https://bananaflow.bananaflow-media.workers.dev/> — downloads, Help, FAQ and support are available there in English and Hebrew.

## 1. What BananaFlow includes

| Area | What it does |
|---|---|
| Queue / downloads | Resolve URLs, inspect playlists/albums/channels and download audio/video through the shared yt-dlp-based engine |
| Search | YouTube, YouTube Music, and optional Spotify text search |
| History | Searchable SQLite-backed record of completed downloads |
| Converter | Convert local media to supported target formats with output verification |
| Tag Editor | Proposal-first batch metadata editing with review, backup, journal, undo/redo, artwork/lyrics/ReplayGain/tools and safe Apply/restore |
| Cookie Wizard / YouTube Doctor | Isolated sign-in helper plus local readiness diagnostics |
| CLI | Headless access to the same core download engine |
| Languages | English and Hebrew with RTL-aware layout |
| Touch/accessibility | Keyboard/screen-reader/high-DPI/touch-aware behavior across supported UI surfaces |

## 2. Supported platforms

- **Windows 10/11 x64** — primary supported packaged platform; installer and portable builds.
- **macOS Apple Silicon** — experimental packaged support as described by current release notes; signing/notarization limitations may apply.
- **Linux** — source/developer use only unless a release explicitly says otherwise.

Packaged users do not need a separate Python installation. Source developers need the dependencies described in `README.md`/`CONTRIBUTING.md`.

## 3. The main workflow

1. Paste a supported URL into the Queue screen or choose a result from Search.
2. Fetch/resolve the URL so BananaFlow can build the item list.
3. Choose media type, format/quality and output behavior.
4. Review the queue.
5. Start the batch. Progress, completion/failure state and available ETA/speed information are shown while the backend works.
6. Successful downloads are persisted in History and written under the configured output directory.

Batch inputs can include playlists/albums and other supported collection URLs. Duplicate handling can skip, warn or overwrite according to settings and the applicable workflow.

## 4. YouTube and YouTube Music

BananaFlow uses yt-dlp for extraction/download and `ytmusicapi`/supporting logic for structured YouTube Music discovery where appropriate. YouTube sites change frequently, so current yt-dlp/runtime/provider health matters more than a permanently frozen list of supported page details.

### Reliability mode

YouTube conservative reliability mode is the default. It limits YouTube request concurrency/cooldowns more aggressively than general downloads to reduce avoidable rate-limit/authentication challenges. A Fast mode is an explicit opt-in and may increase the chance of 403/rate-limit/sign-in/PO-token challenges.

Run **YouTube Doctor** when YouTube behavior changes unexpectedly.

## 5. Spotify: URL import is not Spotify search

This distinction is important.

### Spotify URL import

Pasting a Spotify track, album, playlist or artist URL uses BananaFlow's browser-backed Spotify metadata scraper/resolver. It does **not** require the optional Spotify text-search proxy.

BananaFlow does not download Spotify audio streams. It reads descriptive metadata, then resolves a separate media source (normally YouTube/YouTube Music) and downloads that source.

### Spotify text search

Searching Spotify by typing a query in the **Search** screen uses the configured Spotify search proxy. The proxy API contract is [`spotify-proxy-api.md`](spotify-proxy-api.md). If no usable proxy is running/configured, Spotify text search will not return usable proxy results even though pasted Spotify URLs can still be imported.

## 6. Search

- **YouTube Music** — structured songs/albums/artists/playlists.
- **YouTube** — videos plus available playlist/channel categories.
- **Spotify** — optional proxy-backed text search.

Search results can be added to the queue. A track result resolves to a downloadable source; collection results preserve enough source information for the normal import workflow.

## 7. Audio/video formats and quality

Audio output supports MP3, M4A, FLAC and Opus. Video output is MP4 in the current product contract. Quality is selected through stable preset IDs/labels; fixed-resolution video presets act as caps while Auto/Best can select higher available resolution.

Higher output bitrate cannot restore source quality that was already lost. FLAC avoids another lossy encoding step; it does not make a lossy source lossless.

FFmpeg is required for conversion/post-processing paths. Packaged releases stage the required reviewed FFmpeg component; source installs need an available FFmpeg as described by setup documentation.

## 8. Output organization

The default output root is `~/Downloads/BananaFlow` unless changed. Single items can land directly in the output root; collections/discographies may use named subfolders and track-index prefixes according to the workflow/settings.

Filename cleaning removes common presentation noise while preserving meaningful variants where the matching/filename policy treats them as identity-relevant. Do not rely on a prose example as a stronger contract than the current filename/matching tests.

## 9. History

History is stored locally in `downloads.db` under the BananaFlow app-data directory. The History screen supports recent-record browsing, full-text search, record deletion and CSV export; current UI actions also refresh from committed history while the application is running.

Clearing a history record does not itself delete the downloaded media file unless a separate explicit file action says so.

## 10. Converter

The Converter works on local files and does not require an online service for the conversion itself. Add files, choose a target format/quality/output destination and start conversion. BananaFlow writes to a controlled output path and verifies the produced output rather than treating an FFmpeg exit alone as proof of success.

Use disposable copies when testing unusual/unsupported media. Conversion should not silently destroy the source file.

## 11. Tag Editor

The Tag Editor is an offline-first metadata workspace. Network access occurs only for explicitly selected online metadata/artwork/lyrics features.

### Proposal-first model

- Scanning loads original file state.
- Edits/actions create **proposals**; they do not immediately change media files.
- Row selection controls the scope of editing actions.
- Apply scope is based on pending, non-excluded, non-blocked proposals — not simply current selection/filter.
- Review Changes shows what will be written.
- Undo/redo before Apply works on proposals in memory.

### Apply safety

A disk-changing Apply uses the documented safety path: validate backup target → write/validate backup and durable operation plan/journal → write each file through a same-filesystem temporary copy → read back/verify intended fields → atomically replace the original → perform validated rename steps → report per-file outcomes.

A failed verification leaves the original untouched. Rename failures do not get counted as complete success and their proposal remains recoverable/retryable. Incomplete journals can trigger recovery on a later launch.

The detailed invariants are in `docs/architecture/tag-editor-safety.md`.

### Tools

The current Tag Editor includes fields, artwork, lyrics, ReplayGain, file properties, auto-arrange/actions/templates/workflows, duplicate handling, online MusicBrainz/Cover Art review, pending-change/problem/external-change review, CSV/report/playlist import/export flows and safe file-management actions.

## 12. Authentication and cookies

Only configure cookies for content that genuinely requires authenticated access.

### Preferred sign-in helper

Settings provides a sign-in helper that opens a BananaFlow-owned isolated Playwright browser profile. It does not silently reuse your normal Chrome/Edge profile. BananaFlow stores only the session material required by its documented YouTube flow and protects the BananaFlow-owned store to the current user where supported; Windows uses DPAPI for the protected cookie store.

### Manual cookie file

A Netscape-format `cookies.txt` can be configured where supported. Treat it like a password. Never attach it to a public issue or paste its values into logs.

Live normal-browser extraction can be limited/disabled on platforms where browser locks/encryption make it unsafe or unreliable; use the isolated helper/manual export path documented by the current Settings UI instead of trying to bypass browser protections.

### Delete stored sign-in data

The Settings action removes BananaFlow-owned cookie/profile state. It does not sign you out of your normal browser.

## 13. YouTube Doctor

YouTube Doctor is a local readiness diagnostic available from Settings and through:

```bash
bananaflow-cli --doctor
```

It checks relevant downloader/runtime/cookie/provider/reliability state and gives recommendations without printing cookie values. The CLI diagnostic output is developer/support oriented and intentionally uses a stable console format; the GUI is the localized end-user surface.

## 14. Runtime components

Packaged Windows releases can stage yt-dlp support components, a JavaScript runtime (Deno), Playwright Chromium and the configured PO Token Provider stack so a clean machine does not require manual runtime assembly. Exact versions/licenses/source information belong in `THIRD_PARTY_NOTICES.md`, `SOURCE_OFFER.md` and the release SBOM.

BananaFlow itself does not manually scrape/store/inject live PO-token values; yt-dlp obtains them through its provider mechanism when needed.

## 15. Settings and important defaults

`config.py` is the source of truth. Important current defaults include:

| Setting | Default |
|---|---|
| Output directory | `~/Downloads/BananaFlow` |
| Media type | audio |
| Audio format | MP3 |
| Video format | MP4 |
| Theme | light |
| Accent | BananaFlow green (`#10A37F`) |
| Language | detected from system (Hebrew when detected, otherwise English) |
| Clipboard monitor | off |
| Update checks | on |
| Max general parallel downloads | 3 |
| YouTube reliability mode | conservative |
| Duplicate action | warn |
| Embed thumbnail / metadata | on |
| Square thumbnails | on |
| MusicBrainz enrichment | on |
| Lyrics / ReplayGain / SponsorBlock | off by default |
| Spotify search proxy URL | `http://localhost:8000` until the user changes/disables it |

The Settings UI is organized into user-facing groups/pages; rely on the labels in your installed version rather than old screenshots from pre-release development.

## 16. Updates

When update checks are enabled, BananaFlow checks the public GitHub release feed. Packaged users normally update BananaFlow as a whole so the app and bundled components stay on a tested combination. Component-version checks can provide advanced/source-install guidance, but the normal packaged path is the official BananaFlow release/download page.

The current product does not silently install an unapproved application update in the background.

## 17. CLI

See [`cli.md`](cli.md). The exact option list is always available through:

```bash
bananaflow-cli --help
```

Common flags include output directory, media type, audio format, quality preset, parallelism, cookies, list-only, quiet/debug, `--doctor` and `--version`.

## 18. Local data and privacy

BananaFlow desktop telemetry is not automatically uploaded to the maintainer. Local application state includes configuration, history, update state, logs, optional sign-in data/browser profile and Tag Editor recovery/workflow data. The complete current inventory and network-service table are in [`../../PRIVACY.md`](../../PRIVACY.md).

The official website is a separate project with its own privacy/consent notice.

## 19. Troubleshooting

### YouTube suddenly fails

Run YouTube Doctor. Update BananaFlow if a newer release exists. Distinguish sign-in/cookie failures from PO-token/runtime failures and from permanent video availability/geo/copyright errors; repeatedly retrying a permanent failure is not a fix.

### HTTP 429 / rate limit

Stop creating additional request bursts, allow the service to recover and keep conservative reliability mode enabled. More parallelism is not necessarily faster when a remote service is throttling.

### HTTP 403 / sign-in required

Follow the error/Doctor recommendation. Do not assume every 403 is fixed by cookies; provider/runtime, availability and service-side policy can produce different permanent failures.

### Spotify URL works but Spotify Search does not

This is expected when the optional search proxy is not running/configured. URL import and text search are separate systems.

### FFmpeg missing in source install

Install a recent FFmpeg and put it on `PATH`. Packaged release builds stage their reviewed FFmpeg input.

### Playwright/browser component missing in source install

Install the repository's documented Playwright Chromium dependency. Packaged builds include the browser payload expected by the release.

### Logs

Use debug output only when needed and inspect it before sharing. Never post cookies/tokens/private URLs or unredacted configuration publicly.

## 20. Current limitations

- Windows is the primary supported packaged platform; other platforms have the status described above.
- Windows binaries are currently unsigned, so SmartScreen may warn on first run; verify official release provenance/checksums.
- Third-party sites can change without notice; generic-site extraction is best effort and intentionally does not promise DRM bypass.
- Spotify text search depends on the optional configured proxy, while Spotify URL import does not.
- Update checks do not mean silent background installation.

## 21. Developer/release documentation

End users normally do not need build internals. Contributors should use:

- `CONTRIBUTING.md`
- `docs/testing/TESTING.md`
- `docs/architecture/overview.md`
- `PROJECT_STRUCTURE.md`
- `docs/release/RELEASING.md`
- `SECURITY.md` / `PRIVACY.md`

License: GPL-3.0-or-later; third-party notices and source availability are documented in the repository's license/source bundle.
