# ADR — Playwright / Chromium packaging

Status: **Accepted decision**  
Decision date: 2026-07-17  
Current authority: this ADR records the decision rationale; current packaging code and release docs define what ships now.

## Decision

Keep a bundled Playwright Chromium payload as the packaged baseline while using cheaper non-browser paths where they can be proven reliable without removing functionality.

The decision was made because several BananaFlow workflows genuinely require DOM/JavaScript execution, and the isolated Cookie Wizard requires a controlled headed browser. Removing Chromium from the default package would trade download/install size for feature loss, first-use network dependency or untested system-browser variance.

## Feature dependency summary at decision time

Browser-backed paths included:

- Spotify track/album/playlist/artist metadata scraping;
- Cookie Wizard interactive sign-in using a dedicated profile;
- generic site network interception/listing fallbacks;
- YouTube channel/tab fallback discovery where cheaper extraction fails.

A YouTube channel/tab probe using yt-dlp/HTTP was identified as a good partial optimization because it does not require DOM execution for the common path. That optimization was compatible with keeping Chromium as fallback.

## Alternatives considered

### Use installed Edge/Chrome first

Potentially smaller package, but introduces per-user browser-version/policy/extension variance and increases uncertainty for the authentication-sensitive Cookie Wizard. It also requires strict isolated-profile discipline so BananaFlow never accidentally uses the user's normal browser profile.

### Download a browser pack on first use

Reduces initial package size, but adds a large first-use download, new progress/failure/retry UX and an offline regression for browser-dependent features.

### Replace browser paths with HTTP/yt-dlp

Use when a real reliability comparison proves parity for a particular feature. This is an optimization strategy, not justification to remove the browser globally.

### Full/Lite editions

Would create a second product/release/support matrix and a Lite edition missing many differentiated features. Rejected as unnecessary complexity.

## Historical measurement evidence

The point-in-time package/runtime measurements that informed the decision are retained in [`../performance/PACKAGE_AND_RUNTIME_PROFILE.md`](../performance/PACKAGE_AND_RUNTIME_PROFILE.md). They describe the July 2026 pre-release build and must not be presented as current Stable package sizes.

## Consequences

- Packaged downloads are larger because Chromium/Playwright are substantial dependencies.
- Browser/runtime payload is loaded only when a feature needs it; presence on disk is not the same as idle runtime cost.
- Packaging/release work must keep the browser revision/license/source treatment current.
- Browser-backed features need degradation behavior for source installs where Chromium is not installed.
- Future attempts to remove/replace the bundled browser require fresh correctness/reliability/package-size evidence and explicit architecture review.
