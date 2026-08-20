# Tag Editor disk-safety invariants

Status: **Current / normative**

This document defines the safety properties that current Tag Editor Apply/restore work must preserve. It replaces the old pre-implementation “Phase 1” status language; historical phase/test-count notes are not requirements.

## Core model

- Scanning creates a workspace with original on-disk state.
- Editing actions create proposals; they do not write media files immediately.
- Selection determines editing/action scope.
- Pending non-excluded proposals determine Apply scope.
- Review occurs before disk mutation.

## Binding invariants

### TE-SAFE-01 — backup before media mutation

A batch may not modify a media file until the backup target has been validated and the required backup data has been written and read back successfully. Backup failure aborts the disk-changing batch.

### TE-SAFE-02 — atomic/validated backup

Backup/journal state is written using temp → flush/fsync where supported → atomic replace → read-back/parse validation. A torn/partial destination must not be published as a valid backup.

### TE-SAFE-03 — versioned compatibility

Backup/persisted recovery formats are versioned and loaders/migrations preserve support for documented older schemas that can exist on user machines. Persisted-state changes require migration tests and documentation.

### TE-SAFE-04 — metadata write and rename are separate outcomes

A successful metadata write followed by rename failure is **partial**, not complete success. The filename proposal stays pending/recoverable.

### TE-SAFE-05 — failed rename never clears intent

Collision, lock, platform-invalid name or other rename failure must not silently clear the proposal or increment complete-success counts.

### TE-SAFE-06 — structured results

Apply exposes batch-level and per-file structured outcomes with enough information to distinguish planning/backup/write/verify/rename/cancel/partial states and to provide actionable UI messages.

### TE-SAFE-07 — preserve untouched metadata

Writing one proposed field must not rewrite/drop unrelated comments, artwork, lyrics, ReplayGain, custom fields or multivalue metadata merely as a side effect. Only explicit deltas are applied.

### TE-SAFE-08 — verify before replacing original

Changed fields/container readability are verified on the temporary copy **before** it replaces the original.

### TE-SAFE-09 — stale-operation isolation

A worker/result from an older workspace/generation must not mutate a newer workspace after root/rescan/lifecycle changes.

### TE-SAFE-10 — whole-batch rename preflight

Plan rename graphs before disk moves. Account for destination/case-insensitive collisions, case-only rename, cycles, Windows reserved names/trailing dot-space/invalid characters, root escape and locked/unavailable paths. Hazards block/report affected renames; they are not silently ignored.

### TE-SAFE-11 — durable Apply journal

Persist the complete operation plan before the first media mutation. Per-file/batch transitions are durably recorded so an interruption between steps is recoverable. Incomplete journals trigger review-first recovery; do not perform automatic destructive recovery.

### TE-SAFE-12 — same-filesystem temp-copy write

For each media file:

1. create a temporary copy on the same filesystem as the original;
2. preserve required permissions/attributes;
3. apply only the proposed metadata delta to the temp copy;
4. close handles;
5. read back and verify intended fields/container;
6. flush/fsync where supported;
7. atomically replace the original only after verification;
8. on failure, remove temp state and leave the original untouched.

### TE-SAFE-13 — bounded event-loop-safe shutdown

Never destroy/terminate a running disk-changing `QThread`. Cancellation occurs only at declared safe boundaries. Application close is deferred/refused while an operation is finishing safely rather than blocking the UI indefinitely or killing a thread mid-write.

## Rename behavior

The rename planner validates the whole batch before moves. Cycles/case-only operations use deterministic temporary hops only after the graph is safe. A metadata-success/rename-failure result updates the known on-disk metadata state while keeping the filename proposal pending.

## Recovery and Undo Applied Batch

The durable journal covers interrupted Apply. A completed Apply can additionally be reversed through the separate **Undo Applied Batch** flow using verified backup/restore preview. The restore path refuses unsafe overwrite of files that changed externally after the recorded operation.

See [`tag-editor-undo-rollback-guarantees.md`](tag-editor-undo-rollback-guarantees.md).

## Preservation and supported formats

Format-specific capabilities may differ, but an unsupported/read-only format must be refused or kept read-only rather than partially mutated. WAV/container-specific behavior must preserve unrelated structures according to the implemented format policy.

## Tests required for changes to this surface

Changes to Apply/backup/journal/rename/restore need focused fault-injection coverage, including as applicable:

- backup target/write/read-back failure → zero media modified;
- write/verify failure → original byte-safe according to the invariant;
- rename collision/lock/case/cycle/root escape/platform-invalid names;
- proposal preservation on partial failure;
- crash/interruption after durable transitions and startup recovery;
- stale-operation result rejection;
- close/cancel during Apply without destroying a running thread;
- metadata preservation fixtures for supported formats.

Use disposable media fixtures only. Run the focused Tag Editor suite and the supported isolated full gate before merging safety changes.

## Change control

These invariants may change only through an explicit architecture/product decision with tests, migration/recovery analysis where relevant and updates to this document, the user guides and `docs/architecture/tag-editor-undo-rollback-guarantees.md` in the same PR.
