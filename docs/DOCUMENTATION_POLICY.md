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

Review every matching row. A path listed under **review** is satisfied when the
document changes in the PR or its exact repository path is listed under
“Relevant documentation updated/reviewed” in the PR body. **Update all** paths
must change. **Update one** requires at least one listed path to change. The
machine gate verifies that every executable rule below exists here and names
the same documents, so the policy and CI map cannot silently lose a domain.

| CI rule / changed area | Enforced documentation impact |
|---|---|
| <!-- impact-rule: downloader/reliability --> Downloader, orchestration, retry and yt-dlp options | Review `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`, `docs/architecture/overview.md`, `docs/testing/TESTING.md`. |
| <!-- impact-rule: CLI --> `cli.py` | Update one of `docs/user-guide/cli.md`, `docs/user-guide/user-manual.md`; review both `docs/user-guide/cli.md`, `docs/user-guide/user-manual.md`. |
| <!-- impact-rule: authentication/privacy --> Cookies, authentication, browser sessions and YouTube Doctor | Review `SECURITY.md`, `PRIVACY.md`, `docs/security/threat-model.md`, `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`. No global no-impact bypass. |
| <!-- impact-rule: Spotify/search --> Spotify, YouTube Music and search integration | Review `docs/user-guide/spotify-proxy-api.md`, `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`, `PRIVACY.md`, `docs/architecture/overview.md`. |
| <!-- impact-rule: Tag Editor safety --> Tag Editor apply, restore, rename, delete, drafts and backups | Review `docs/architecture/tag-editor-safety.md`, `docs/architecture/tag-editor-undo-rollback-guarantees.md`, `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`, `docs/design/tag-editor/current-design.md`. No global no-impact bypass. |
| <!-- impact-rule: Tag Editor UI/actions --> Tag Editor panels and actions | Review `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`, `docs/design/tag-editor/current-design.md`, `docs/accessibility/ACCESSIBILITY.md`, `docs/i18n/TRANSLATING.md`. |
| <!-- impact-rule: persistence/config --> Configuration, migrations, history, queues, caches, stores and journals | Review `docs/migrations/README.md`, `PRIVACY.md`, `docs/architecture/overview.md`, `docs/user-guide/user-manual.md`. No global no-impact bypass. |
| <!-- impact-rule: visible UI --> Visible `ui/**` behavior | Review `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`, `docs/accessibility/ACCESSIBILITY.md`, `docs/i18n/TRANSLATING.md`. |
| <!-- impact-rule: user-facing translations --> English/Hebrew string tables | Review `docs/i18n/TRANSLATING.md`, `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`. |
| <!-- impact-rule: dependency inventory --> `requirements*.txt` and constraints | Update all of `THIRD_PARTY_NOTICES.md`; review `docs/security/supply-chain.md`, `docs/release/RELEASING.md`. No global no-impact bypass. |
| <!-- impact-rule: packaging/dependencies --> Packaging, `pyproject.toml`, release workflows and build/staging scripts | Update one of `docs/release/RELEASING.md`, `docs/security/supply-chain.md`; review `docs/release/RELEASING.md`, `THIRD_PARTY_NOTICES.md`, `SOURCE_OFFER.md`, `docs/security/supply-chain.md`. No global no-impact bypass. |
| <!-- impact-rule: updaters/components --> Update checker and runtime component updates | Review `docs/user-guide/user-manual.md`, `SECURITY.md`, `PRIVACY.md`, `docs/architecture/secure-component-updater.md`, `docs/security/threat-model.md`. No global no-impact bypass. |
| <!-- impact-rule: external metadata services --> Metadata APIs, clients and scrapers | Review `PRIVACY.md`, `docs/user-guide/user-manual.md`, `docs/security/threat-model.md`, `docs/legal/acceptable-use.md`. No global no-impact bypass. |
| <!-- impact-rule: version/release channel --> `version.py` | Update all of `CHANGELOG.md`; review `SECURITY.md`, `docs/release/RELEASING.md`. No global no-impact bypass. |
| <!-- impact-rule: platform/package support --> Test/release workflows and platform packaging | Review `README.md`, `docs/release/RELEASING.md`, `SECURITY.md`, `docs/user-guide/user-manual.md`, `docs/user-guide/user-guide-he.md`, `docs/AI_CONTEXT.md`, `docs/testing/TESTING.md`. No global no-impact bypass. |
| <!-- impact-rule: documentation ownership/layout --> Documentation entry points, policy, enforcement and AI adapters | Update one of `docs/README.md`, `docs/DOCUMENTATION_POLICY.md`, `docs/AI_CONTEXT.md`; review all three paths. |

## Pull-request documentation declaration

Every PR must select at least one documentation-impact category in the PR
template. “No documentation impact” is accepted only when the checkbox is
selected and the reason line contains a meaningful explanation (not the
template comment or a placeholder). It cannot bypass the sensitive rules
marked above. For all other mapped changes, list every reviewed path exactly;
an unrelated Markdown edit does not satisfy the map. The declaration never
overrides a hard consistency failure.

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
