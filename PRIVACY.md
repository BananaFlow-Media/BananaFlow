# Privacy Notice

This notice describes the behavior of the BananaFlow desktop application
in this repository. It is a technical description, not a promise about future
versions or the independent privacy practices of third-party services.

## Telemetry and analytics

BananaFlow has no product telemetry, advertising identifier, usage
analytics, background crash-upload service, or BananaFlow-operated analytics
backend. It does not automatically send logs, diagnostics, history, or crash
information to the maintainer. No public project website is currently operated,
so there is no project website analytics deployment to describe.

Network providers still receive ordinary connection information such as the
user's IP address, request time, TLS/network metadata, and the application's or
browser's user agent. Their own privacy terms apply.

## Network services used

The app contacts services only for a selected feature or an enabled check:

| Service | When used | Data sent |
|---|---|---|
| YouTube and YouTube Music | Search, metadata resolution, playback-page extraction, and download operations | URLs, search terms, video/playlist/channel identifiers, request headers, and configured authentication cookies when needed |
| Spotify (`open.spotify.com`, Spotify Accounts/Web API) | Resolve Spotify links and metadata when direct credentials are configured | Spotify URLs/IDs and, for the Web API, the configured client credentials to Spotify's token endpoint |
| User-configured Spotify proxy | Resolve Spotify metadata when a proxy URL is configured | Spotify URL/ID and the optional `X-App-Token` to that operator |
| GitHub API and release pages | Automatic/manual application update checks and opening a selected release | Repository/version request and normal connection metadata; no app history or cookies |
| PyPI | Automatic/manual component version checks; source-mode upgrades only after user action | Monitored package names and normal connection metadata |
| MusicBrainz | Optional metadata lookup/enrichment | Track title, artist, album, duration, and app user agent |
| Cover Art Archive | Optional online artwork lookup | MusicBrainz release identifier and normal connection metadata |
| Lyrics providers used by `syncedlyrics`, with a Genius fallback | Optional lyrics feature, disabled by default | Track title and artist search terms; provider selection may change with that dependency |
| SponsorBlock through yt-dlp | Optional segment removal | Video identifier and selected segment categories |
| Other user-selected sites supported by yt-dlp or the universal extractor | URL inspection/download | The selected URL, required headers, and cookies configured for that site |

Spotify audio is not downloaded from Spotify. Spotify supplies metadata that
the app uses to search for and match a separate source, normally YouTube or
YouTube Music. See `docs/legal/acceptable-use.md` for use responsibilities.

## Data kept on the device

The per-user application-data directory is `%APPDATA%\.bananaflow` on Windows,
`~/Library/Application Support/BananaFlow` on macOS, and
`$XDG_CONFIG_HOME/bananaflow` or `~/.bananaflow` on Linux. It can contain:

- `config.json`: preferences, paths, optional proxy/API configuration, and UI
  state;
- `downloads.db`: download history;
- `update_state.json`: dismissed or snoozed update prompts;
- `logs/bananaflow.log` plus rotating backups: local operational logs;
- `app_cookies.dpapi` on Windows: a current-user DPAPI-protected, minimized
  YouTube cookie store (`app_cookies.txt` with owner-only permissions on other
  platforms);
- `browser_profile/`: the dedicated persistent profile used by the sign-in
  wizard;
- local caches, pending Tag Editor drafts, and recovery/backup data used by
  enabled workflows; and
- the downloaded media and exported reports at locations the user selected.

Cookie values are not intentionally displayed in diagnostics or written to
logs. Central redaction also covers common credential and authorization forms.
The app retains only the cookie names and YouTube/Google domains needed for
yt-dlp's YouTube session rather than copying a whole Google account jar.
Plaintext is materialized into an owner-only temporary file only while a
consumer runs and is deleted afterward. Cookie storage, configuration, logs,
and the dedicated browser profile are restricted to the current user where
supported. Windows DPAPI protects against offline or cross-user access on that
computer; it does not protect against malware already running as the same user.
Cookies can still grant account access: treat them like passwords, avoid
sharing them, and consider a dedicated account.

The app browser profile is separate from the user's normal Chrome/Edge/etc.
profile. If the user instead enables direct browser-cookie reading, yt-dlp reads
the selected normal browser's cookie store for the requested operation; BananaFlow's
delete action disables that setting but does not delete or sign out the normal
browser.

## Diagnostics, logs, and reports

Logs rotate locally (up to 5 MB each with three backups). Startup diagnostics,
YouTube Doctor output, error details, and CLI diagnostics are local unless the
user chooses to copy or share them. The app has no automatic support-bundle or
crash-report upload. Exported history or metadata reports go only to the path
the user selects.

Redaction is defense in depth, not a guarantee against every future token or
third-party message format. Before sharing any log or report, inspect it and
remove URLs, paths, media names, account information, and any remaining secret.
Never attach credentials or cookies to a public Issue.

## Deleting data

- In Settings, **Delete stored sign-in data** asks for confirmation and deletes
  BananaFlow's protected cookie store (including a legacy plaintext store, if
  present) and dedicated `browser_profile/`. Locked data is
  reported and left for a safe retry.
- The History screen can clear download-history records.
- Downloaded files and user-selected exports must be deleted from their chosen
  folders by the user.
- To remove all remaining local app state, exit BananaFlow and delete its per-user
  application-data directory. Back up anything needed first.

Uninstalling the Windows application removes installed program files but
intentionally leaves the per-user app-data directory and downloaded media in
place. Delete stored sign-in data before uninstalling, or remove the app-data
directory afterward, if those files should not remain.

## Changes and questions

Service behavior and terms can change. Material app behavior changes should be
reflected in this document. For security-sensitive questions, follow
`SECURITY.md` and do not post private data publicly.
