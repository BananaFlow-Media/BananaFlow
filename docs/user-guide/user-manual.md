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

### Importing a whole artist or YouTube channel

When you paste a YouTube channel URL, BananaFlow discovers the channel tabs and asks which ones to scan (for example Videos or Playlists). When you paste a Spotify or YouTube Music artist URL, it similarly discovers the categories that actually exist for that artist—such as Albums, Singles & EPs, Live performances, Videos, Compilations or Appears On. If only one importable category exists, BananaFlow scans it automatically; otherwise you choose one or more categories first.

During catalog expansion, BananaFlow treats a stable Spotify or YouTube Music release ID as the release identity. If that release was advertised in more than one selected category, those categories are retained as discovery roles but its tracks are imported once. A partial Spotify grid may still be revisited through another role to recover missing positions. Releases without a stable ID are never merged by title alone.

Spotify artist rows receive their duration and square release cover from Spotify's public release metadata before they enter the queue. The Spotify cover remains the preferred artwork when the track is later matched to a downloadable YouTube/YouTube Music source; the matched video's rectangular thumbnail does not replace it.

After the selected categories are scanned, BananaFlow checks for the same recording appearing in genuinely separate releases or categories—including two albums or two singles inside one category. Exact provider IDs are treated as exact matches; a conservative title/artist/duration comparison may be shown as a probable match for review. For every group you can keep only one category or release, keep both/all occurrences, clear all, or adjust each occurrence separately. A Live, Remix or Remaster title is not silently merged with a differently named studio version. If a provider accidentally returns the exact same release position twice, BananaFlow collapses that repeated row before it reaches the queue. Repeated placements at different positions remain separate.

Spotify tracks are matched to separate downloadable sources only after the queue is ready. If two different Spotify recordings unexpectedly resolve to the same concrete YouTube video, BananaFlow invalidates the suspicious match and searches again while excluding the already-used video. If a distinct trustworthy match still cannot be found, the suspect track fails with an explanation instead of silently downloading a second copy of another track. Two occurrences that represent the same recording may intentionally share the same source when you chose to keep both releases.

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

The default output location is under `Downloads/BananaFlow`, unless you change it in Settings. Collections such as playlists/albums can be organized into subfolders.

Filename prefixes follow the source's original position on both Spotify and YouTube Music; reordering the download queue does not change them:

- a directly downloaded song has no number and uses `Artist - Title` when an artist is known;
- an album or EP always uses its original track number (`01 -`, `02 -`, ...);
- a compilation has no filename number and uses `Artist - Title` to avoid title collisions;
- a playlist uses its original playlist position only when **Playlist Position Prefix** is enabled in Settings;
- an artist import applies the same rule per item: albums and EPs use release track numbers, compilations and standalone singles/videos/performances do not, and playlists follow the playlist setting.

With collection subfolders enabled, the source collection—not a member track's album tag—controls the folder:

- a direct playlist uses `Playlist/` and a direct album uses `Album/`;
- an artist import uses `Artist/Albums/Album`, `Artist/Singles & EPs/EP`, `Artist/Compilations/Compilation` or `Artist/Playlists/Playlist`;
- when provider metadata identifies a multi-disc album, it adds `Disc 1`, `Disc 2`, ... below the release folder so equal track numbers on different discs cannot collide, even when only one disc is selected;
- disabling collection subfolders keeps every automatic folder out of the path. Disabling the Singles & EPs category keeps EP release folders directly below the artist.

Meaningful version labels such as Live, Acoustic, Remix, Edit, Original and Remaster are retained in filenames. Promotional labels such as “Official Video” are removed.

When BananaFlow finds an existing file, the duplicate policy can skip, warn or overwrite according to your settings and the current workflow. Read the prompt before confirming an overwrite.

The artist/channel duplicate review described above happens before items enter the queue and controls which source locations are imported. It is separate from the existing-file policy, which runs later against files already present in the output folder.

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

### What happens when downloads fail

An ordinary track-specific failure does not stop the queue. The first affected track opens one non-blocking incident dialog; later tracks with the equivalent error update that same dialog and its details list instead of opening duplicate popups. Other tracks keep downloading. **Retry these tracks** resubmits every track collected in the dialog, while **Skip these tracks** settles that group without retrying it.

Authentication, cookie, bot-challenge and connectivity failures are counted as consecutive systemic failures. The same incident is visible from the first failure and updates on the second. Other work continues until the third consecutive failure; at exactly the third failure BananaFlow pauses all new download starts because continuing with the same broken condition would only fail more tracks. A successful track breaks an unpaused streak. After sign-in/cookies are repaired, BananaFlow retries every track collected for that incident—it does not silently skip them.

An explicit YouTube rate limit is different: BananaFlow pauses all new YouTube work immediately. The dialog keeps YouTube's exact message in **Details**, shows a live countdown and adds a small safety margin after the advertised wait. When the timer expires, the track that observed the limit is tried first as a canary; the remaining work is released only after that attempt proves requests can continue.

When the first Spotify-to-YouTube wording has no usable match, BananaFlow automatically tries a bounded set of alternatives: normalized punctuation, title/artist order and album context when available. Every search still includes both the artist and title. Results are combined and scored by title, artist credits, duration and recording version; a clear match stops the extra searches. When song search still misses and album metadata is available, BananaFlow also checks the verified YouTube Music album and scores its track list. If all searches and the final conservative request return zero usable YouTube items, BananaFlow reports **No matching YouTube result**. This means no source was available or safely identifiable at that time and no output file was created; it does not mean a downloaded file disappeared.

The no-result dialog offers **Choose other sources** in addition to retry and skip. It opens YouTube search inside BananaFlow for each stopped track. Compare the original track shown in the banner with each result's title, artist and duration, then choose **Use source**. BananaFlow stages the choices and retries them together through the normal bounded download pool, while keeping the original Spotify title, artist, album, numbering and folder layout. You can edit the search wording or stop choosing; choices already made are retried and the aggregate incident reopens for the remaining tracks. The application never accepts a weak alternative automatically: a source cannot be guaranteed when the exact recording was never uploaded, is private, region-blocked or temporarily hidden by YouTube.

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

Leave Conservative Mode enabled and let BananaFlow's visible countdown finish. It already stops new YouTube work, waits for the advertised duration plus a safety margin and tests the same track before releasing the queue. Hiding the dialog does not cancel the timer; do not start repeated manual retries.

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
