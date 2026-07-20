# Tag Editor — Undo and Rollback Guarantees (contributor reference)

This is a consolidated, contributor-facing summary of what the Tag
Editor actually guarantees about undo, backup, and crash recovery. The
binding, detailed requirements this summarizes live in
`docs/architecture/tag-editor-safety.md` (acceptance criteria TE-SAFE-01
through TE-SAFE-13) and the real implementation in
`core/metadata_processor.py`, `ui/workers/metadata_worker.py`, and
`core/undo_applied_batch.py`. If this page and the code ever disagree,
the code and the Phase 1 requirements doc are authoritative — file an
issue so this page gets corrected.

## There are two, deliberately separate undo mechanisms

**1. Proposal undo (Ctrl+Z / Redo) — before Apply, in memory only.**
Editing a tag, running an action, or accepting a template only ever
creates a *proposed* change; nothing touches disk until Apply runs.
Ctrl+Z/Ctrl+Y walk this in-memory undo stack. This is ordinary
UI-level undo and carries no special crash-recovery concerns, since
nothing has been written yet.

**2. Undo Applied Batch — after Apply, restores real files from a
verified backup.** This is a *disk-level* operation
(`core/undo_applied_batch.py`), completely separate from the proposal
undo stack above. It only works against a completed, verified Apply
operation's manifest and backup, and it always shows a preview first
(`core.restore_preview.preview_restore`) — it never silently moves
files. If a file changed size or modification time since the Apply that
touched it, restore refuses that file (`file_changed_externally`)
rather than overwriting an externally-modified file.

## What Apply itself guarantees, before either undo path is even relevant

Every Apply goes through one audited path (see `PHASE_1_SAFETY.md` for
the full acceptance criteria):

* **Backup happens before any media write, and must succeed or nothing
  is touched.** The backup itself is written atomically (temp file on
  the same filesystem → flush → fsync → `os.replace` → re-opened and
  parsed back to confirm it's valid) — a crash mid-backup-write leaves
  no partial file and no media modified.
* **A durable journal is written before the first disk change**,
  recording the complete plan (every file's original/intended path, its
  tag delta, and the full validated rename graph) so a crash between
  any two steps is recoverable, not just detectable. Each file moves
  through `PLANNED → BACKED_UP → WRITTEN → VERIFIED → RENAMED →
  COMPLETE` (or a terminal `FAILED`/`PARTIAL`/`SKIPPED`/`CANCELLED`),
  with the journal rewritten atomically at every transition.
* **A tag write is verified by re-reading it back before the original
  file is replaced.** If the readback doesn't match what was intended,
  the original is left untouched — the temp file is discarded, not
  promoted.
* **Only the fields that actually changed are touched.** Untouched tags
  (comments, artwork, lyrics, ReplayGain, custom/multi-value fields) are
  never rewritten as a side effect of editing something else.
* **The whole batch's rename graph is preflighted before any file
  moves**, catching destination collisions, case-only renames (which
  need a temp-hop on case-insensitive filesystems — see
  `core.metadata_processor.plan_renames` and the platform-dependent
  behavior fixed in issue #22), cycles, reserved names, and root
  escapes. A hazard blocks that specific file with its proposal
  preserved; it is never silently dropped or reported as a false
  success.
* **A run interrupted mid-Apply is recoverable on next launch**, not
  left as a half-written library — the journal is what makes this
  possible, and unresolved journals are what trigger the recovery
  prompt on startup.

## What this does NOT cover

* **AAC, AIFF, WMA are read-only**; APE and MPC are refused entirely.
  There is no write path to undo for these formats in the first place.
* **WAV** only writes ID3-in-WAV artwork; existing RIFF INFO/BWF
  metadata is never touched, so there's nothing there for Apply (or its
  undo) to have modified.
* Undo Applied Batch restores what the backup captured (tag state and,
  where a rename happened, the path mapping) — it is not a full
  filesystem snapshot/rollback of unrelated changes made to the same
  files by other software after Apply ran.
