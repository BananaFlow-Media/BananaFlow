# Releasing BananaFlow

The maintainer's guide to cutting a BananaFlow release. Every release is
built by CI from a tag, attached to a **draft** GitHub Release, manually
verified, and only then published by the maintainer.

## Version model

`version.py` is the single source of truth:

| Constant | Example | Used by |
|---|---|---|
| `__version__` | `1.0.0` | Numeric SemVer core; Windows `FixedFileInfo`; macOS `CFBundleVersion` |
| `PRERELEASE` | `"beta.1"` / `None` | Pre-release suffix |
| `FULL_VERSION` | `1.0.0-beta.1` | Git tag (`v` + this), artifact names, updater, About, `--version` |
| `PEP440_VERSION` | `1.0.0b1` | `pyproject.toml` |

`tests/test_p0_gates.py::TestVersionConsistency` fails the build if any
representation drifts. To bump a version:

1. Edit `__version__` and/or `PRERELEASE` in `version.py`.
2. Set `pyproject.toml` `version` to the new `PEP440_VERSION`.
3. Update the `Version reference: BananaFlow \`…\`` line in
   `THIRD_PARTY_NOTICES.md`.
4. Add the new section to `CHANGELOG.md`.
5. Run `python scripts/run_isolated_tests.py` — the drift guard must pass.

## Release procedure

1. **Pre-flight (on `main`, green CI):**
   - Tests, Security and Repository scans workflows are all green.
   - `THIRD_PARTY_NOTICES.md` names the component versions the build
     will actually stage (FFmpeg, Deno, PO Token Provider, Python deps).
   - `CHANGELOG.md` has an entry for this version.
2. **Tag:** create an annotated tag `v<FULL_VERSION>` (e.g.
   `v1.0.0-beta.1`) on the release commit and push it. Both release
   workflows refuse to build if the tag does not exactly match
   `version.FULL_VERSION`.
3. **CI builds:**
   - *Build Windows release* — PyInstaller one-folder build, smoke tests
     (`--version`, `--doctor`, GUI launch), portable ZIP, Inno Setup
     installer, SHA-256 checksums, SBOM, build attestations; attaches
     everything to a draft release.
   - *Build macOS release* — arm64 `BananaFlow.app` + DMG, ad-hoc
     signed, attested; attaches to the same draft.
4. **Manual acceptance (blocking):** download the built artifacts from
   the draft release onto a clean machine and verify:
   - checksums match `SHA256SUMS*.txt`;
   - installer installs, launches, uninstalls cleanly; portable ZIP
     runs; DMG mounts and the app opens (right-click → Open the first
     time — ad-hoc signature);
   - `bananaflow-cli --version` prints the release version;
     `bananaflow-cli --doctor` passes its blocking checks;
   - a real download, a conversion and a tag-editor apply/undo cycle
     succeed;
   - the EXE properties / `Info.plist` show the correct product,
     publisher, version and copyright;
   - app data is created only under the BananaFlow app-data directory.
5. **Release notes:** highlights, known limitations, the unsigned-binary
   guidance (SmartScreen / Gatekeeper), checksum verification
   instructions, and a link to the corresponding source (the tag).
   Prereleases are marked **pre-release**.
6. **Publish:** the maintainer presses Publish. Nothing publishes
   automatically.
7. **Website follow-up (after publishing):** the official website —
   <https://bananaflow.bananaflow-media.workers.dev/> — builds its
   download pages from a verified snapshot of *this* repository's
   GitHub Releases. The snapshot is refreshed by a scheduled job in the
   website project and only promotes a release once at least one
   channel verifies (asset names, sizes and published SHA-256 values
   must match), so a freshly published release does not appear on the
   site instantly. Before announcing a release, confirm that
   <https://bananaflow.bananaflow-media.workers.dev/en/download/> offers
   the new version and that its checksum matches `SHA256SUMS*.txt`. If
   it still shows the previous version some hours later, the snapshot
   refresh failed verification — that is a website-project issue, not a
   reason to re-tag here.

## Rollback

- Before the tag: nothing public exists — fix and re-run.
- After the tag, before publish: delete the draft release and the tag,
  fix, re-tag.
- After publish: never delete a published release's assets (the source
  offer references them). Mark the release as a pre-release if it must
  be de-emphasized, and publish a superseding patch release.

## Signing status

Windows binaries are currently unsigned and macOS builds are ad-hoc
signed, not notarized. Checksums and GitHub build attestations are the
verification story until code signing is procured. Release notes must
state this plainly.
