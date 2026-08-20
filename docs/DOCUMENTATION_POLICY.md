# BananaFlow documentation policy

Status: **Current / normative**

The purpose of this policy is to prevent documentation drift. Code, tests and affected documentation are one deliverable.

## Principles

1. **One source of truth per fact.** Link to canonical data instead of copying version/status information into many documents.
2. **Current vs historical is explicit.** Current normative documents describe what BananaFlow promises now. Historical research records why a decision was made at a point in time and carries a date/status banner.
3. **Behavior changes include documentation review.** A PR that changes externally observable behavior, architecture, persistence, security/privacy, packaging or developer workflow must update the mapped documents or explicitly explain why none changed.
4. **English/Hebrew parity is a product requirement.** User-facing behavior documented in one full user guide must be reflected in the other; wording can differ naturally but capability and safety guidance must agree.
5. **End-user docs are for end users.** The main user manuals assume no programming knowledge. Build/architecture/test internals belong in contributor documentation unless a technical detail is genuinely needed to solve a user task.
6. **Do not encode “latest” as a hard-coded version unless the file owns that policy.** `version.py` owns the application version; `CHANGELOG.md` owns release history.

## Source-of-truth matrix

| Fact/domain | Source of truth |
|---|---|
| Application version/product metadata | `version.py` |
| Release history | `CHANGELOG.md` |
| Release procedure | `docs/release/RELEASING.md` |
| Platform support/distribution status | `README.md` + `docs/release/RELEASING.md`; user manuals must agree |
| Security support/reporting | `SECURITY.md` |
| Privacy/network/local data | `PRIVACY.md` |
| Architecture boundaries | `docs/architecture/overview.md` + `PROJECT_STRUCTURE.md` |
| Test policy | `docs/testing/TESTING.md` |
| Contribution workflow | `CONTRIBUTING.md` |
| User behavior | `docs/user-guide/user-manual.md` + `docs/user-guide/user-guide-he.md` |
| CLI contract | `cli.py` + `docs/user-guide/cli.md` |
| Tag Editor disk-safety invariants | `docs/architecture/tag-editor-safety.md` |
| Third-party licenses/components | `THIRD_PARTY_NOTICES.md` |
| Source availability | `SOURCE_OFFER.md` |
| Official website URL | `utils/website.py` (code), linked rather than retyped where practical |
| Documentation ownership/lifecycle | this file |

## Code → Documentation impact map

Review every matching row. “Review” means either update the document or record in the PR why the behavior described there did not change.

| Changed area | Required documentation review |
|---|---|
| `core/downloader.py`, `core/download_orchestrator.py`, retry/reliability/options | User manual EN+HE; `architecture/overview.md`; troubleshooting/reliability sections; tests |
| Spotify/YTM/YouTube scraping, matching, search | User manual EN+HE; `PRIVACY.md` when network/data flow changes; architecture; Spotify proxy contract when applicable |
| `core/search_engine.py` Spotify proxy | `user-guide/spotify-proxy-api.md`; user manual EN+HE; privacy if transmitted fields/headers change |
| Cookies/auth/browser profile/YouTube Doctor | `SECURITY.md`; `PRIVACY.md`; user manual EN+HE; threat model; tests |
| `config.py`, `config_migrate.py` | User manual settings; migration docs when persisted schema/default/meaning changes; tests |
| History/queue persistence/databases | user manual if behavior changes; migration docs for schema/path changes; privacy for retained data changes |
| Tag Editor apply/restore/rename/delete | Tag Editor safety + undo/rollback docs; user manual EN+HE; migration docs if persistence changes; tests |
| Tag Editor UI/actions | user manual EN+HE; current design document; accessibility/i18n when relevant |
| `ui/**` visible behavior | user manual EN+HE; accessibility; translation rules; screenshots in PR for visible changes |
| `cli.py` | `user-guide/cli.md`; README/user manual when discoverability or behavior changes |
| Dependency/`requirements`/`pyproject` | `THIRD_PARTY_NOTICES.md`; supply-chain doc if process changes; release docs when packaging impact exists |
| `packaging/**`, release workflows/build scripts | `docs/release/RELEASING.md`; `SOURCE_OFFER.md`; `THIRD_PARTY_NOTICES.md`; supply-chain doc; packaging README(s); platform support text if distribution changes |
| Update checker/updater/component updates | user manual; security; privacy if new network calls/data; updater architecture doc; threat model |
| New external service or endpoint | `PRIVACY.md`; user manual; threat model; acceptable-use if user responsibility changes |
| New persisted file/cache/log | `PRIVACY.md`; migration docs; architecture; deletion/retention guidance |
| Version/release channel | `CHANGELOG.md`; `SECURITY.md` policy review; release notes/docs; avoid hard-coded copies elsewhere |
| Platform/package support status | `README.md`; `docs/release/RELEASING.md`; `SECURITY.md`; EN+HE user manuals; AI context |
| New user-facing text | `ui/i18n.py` EN+HE; relevant user guide if behavior/instruction changes |
| Documentation layout/ownership | `docs/README.md`; this policy; AI context/adapters if navigation changes |

## Pull-request documentation declaration

Every PR must select at least one documentation-impact category in the PR template. “No documentation impact” is acceptable only with a short reason. CI applies conservative path-based checks to high-risk areas; the declaration does not override a failed hard consistency check.

## Document lifecycle

Use one of these statuses when a document could be mistaken for current policy:

- **Current / normative** — maintained with the code and may define requirements.
- **Decision record / accepted** — records an architectural decision; update status if superseded.
- **Historical evidence** — point-in-time research, measurements or phase reports; never an authority for current behavior.
- **Superseded** — retained for history and links to its replacement.

Historical documents should include a date, the product/revision measured when known, and a prominent statement that current code/docs take precedence.

## Review cadence

- Every behavior-changing PR: mapped documentation review.
- Every release: documentation release gate in `docs/release/RELEASING.md`.
- After changes to external services, authentication, storage or packaging: targeted privacy/security/supply-chain review.
- Periodically: run the repository documentation gate and repair all broken internal references rather than suppressing them.
