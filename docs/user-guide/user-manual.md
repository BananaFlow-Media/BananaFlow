# BananaFlow User Manual

Status: **Current end-user reference**

This manual is for people who want to **use BananaFlow**, not develop it. No programming knowledge is assumed.

Official website: <https://bananaflow.bananaflow-media.workers.dev/> — downloads, Help, FAQ and support are available there in English and Hebrew.

## 1. Supported systems

- **Windows 10/11 x64** — supported. Use the installer or portable package.
- **macOS Apple Silicon** — supported. Use the macOS app/DMG from the official release. Because signing/notarization is currently limited, macOS may ask for an extra first-run approval; follow the release/website instructions if Gatekeeper blocks the first launch.
- **Linux** — supported when running from source. BananaFlow is expected to work normally once its dependencies are installed, but there is no official Linux installer/package yet.

For most users, the easiest path is the official download page. You do **not** need to install Python separately when using the Windows or macOS packaged release.

## 2. What BananaFlow does

BananaFlow gives you one desktop app for:

- downloading audio/video from YouTube and YouTube Music;
- importing Spotify track/album/playlist/artist links and finding a separate downloadable source;
- searching YouTube/YouTube Music and, optionally, Spotify text search;
- converting local media files;
- editing music tags in batches;
- keeping a searchable download history;
- signing in to YouTube only when a video really requires it; and
- diagnosing YouTube problems with YouTube Doctor.

## 3. Normal download workflow

1. Paste a link into BananaFlow, or choose a result from Search.
2. Let BananaFlow load the item or collection.
3. Choose audio/video, format, quality and output folder.
4. Review the queue.
5. Start the download.
6. Follow the progress until the item finishes or BananaFlow shows an error/recommendation.

Completed downloads are written to your chosen folder and added to History.

## 4. Spotify: link import and text search are different

### Pasting a Spotify link

You can paste a Spotify track, album, playlist or artist link directly into BananaFlow. This does **not** require the optional Spotify Search Proxy.

BananaFlow does not download Spotify's protected audio stream. It reads the descriptive information and resolves a separate downloadable source, normally from YouTube/YouTube Music.

### Typing a Spotify search

Spotify **text search** is an optional advanced feature that currently needs a self-hosted Spotify Search Proxy configured in Settings. If you do not run one, pasted Spotify links can still work even when Spotify text search does not.

Most users do not need to know the proxy API. The technical operator documentation is in [`spotify-proxy-api.md`](spotify-proxy-api.md).

There is currently no BananaFlow-operated public Spotify Search Proxy. Use only a self-hosted endpoint you trust and configure yourself.

## 5. Formats and quality

Current audio outputs include MP3, M4A, FLAC and Opus. Video output is MP4.

A higher bitrate cannot restore quality that was already missing from the original source. FLAC avoids another lossy conversion step, but it cannot turn a lossy source into true lossless audio.

## 6. Output folders and duplicates

The default output location is under `Downloads/BananaFlow`, unless you change it in Settings. Collections such as playlists/albums can be organized into subfolders and numbered automatically.

When BananaFlow finds an existing file, the duplicate policy can skip, warn or overwrite according to your settings and the current workflow. Read the prompt before confirming an overwrite.

## 7. Search

The Search screen supports:

- **YouTube Music** — songs, albums, artists and playlists;
- **YouTube** — videos and supported collection results;
- **Spotify** — optional text search through the self-hosted proxy described above.

Choose a result to add it to the normal BananaFlow workflow.

## 8. History

History keeps a local record of completed downloads. You can search it, remove history records and export information to CSV.

Deleting a history record does not automatically delete the media file unless you deliberately choose a separate file-deletion action.

## 9. Converter

The Converter works with local files. Add the files, choose the target format/quality/output location and start the conversion.

BananaFlow checks the produced output before reporting success. Use copies when experimenting with unusual or unsupported media.

## 10. Tag Editor

The Tag Editor lets you edit many music files together without immediately writing every click to disk.

The safe workflow is:

1. Open/scan a folder.
2. Make the changes you want.
3. Review the pending changes.
4. Exclude anything you do not want to apply yet.
5. Press **Apply** only when the review is correct.

BananaFlow creates recovery information before disk-changing operations and verifies the write before replacing the original file. Undo/restore tools are available for supported operations.

The Tag Editor also includes artwork, lyrics, ReplayGain, duplicate tools, MusicBrainz/Cover Art lookup, CSV/report/playlist tools, actions/templates/workflows and file-management helpers.

