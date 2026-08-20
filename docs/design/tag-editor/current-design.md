# Tag Editor — current design decisions

Status: **Current design record**  
Last major redesign review: 2026-08-02

The old HTML prototype under `_reference/` is historical inspiration, not the source of truth. Current product behavior and automated surface contracts take precedence.

## Current workspace rules

- **Include subfolders** affects the scan scope.
- Incremental refresh and full rescan are separate actions.
- Search/filter changes visible rows only; it does not change Apply scope.
- Table selection controls the scope of editing actions.
- Pending non-excluded/non-blocked proposals control Apply scope.
- Every implemented/wired feature must have a discoverable UI/keyboard path; do not hide working functionality merely to match an obsolete prototype.

## Inspector information architecture

### Edit

- Fields
- Artwork
- Lyrics
- ReplayGain
- File Properties

### Tools

- Auto Arrange
- All Actions
- Duplicates
- Online Metadata

### Check

- Pending Changes
- Problems
- External Changes

Older shortcut pages for filename/cleanup/rename behavior were consolidated under **All Actions** rather than deleting capability. Duplicate scanning/results/management have a single discoverable tools surface.

## Safety boundaries

Design work must not bypass the Tag Editor disk-safety architecture:

- proposal-first editing;
- Review before Apply;
- backup/journal/verify-before-replace;
- external-change blocking/review;
- safe move/rename/delete paths;
- recovery/Undo Applied Batch semantics.

See `docs/architecture/tag-editor-safety.md`.

## Accessibility / RTL

- Every interactive inspector/action row is keyboard reachable and exposes accessible semantics.
- Inspector state clears/updates when selection changes; stale previous-file values/actions must not remain active.
- Hebrew layout is RTL while technical file/path/identifier content stays readable.
- High-DPI/high-contrast behavior remains part of the acceptance surface.

## Surface regression protection

The Tag Editor surface-contract tests exist to prevent quiet loss of implemented functions. New design changes should update the contract intentionally rather than disabling it to fit a mockup.

## Historical prototype

See [`_reference/README.md`](_reference/README.md) and the archived July parity report for the old HTML-prototype context. Those files explain what was built and why, but they do not decide current UI disputes.
