# Governance

Status: **Current / normative**

This document describes BananaFlow's actual governance model rather than an aspirational committee structure.

## Current model: single maintainer

BananaFlow currently has one active maintainer, listed in [`MAINTAINERS.md`](MAINTAINERS.md). The maintainer has final responsibility for project direction, architecture decisions, release publication and what is merged. There is currently no steering committee or voting body.

## Decision-making

- **Routine changes** — bug fixes, small features, documentation and maintenance are reviewed/merged at maintainer discretion subject to repository gates.
- **Large or architecture-affecting changes** — should start with an Issue/Discussion or another explicit design record before a large implementation diff, so the project does not discover basic disagreement only after the work is complete.
- **Security/privacy/safety-sensitive changes** — authentication/cookies/secrets, update/downloaded-code behavior, destructive file operations, runtime providers and similar trust-boundary changes require explicit review against the security/threat/documentation rules even if the code diff is small.
- **Release decisions** — follow [`docs/release/RELEASING.md`](docs/release/RELEASING.md). A green build does not publish a release automatically; blocking manual acceptance remains a maintainer responsibility.
- **Documentation/AI policy changes** — changes to repository-wide sources of truth, Code → Documentation mapping or canonical AI instructions are governance/process changes and should be reviewed as such rather than quietly changed as part of an unrelated feature.

## Conflicts of interest / recusal

A maintainer should not present a self-interested decision as an independent review. When a change/report directly concerns the maintainer's own conduct, account, security-sensitive contribution or another material conflict, document the limitation and seek an independent qualified reviewer when one is reasonably available. With only one maintainer, BananaFlow cannot promise an internal independent adjudication body that does not exist.

## Adding maintainers

Additional maintainers can be invited based on sustained, technically sound and trustworthy contribution plus demonstrated respect for the project's safety, review and community standards. An invitation is maintainer-initiated and becomes effective only after:

1. the person accepts;
2. [`MAINTAINERS.md`](MAINTAINERS.md) records the role/scope;
3. `.github/CODEOWNERS`/repository permissions are updated as appropriate; and
4. any sensitive release/security permissions are granted deliberately rather than implicitly with ordinary review access.

There is no contribution-count threshold that automatically grants maintainer status.

## Removing or reducing maintainer access

Maintainer access may be reduced/removed for resignation, prolonged inactivity where access is no longer needed, repeated violation of project/security/community policy, compromised credentials or loss of the trust required for release/security authority. Repository access, CODEOWNERS and `MAINTAINERS.md` should be brought back into agreement promptly.

## Inactivity and continuity

Because the project currently depends on one maintainer, prolonged unavailability is a real continuity risk. If the project adds maintainers, release/security credentials and ownership responsibilities should be distributed intentionally so ordinary maintenance does not depend on a single personal machine/account. Until then, this document does not pretend there is an automatic successor.

## Code of Conduct enforcement

Community conduct is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Enforcement is normally handled by the active maintainer(s). Where a complaint concerns the only maintainer, the limitation described under conflicts/recusal applies; platform-level abuse/reporting mechanisms remain independent of BananaFlow governance.

## Changing governance

Governance changes require an explicit PR and discussion/review. Update this document, `MAINTAINERS.md`, CODEOWNERS/permissions and any related security/release responsibilities together when the real governance model changes.
