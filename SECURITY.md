# Security Policy

## Supported versions

Security reports are accepted for the latest public BananaFlow release
and for the current `main` development line. At the time this policy was
written, `v0.1.0` is the latest public release. The `v0.2.0` draft and its
artifacts are unpublished historical evidence and are not a supported release.
When a newer public Beta or Stable release supersedes an older version, users
should upgrade; fixes are not guaranteed to be backported.

The official planned Beta platform is Windows 10/11 x64. Other platforms are
experimental or unsupported as described in the release documentation.

## Reporting a vulnerability

Do not put credentials, cookies, personal data, exploit details, or an
unpatched vulnerability in a public Issue, Discussion, log paste, or pull
request.

The official reporting channel is GitHub private vulnerability reporting for
this repository: `Security` > `Advisories` > `Report a vulnerability`. It is
enabled and owner-approved as the primary route. If the form is ever
unavailable, open a public Issue containing only the title **Security contact
requested** and no technical details; a maintainer will arrange a private
channel. That fallback is intentionally not a place to submit the report
itself.

In a private report, include:

- affected version, commit, platform, and installation type;
- a concise impact statement and reproducible steps;
- the smallest safe proof of concept;
- whether authentication or user interaction is required;
- suggested mitigation, if known; and
- a way to contact you privately.

Use synthetic data. Never send a live account cookie, API key, access token,
password, private media, or an unredacted `config.json`. Review logs before
sharing them even though the app applies centralized redaction.

## What to expect

Maintainers will acknowledge and investigate reports as capacity permits, may
ask for clarification, and will coordinate disclosure for confirmed issues.
This project does not promise a response or remediation SLA. Please avoid public
disclosure until a fix or practical mitigation is available and coordinated.

## Security behavior

- No live credential is intended to be committed as a default. Optional
  Spotify and YouTube API values can be supplied through the documented
  environment variables or local configuration.
- Log, diagnostic, error, and configuration-export paths use centralized
  redaction for credentials, authorization headers, cookies, sensitive query
  parameters, known token shapes, and local profile paths.
- BananaFlow-owned cookie files and its dedicated sign-in browser profile are
  restricted to the current user where the operating system supports it.
- Settings provides a confirmed **Delete stored sign-in data** action that
  removes the BananaFlow cookie file and dedicated sign-in profile. It does not
  alter cookies in the user's normal browsers.
- Dependency auditing, secret scanning, dependency review, Dependabot, and
  Python CodeQL are configured in `.github/`.

These controls reduce risk but cannot guarantee that every future secret shape
or third-party error message is recognized. Treat logs and diagnostics as
potentially sensitive and inspect them before sharing.

## Official distribution channels

BananaFlow has exactly two official channels, and any other site offering
"BananaFlow" downloads is not ours:

- the official website, <https://bananaflow.bananaflow-media.workers.dev/>
  (its download pages link to the release assets below and publish the
  matching checksums); and
- the GitHub Releases of this repository,
  <https://github.com/BananaFlow-Media/BananaFlow/releases>, which host
  the artifacts themselves.

The website publishes its own
[security page](https://bananaflow.bananaflow-media.workers.dev/en/security/)
for end users. A vulnerability **in the website itself** — as opposed to
in the application — may be reported either through that page's private
email route or through this repository's private advisory form; both
reach the same maintainer. As here, never open a public Issue for one.

## Updates, signing, and checksums

The app checks the public GitHub Releases API for application updates when
update checks are enabled. Component checks query PyPI. The packaged updater
opens a release page; it does not silently install a new application build.

Current Windows packages are not Authenticode-signed, and current macOS work is
not a supported public Beta target. A checksum can detect accidental corruption
or mismatch against a separately trusted checksum, but it does not replace a
digital signature or establish who produced a file. Verify release provenance,
filenames, versions, and published SHA-256 values before running an artifact.

Security fixes will be issued as new versioned releases rather than by replacing
previously uploaded artifacts. A security report does not authorize testing
against third-party services or other users' accounts.
