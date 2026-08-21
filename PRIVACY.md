# Privacy Notice

Status: **Current / normative for the BananaFlow desktop application**

This notice describes the desktop application in this repository. BananaFlow's official website is a separate project and publishes its own website privacy information; website analytics/consent behavior is not described by this desktop-app notice.

## Product telemetry

The BananaFlow desktop application has no BananaFlow-operated product telemetry backend, advertising identifier, background usage-analytics uploader or automatic crash/log upload. It does not automatically send download history, logs or diagnostics to the maintainer.

Third-party network services still receive normal connection metadata such as IP address, request time, TLS/network metadata and a browser/application user agent when the user invokes features that contact them.

## Network services used by the app

The exact requests depend on selected features and configuration.

| Service | When used | Data normally sent |
|---|---|---|
| YouTube / YouTube Music | Search, metadata/extraction, downloads, channel/listing work, authenticated items when enabled | URLs/IDs, search terms, request headers and configured YouTube session cookies only when required |
| Spotify web pages | Resolving/importing Spotify track/album/playlist/artist metadata through the browser-backed scraper | Spotify URL/ID and normal browser request metadata |
| User-configured Spotify search proxy | Spotify **search** in the Search panel | Search query, requested result limit and optional `X-App-Token` |
| GitHub API / Releases | App update checks, opening official releases, and an explicitly approved packaged-component update | Repository/version/asset request, downloaded public component manifest/bundle and normal connection metadata |
| PyPI | Component-version checks and source/developer component updates | Package names and normal connection metadata |
| MusicBrainz | Explicit or enabled metadata enrichment | Track/artist/album/duration metadata needed for matching |
| Cover Art Archive | Optional artwork lookup | MusicBrainz release identifier and normal connection metadata |
| Lyrics providers used by configured dependencies | Optional lyrics lookup | Track title/artist search terms |
| SponsorBlock through yt-dlp | Optional segment handling | Video identifier and configured segment categories |
| Other user-selected sites supported by yt-dlp/generic extraction | URL inspection/download | Selected URL and the headers/cookies explicitly configured for that operation |

Spotify audio is not downloaded from Spotify servers. Spotify metadata is used to identify a separate source, normally YouTube/YouTube Music.

### Spotify search vs Spotify URL import

These are intentionally different paths:

- Pasting/importing a Spotify URL is handled by BananaFlow's Spotify metadata scraper/resolver and does **not** require the optional Spotify search proxy.
- Searching Spotify by text in the Search panel uses the optional configured proxy API described in `docs/user-guide/spotify-proxy-api.md`.

## Data kept on the device

The per-user application-data directory is resolved by `utils.paths` (Windows `%APPDATA%\.bananaflow`, macOS application-support location, Linux XDG/fallback location). Depending on features used, it can contain:

- `config.json` — settings and UI state, including optional proxy configuration;
- `downloads.db` — download history;
- `update_state.json` — update reminder/skip state;
- `components/downloader/` — verified versioned `yt-dlp` / `yt-dlp-ejs` overlays plus the active/previous selection pointer after an approved packaged-component update;
- rotating local logs;
- protected/minimized BananaFlow-owned YouTube cookie data (`app_cookies.dpapi` on Windows or an owner-permission cookie file on other supported paths);
- `browser_profile/` — dedicated sign-in-helper profile separate from normal browser profiles;
- queues/caches and Tag Editor drafts, backups, journals, presets/workflows/recovery state;
- downloaded media and user-selected exports at locations chosen by the user.

Cookies are credentials. BananaFlow does not intentionally display cookie values in diagnostics or logs. The protected local store reduces offline/cross-user exposure where the OS supports it; it does not protect against malware already running as the same user.

## Browser-cookie behavior

The preferred sign-in path uses BananaFlow's isolated browser profile. When a supported manual cookie-file or browser-cookie option is explicitly configured, that operation uses the selected source for the requested extraction. BananaFlow's delete action removes BananaFlow-owned sign-in data/configuration; it does not delete cookies from or sign the user out of a normal browser profile.

## Logs and diagnostics

Logs and diagnostic output remain local unless the user chooses to copy/share them. Central redaction is defense in depth, not a guarantee against every future third-party error/token format. Before sharing diagnostics, inspect them and remove private URLs, paths, media names, account information and any remaining credential material.

## Deleting local data

- **Delete stored sign-in data** removes BananaFlow-owned cookie/profile sign-in state after confirmation.
- The History screen can clear history records.
- Downloaded files and exports are removed by the user from the locations they selected.
- To remove remaining BananaFlow application state, exit the app and delete its per-user app-data directory after backing up anything needed.

The Windows uninstaller removes installed program files but intentionally does not assume that user-created media or all per-user data should be destroyed automatically.

## Website

The official website is <https://bananaflow.bananaflow-media.workers.dev/>. It is maintained separately from this application repository and has its own privacy/consent pages. A statement about “no desktop telemetry” must not be interpreted as a statement about the website's independently disclosed analytics behavior.

## Changes and questions

Material changes to network services, retained data, credentials, diagnostics or deletion behavior must update this file in the same code change under `docs/DOCUMENTATION_POLICY.md`. Security-sensitive questions should follow `SECURITY.md` and never include private data in public issues.
