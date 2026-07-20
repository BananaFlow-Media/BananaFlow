# Governance

This document describes the project's actual current governance
model.

## Current model: single maintainer

BananaFlow is currently maintained by a single maintainer (see
`MAINTAINERS.md`), who has final say on the project's direction,
architecture decisions, and what gets merged. This reflects the project's
actual current state, not an aspiration — there is no steering committee,
voting process, or maintainer team today.

## Decision-making

* **Day-to-day changes** (bug fixes, small features, documentation):
  reviewed and merged by the maintainer at their discretion.
* **Larger or architecture-affecting changes**: should start as a
  Discussion or Issue before a large PR is written, so direction is
  agreed before significant work is invested (see `CONTRIBUTING.md`'s
  "large proposal process").
* **Security-sensitive changes** (authentication, cookies, secrets
  handling, the PO Token provider path, download-safety behavior such as
  Conservative Mode): require explicit maintainer review regardless of
  contributor seniority.
* **Release decisions** (what ships in a Beta/Stable release, when):
  follow the release process in `docs/release/RELEASING.md`; every
  release is manually verified and published by the maintainer.

## Adding maintainers

The project may add additional maintainers in the future as it grows.
There is no fixed process for this yet; it would be a maintainer-initiated
invitation based on sustained, trusted contribution, recorded as an update
to `MAINTAINERS.md` and `.github/CODEOWNERS`.

## Code of Conduct enforcement

Handled per `CODE_OF_CONDUCT.md`, by the maintainer(s) listed in
`MAINTAINERS.md`.

## Changing this document

Governance changes are themselves subject to the same review process as
any other change — open a PR against this file, and expect maintainer
discussion before it's adopted.
