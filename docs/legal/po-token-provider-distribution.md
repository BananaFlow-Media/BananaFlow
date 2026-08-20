# PO Token Provider distribution — research snapshot and decision record

Status: **Historical legal/policy research + accepted technical distribution decision**  
Research date: 2026-07-17

This file records the evidence considered when deciding whether BananaFlow could transparently bundle the configured yt-dlp PO Token Provider stack. It is not legal advice, does not declare any use lawful/unlawful, and must not be treated as a permanently current interpretation of third-party terms or store policies.

## Technical facts established in the review

At the review date:

- BananaFlow was GPL-3.0-or-later and the configured `bgutil-ytdlp-pot-provider` was GPLv3-compatible for that distribution model.
- Packaging used a committed, readable staging script rather than hidden/manual copying.
- The provider plugin and matching server/script backend source were staged for the packaged build together with a Deno runtime.
- BananaFlow configured yt-dlp's provider interface; BananaFlow did not manually generate/store/inject live PO-token values in application code.
- `THIRD_PARTY_NOTICES.md` / `SOURCE_OFFER.md` carried the license/source treatment and the release SBOM represented bundled packages/components.
- Runtime review found that the upstream provider's BotGuard flow can fetch/execute challenge interpreter JavaScript supplied through YouTube's own challenge response. This mattered to the review of distribution/store policies that restrict downloaded executable scripts.

Exact current provider/runtime versions and hashes are deliberately **not** copied here; read the staging script, `THIRD_PARTY_NOTICES.md` and release SBOM for the current release.

## Options considered

### Bundle the provider stack

Best out-of-box reliability and reproducible component combination; creates packaging/license/supply-chain obligations and requires transparent disclosure/review.

### Require a user-installed provider

Smaller/less bundled default but worse setup/reliability for packaged users; does not by itself resolve policy questions about the underlying technique.

### Separate compatibility pack

Makes the provider optional and independently versioned but adds a second release/repository/support/version-drift surface.

### No provider

Reduces one contested/complex integration surface but knowingly reduces reliability for YouTube requests that require provider support.

## Accepted project decision

The project chose transparent bundling for the packaged baseline while keeping:

- readable staging/build code;
- version/source/license disclosure;
- SBOM/release verification;
- YouTube Doctor readiness reporting;
- no application-level live token logging/storage/injection;
- explicit acceptable-use and third-party-service disclaimers.

Any future change to this decision requires a fresh technical/license/policy review against **current** upstream licenses/terms and current distribution-channel requirements.

## Legal/policy caveat

Third-party terms, platform policies and law can change. The July 2026 research is retained as evidence of the project's reasoning at that time, not as a current legal opinion. `docs/legal/acceptable-use.md` remains the current user-facing responsibility statement.
