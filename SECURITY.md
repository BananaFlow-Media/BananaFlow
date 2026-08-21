# Security Policy

Status: **Current / normative**

## Supported versions

Security reports are accepted for:

- the **latest public BananaFlow release**; and
- the current `main` development line.

Older releases are not guaranteed to receive backports. Users should normally upgrade to the latest public release when a security fix is published. `version.py` is the source of truth for the version being built; do not copy a hard-coded “latest version” into this policy.

Platform support is product support, not a signing claim: Windows 10/11 x64 and macOS Apple Silicon are supported packaged targets. Linux is a supported source-install platform and is expected to work normally when its dependencies are installed; an official Linux installer/package is not published yet.

## Reporting a vulnerability

Do **not** put an unpatched vulnerability, exploit details, credentials, cookies, personal data or private diagnostics in a public Issue, Discussion, log paste or pull request.

The official reporting channel is **GitHub private vulnerability reporting** for this repository:

**Security → Advisories → Report a vulnerability**.

If that private form is unavailable, open a public issue containing only the title **Security contact requested** and no technical details. A maintainer will arrange a private channel; the public issue is not a place to submit the report itself.

A useful private report includes:

- affected BananaFlow version/commit, platform and install type;
- concise impact and threat scenario;
- reproducible steps using synthetic data;
- the smallest safe proof of concept;
- whether authentication or user interaction is required;
- suggested mitigation, if known; and
- a private way to contact the reporter.

Never send live cookies, passwords, access tokens, API keys or private media as reproduction data.

## What to expect

Maintainers review reports as capacity permits and may request clarification or additional safe evidence. The project does not promise a response/remediation SLA. For confirmed issues, please coordinate public disclosure until a fix or practical mitigation is available.

## Security boundaries and current controls

BananaFlow treats authentication material, downloaded-code/runtime components and destructive filesystem paths as security-sensitive surfaces. The current threat model is in [`docs/security/threat-model.md`](docs/security/threat-model.md).

Key controls include:

- centralized redaction for common credentials, cookies, authorization headers, sensitive query parameters and local profile paths in logs/diagnostics;
- a BananaFlow-owned isolated browser profile for the sign-in helper rather than silently reusing a normal browser profile;
- minimized BananaFlow-owned cookie storage, protected to the current user where supported (Windows uses DPAPI for the protected store);
- a confirmed **Delete stored sign-in data** action that removes BananaFlow-owned cookie/profile state without signing the user out of their normal browsers;
- bounded retry/reliability policy that distinguishes transient failures from permanent/authentication failures;
- dependency/security scanning and repository security workflows under `.github/`;
- versioned release artifacts with published SHA-256 checksums, SBOM and build-attestation evidence as configured by the release process;
- authenticated GitHub release-asset digests, compatibility checks, bounded safe unpacking, isolated health checks and atomic last-known-good fallback for approved packaged-component overlays;
- Tag Editor backup/journal/verify-before-replace invariants for destructive metadata operations.

These controls are defense in depth, not guarantees. Treat logs and diagnostics as potentially sensitive and inspect them before sharing.

## Official distribution channels

BananaFlow has two official application distribution entry points:

1. the official website, <https://bananaflow.bananaflow-media.workers.dev/>, whose download pages link to verified release artifacts; and
2. this repository's GitHub Releases, <https://github.com/BananaFlow-Media/BananaFlow/releases>, which host the artifacts.

Downloads offered elsewhere are not official BananaFlow releases.

## Updates, signing and checksums

BananaFlow checks public release metadata when update checks are enabled. Packaged users are directed to the official release/download path for application updates; the app does not silently replace itself with an unverified build. A user can separately approve a downloader-component update from the official repository channel. That path verifies GitHub's SHA-256/size metadata and the channel manifest before preparing a versioned per-user overlay; it never rewrites the installed application and takes effect only after restart. Before an approved overlay is reused, its authenticated channel control record is refreshed at most once per 24 hours; a revoked, disabled, stale-unverifiable or app-incompatible overlay is not activated, and the bundled components remain available.

Windows packages are currently unsigned with Authenticode. The supported macOS package can also have signing/notarization limitations documented in the release notes. A checksum detects mismatch/corruption relative to a separately trusted published value, but it is not a substitute for publisher identity or code signing.

Published release assets are immutable evidence: security fixes ship as new versioned releases rather than silently replacing previously published binaries.

## Scope

Security reports about BananaFlow's own code, packaged application, update/release behavior, credential handling and documented website integration are welcome through the private route. A report does not authorize testing against third-party services, other users' accounts, or infrastructure you do not own or have permission to test.
