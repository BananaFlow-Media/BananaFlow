# Releasing BananaFlow

Status: **Current / normative maintainer procedure**

Every public release is built from a versioned tag by CI, assembled as a draft release, manually verified, and only then published by the maintainer.

Official website: <https://bananaflow.bananaflow-media.workers.dev/>.

## Version model

`version.py` is the source of truth for the semantic version core, optional prerelease suffix, public `FULL_VERSION`, PEP 440 form and Windows product metadata. Tests enforce consistency with project metadata/updater/application version fields.

When cutting a version:

1. update `version.py` (and `pyproject.toml` representation required by the version gate);
2. update `THIRD_PARTY_NOTICES.md` release/version reference so its shipped compliance snapshot matches `FULL_VERSION`;
3. add/update `CHANGELOG.md`;
4. run all release gates below.

Do not hard-code the new “latest version” into unrelated manuals/security policy. Those documents should point to release/version sources so they do not immediately drift.

## Release pre-flight

### Code/test gates

- The blocking Windows and Ubuntu test matrices are green across the supported Python versions.
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
4. macOS release workflow builds and attaches the supported Apple Silicon package/artifacts for the same stable or pre-release tag. Signing/notarization status is a separate release property and does not by itself make the product experimental.
5. Linux is a supported source-install platform; there is currently no official Linux installer/package artifact to attach.

### Component pin automation

The daily component-channel workflow compares the exact reviewed `yt-dlp[default]` pin with the newest non-yanked PyPI candidate. A newer candidate intentionally fails that advisory job for maintainer review; it does not create a bot-authored commit, change a pin or publish unreviewed executable code.

After a human-reviewed pin change is merged to `main`:

1. `.github/workflows/component-channel.yml` installs the release dependency set, builds the deterministic `yt-dlp` / `yt-dlp-ejs` overlay, runs focused updater tests, records build provenance and updates the official `component-channel-v1` pre-release assets (bundle first, authenticated manifest last). The same publication job also runs for an application version tag, ensuring the first packaged release that supports overlays has a channel to consume.
2. The Windows and macOS release workflows run automatically and retain full release-candidate artifacts in Actions. A branch-triggered build creates no GitHub application release.
3. Only a matching version tag creates/updates the normal draft application release, and publication remains the manual procedure below.

## Manual acceptance — blocking

Download the **CI-produced draft artifacts** onto clean/representative machines and verify at minimum:

- published SHA-256 values match downloaded artifacts;
- Windows installer installs/launches/uninstalls and the portable build runs;
- macOS Apple Silicon package opens and the main application workflow launches successfully; document any current Gatekeeper/signing first-run requirement;
- `bananaflow-cli --version` prints the release version where the CLI is part of the tested install;
- `bananaflow-cli --doctor` passes its blocking checks on the packaged build;
- a real download succeeds;
- a conversion succeeds and output verifies;
- a Tag Editor Apply + Undo Applied Batch cycle succeeds on disposable fixtures;
- when component-overlay code changes, an approved overlay activates only with a current authenticated control record and a revoked test bundle cannot activate;
- expected product/version/publisher metadata appears in the executable/package;
- app data is created only in documented per-user locations;
- critical Hebrew/English UI and the YouTube Doctor/manual QA surfaces render correctly when changed by the release.

For Linux source support, the CI matrix plus a source-install smoke on a representative Linux environment are the release evidence until an official Linux package is introduced.

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

After publication, confirm <https://bananaflow.bananaflow-media.workers.dev/> and its verified release snapshot/download page have picked up the new version and matching checksums before announcement. A lagging/failed website snapshot is a website-project issue, not a reason to retag an unchanged application release.

## Rollback / bad release

- Before tagging: fix and rerun.
- Tagged but unpublished draft: delete the draft/tag if appropriate, fix and create the corrected tag.
- Published: do not silently replace uploaded assets. Publish a superseding patch/security release and clearly mark/de-emphasize the bad release as appropriate.

## Signing status

Current signing/notarization status must be stated in release notes and user documentation. Checksums/build attestations provide useful integrity/provenance evidence but are not a substitute for platform code-signing identity.
