# Explicit application updates

Status: **Current / normative**

Application updates and downloader-component updates are separate operations.
The component overlay contract remains in
[`secure-component-updater.md`](secure-component-updater.md). Updating a
component does not require downloading the whole application. Both paths require
an explicit user decision before preparation and another explicit decision to
restart; checking never authorizes installation or process termination.

## Trust and artifact selection

The full application updater accepts release metadata only from the exact
`BananaFlow-Media/BananaFlow` GitHub repository over certificate-verified HTTPS.
It selects the artifact for the current operating system, architecture and
installation kind rather than executing the first release attachment. Missing,
ambiguous, malformed or unsupported artifacts fail closed. Artifact bytes must
match the official asset's exact SHA-256 and bounded size before they can be
used. Partial downloads are not installable.

This is a GitHub-repository/API trust model, not an independently signed update
manifest and not proof of Windows Authenticode or Apple Developer ID identity.
A compromise of that repository or its release authority is a residual risk.
Published build attestations are additional release evidence; their existence
must not be described as client-side verification unless the client actually
verifies them. Platform signing status remains documented in `SECURITY.md` and
the release notes. The updater never disables certificate validation, bypasses
an operating-system security prompt, or strips quarantine to force execution.
Windows preparation marks downloaded artifacts with Internet provenance.
Portable extraction preserves the archive's exact existing `Zone.Identifier`
on extracted files; it neither removes a mark nor invents one for a locally
built, unmarked archive. Native launch failures retain the Windows return/error
codes, executable existence/architecture and rollback outcome for diagnosis.
On macOS, download marking and DMG-to-application quarantine propagation use
Darwin's native extended-attribute API, preserving bounded binary attribute
bytes without following symlinks. Only `ENOATTR` means no quarantine attribute;
permission, unsupported-operation and other errors stop the update.

## Lifecycle and process boundaries

Network preparation runs off the GUI thread. A successfully prepared update
is not a command to shut down: the user can continue working and choose the
install/restart action later. The application owns the shutdown decision and
must not terminate an active download, conversion or disk-changing Tag Editor
operation merely to install an update. A vetoed or incomplete shutdown must
not start replacement or relaunch a second interactive application.

The replacement helper is a separately packaged executable. It runs outside
the installation being replaced, waits for the old process to exit, rechecks
the prepared artifact, and uses the supported platform installation path.
Windows installation detection preserves the exact historical Inno AppId,
including its literal double closing brace, and requires the matching 64-bit
uninstall registry entry and installation path. It does not rename the installer
identity, infer installation from an uninstaller alone, or accept guessed aliases.
Windows installed and portable packages remain distinct. A portable update
must not silently become an installed application. macOS updates replace the
selected application bundle, not an arbitrary user-supplied directory.
User data, media and settings are outside the replacement scope.

Prepared state is stored under `app-updates/` in per-user app data. A
versioned pointer identifies a private preparation directory containing the
artifact, copied helper and prepared metadata. Each explicit install attempt
writes a transaction plan and local status; a process-owned apply lock prevents
concurrent installers. Loading state re-verifies it and never auto-installs.

Filesystem replacement uses staged state and recovery data; failure must not
be reported as success. Windows installed updates use Inno Setup in the existing
installation scope and do not claim atomic directory rollback. Portable/macOS
updates retain a transaction-specific sibling backup of the old application.
The helper status `launched` means the operating system accepted the launch
request, not a claim that the new GUI passed a health check. Packaged test
evidence must separately confirm the candidate process/version. A rejected system permission or security prompt is a
recoverable installation failure or manual handoff, never a reason to weaken
security. The official website download option remains available throughout
as an independent manual path. Source installations and unsupported package
layouts retain that manual application-upgrade path.

## Acceptance

Unit tests cover hostile metadata, wrong hashes/sizes, unsafe archive entries,
wrong platform/layout, failure/recovery and lifecycle decisions. Qt tests cover
English/Hebrew presentation, explicit actions and shutdown vetoes. Packaged CI
must exercise real application replacement and fresh-process relaunch on
Windows and macOS; configuration-migration fixtures alone are not upgrade
evidence. PR packaging runs cannot publish application releases or component
channels. The public-release manual acceptance checklist remains blocking.
