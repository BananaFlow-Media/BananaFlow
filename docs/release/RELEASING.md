# Releasing BananaFlow

Status: **Current / normative maintainer procedure**

Every public release is built from a versioned tag by CI, assembled as a draft release, manually verified, and only then published by the maintainer.

## Version model

`version.py` is the source of truth for the semantic version core, optional prerelease suffix, public `FULL_VERSION`, PEP 440 form and Windows product metadata. Tests enforce consistency with project metadata/updater/application version fields.

When cutting a version:

1. update `version.py` (and `pyproject.toml` representation required by the version gate);
2. update `THIRD_PARTY_NOTICES.md` release/version references where exact release inventory is recorded;
3. add/update `CHANGELOG.md`;
4. run all release gates below.

Do not hard-code the new “latest version” into unrelated manuals/security policy. Those documents should point to release/version sources so they do not immediately drift.

## Release pre-flight

### Code/test gates

- Windows blocking test matrix is green; non-blocking platform feedback reviewed.
- Security/repository scan workflows are green or any advisory/non-blocking result is understood.
- `python scripts/run_isolated_tests.py` passes on the release candidate.
- `python scripts/run_network_tests.py` is run from a normal network before release for live third-party compatibility evidence.

### Documentation gate

Run:

```bash
python scripts/check_documentation.py
```

Then explicitly verify:

- `CHANGELOG.md` has the release entry and no misleading future/past status;
- `SECURITY.md` still describes the intended support policy without a stale hard-coded latest version;
- `PRIVACY.md` matches current network services, retained local data, sign-in behavior and official-site separation;
- English/Hebrew user manuals agree on capabilities, setup, Spotify search vs URL import, authentication and limitations;
- no current document says Stable is still a future milestone when `PRERELEASE` is `None`;
- internal Markdown file references resolve;
- `THIRD_PARTY_NOTICES.md`, `SOURCE_OFFER.md`, packaging README files and the SBOM/release inputs describe the actual staged components;
- any persisted schema/path changes have migration documentation/tests;
- AI/context/documentation maps still point to current sources of truth.

### Packaging/license inputs

- Review staged FFmpeg, Deno, PO-provider, browser/runtime and Python dependency versions against release documentation.
- Ensure no generated/staged binary was accidentally committed.
- Verify required license/source bundle files are packaged.
- Generate/verify SBOM and checksum assets.

## Tag and CI build

1. Create an annotated tag `v<FULL_VERSION>` on the release commit and push it.
2. Release workflows reject a tag/version mismatch.
3. Windows CI builds the one-folder application, portable ZIP, installer, checksums, SBOM/attestation evidence and smoke tests, then attaches assets to a **draft** release.
4. macOS release workflow contributes its current experimental package/artifacts to the same release when applicable.

## Manual acceptance — blocking

Download the **CI-produced draft artifacts** onto a clean/representative machine and verify at minimum:

- published SHA-256 values match downloaded artifacts;
- installer installs/launches/uninstalls; portable build runs;
- `bananaflow-cli --version` prints the release version;
- `bananaflow-cli --doctor` passes its blocking checks on the packaged build;
- a real download succeeds;
- a conversion succeeds and output verifies;
- a Tag Editor Apply + Undo Applied Batch cycle succeeds on disposable fixtures;
- expected product/version/publisher metadata appears in the executable/package;
- app data is created only in documented per-user locations;
- critical Hebrew/English UI and the YouTube Doctor/manual QA surfaces render correctly when changed by the release.

## Release notes

Include:

- user-visible highlights/fixes;
- known limitations;
- supported-platform/signing status;
- checksum verification guidance;
- corresponding source/tag link;
- migration/action-required notes when applicable;
- prerelease marking when `PRERELEASE` is not `None`.

## Publish

Publication is a maintainer action after the draft passes manual acceptance. Do not auto-publish merely because build CI is green.

After publication, confirm the official website's verified release snapshot/download page has picked up the new version and matching checksums before announcement. A lagging/failed website snapshot is a website-project issue, not a reason to retag an unchanged application release.

## Rollback / bad release

- Before tagging: fix and rerun.
- Tagged but unpublished draft: delete the draft/tag if appropriate, fix and create the corrected tag.
- Published: do not silently replace uploaded assets. Publish a superseding patch/security release and clearly mark/de-emphasize the bad release as appropriate.

## Signing status

Current signing/notarization status must be stated in release notes and user documentation. Checksums/build attestations provide useful integrity/provenance evidence but are not a substitute for platform code-signing identity.