## 11. YouTube sign-in and cookies

Do not sign in unless the video actually requires authenticated access.

The preferred method is BananaFlow's **Sign in** / Cookie Wizard flow in Settings. It opens an isolated BananaFlow-controlled browser session instead of silently taking cookies from your normal browser profile.

If you manually use a `cookies.txt` file, treat it like a password. Never post it in an Issue, Discussion or screenshot.

Settings also provides **Delete stored sign-in data** for BananaFlow-owned sign-in state.

## 12. YouTube Doctor

If YouTube suddenly stops working, open **YouTube Doctor** in Settings. It checks the local download environment and gives a recommendation without showing your cookie values.

Useful examples:

- outdated/broken downloader component;
- missing JavaScript/runtime support;
- sign-in/cookie problem;
- PO Token Provider problem;
- conservative/fast reliability mode state.

You normally do not need to understand the implementation behind those checks — follow the recommendation shown by the app.

## 13. Updates

When update checks are enabled, BananaFlow checks for a newer official application release and for newer critical downloader components.

- An **application update** opens BananaFlow's official website download page (not GitHub directly) and remains a normal full installation. BananaFlow does not download and run a new application installer automatically: the currently unsigned Windows package requires the user to choose and launch the full installer explicitly.
- In an **installed packaged build**, **Update Components** downloads the reviewed `yt-dlp` / `yt-dlp-ejs` bundle from BananaFlow's official GitHub component channel, verifies its exact size and SHA-256, checks compatibility, safely prepares it in per-user app data and health-checks it in a separate process. It becomes active only after BananaFlow restarts; the installed application is not rewritten and the previous valid bundle remains available for fallback. After you approve such an update, BananaFlow refreshes its small public safety record at most once per day before reusing it; if it reports the bundle is no longer safe, BananaFlow uses its built-in downloader instead.
- In a **source environment**, the same button runs the documented pip upgrade in that environment.

Neither path installs silently: checking and installing are separate, and the update button is the approval gate.

When upgrading with the Windows installer, the new version replaces the existing BananaFlow installation. The installer cleans obsolete bundled downloader files and version metadata before copying the new package. For a Portable download, extract it to a new folder instead of merging it into an older Portable folder.

## 14. Common problems

### YouTube fails suddenly

Run YouTube Doctor and check whether a newer BananaFlow release exists.

### Rate limit / HTTP 429

Stop creating more requests, leave Conservative Mode enabled and try again after the service has had time to recover. More parallel downloads are not always faster.

### 403 / sign-in required

Follow the exact BananaFlow/YouTube Doctor recommendation. Cookies are not the answer to every 403.

### Spotify link works but Spotify Search does not

That usually means the optional self-hosted Spotify Search Proxy is not configured/running. Link import and text search are separate systems.

### Windows SmartScreen warning

Current Windows packages are not Authenticode-signed. Download only from the official website/GitHub release and verify the published release information before proceeding.

### macOS blocks the first launch

The supported macOS package may require the current Gatekeeper first-run approval while signing/notarization is limited. Use the official release/help instructions rather than disabling system security globally.

### Linux has no installer

Linux is supported from source, but an official packaged installer is not published yet. The source-install instructions live in the repository README for users comfortable with that setup.

## 15. Privacy and support

BananaFlow does not automatically upload desktop usage telemetry to the maintainer. Some features contact third-party services because that is how the feature works. See [`../../PRIVACY.md`](../../PRIVACY.md) for the full data/network list.

Never share cookies, passwords, tokens or private media/log details publicly.

For help:

- official website Help/FAQ/support;
- [`../../SUPPORT.md`](../../SUPPORT.md);
- GitHub Issues for reproducible bugs;
- the closest GitHub Issue form for reproducible bugs, feature requests and pre-release build feedback.

Use BananaFlow only for material you are entitled to access, download and store. See [`../legal/acceptable-use.md`](../legal/acceptable-use.md).

## 16. Advanced users

The ordinary user manual intentionally stops here. Advanced/technical material is kept separately so it does not make basic help harder to read:

- [`cli.md`](cli.md) — command-line use;
- [`spotify-proxy-api.md`](spotify-proxy-api.md) — self-hosted Spotify text-search proxy;
- [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) — development;
- [`../testing/TESTING.md`](../testing/TESTING.md) — testing;
- [`../architecture/overview.md`](../architecture/overview.md) — architecture.
