# Tag Editor — Phase 1: Safety Hardening (binding requirements)

> **Status:** APPROVED as the **only** first implementation diff. Implementation NOT started.
> This document is the binding, complete requirement set for Phase 1. It supersedes the summary in
> `MASTER_PLAN.md` §F where more specific. Nothing outside Phase 1 belongs in the Phase 1 diff.

## Scope & non-goals
- **In scope:** make Apply / backup / rename **honest, crash-safe, recoverable, and preservation-safe.**
- **Out of scope (must NOT appear in the Phase 1 diff):** visual redesign; the workspace state-model
  change (Selection=edit / Changed=Apply); folder navigation; search/filters; artwork; extended
  fields; online metadata. Those are later phases in `MASTER_PLAN.md`.
- **No change** to the `OriginalTags`/`ProposedTags` field set (that is Phase 5).

## Locked state-model contract (context only — implemented in Phase 2, not here)
> Selection determines editing scope. Pending changed files determine Apply scope. Editing actions
> only create proposed changes; they never write to disk. Apply shows how many files are affected first.

## The four approved clarifications (all binding, all Phase 1 except #1 which is Phase 0)
1. **Complete isolated-process baseline (Phase 0):** each test file run in a fresh process; aggregate
   exact totals; identify individually crashing files. **Result on HEAD `b979147`: 885 passed, 0
   failed, 0 skipped; one file (`tests/test_metadata_editor_toolbar_state.py`) exits 139 (native
   segfault at teardown) though its test passes.** See `IMPLEMENTATION_STATUS.md`.
2. **Durable Apply operation journal** — recoverable path/state after a crash (see R-JOURNAL).
3. **Media-file atomic-write ordering** — temp-copy → verify → atomic replace (verify BEFORE
   replace) (see R-ATOMIC).
4. **Bounded, event-loop-safe worker shutdown** — never block the GUI thread indefinitely; never
   destroy a running QThread (see R-SHUTDOWN).

---

## Acceptance criteria (TE-SAFE-01 … TE-SAFE-13)
| ID | Requirement | Current defect | Acceptance signal |
|---|---|---|---|
| TE-SAFE-01 | Backup must succeed before any media write | Non-fatal (`ui/workers/metadata_worker.py:130-138`) | fault-injection: **0 files modified** when backup fails |
| TE-SAFE-02 | Backup atomic (temp+fsync+replace) + readback-validated | Direct `json.dump` (`core/metadata_processor.py:621-631`) | interrupted write leaves no partial dest; readback parses |
| TE-SAFE-03 | Versioned backup schema; legacy + new loader | 9-field list only | loads legacy list AND new object; round-trips |
| TE-SAFE-04 | Metadata-write vs rename tracked separately | Collapsed into one bool (`:158-178`) | rename-fail ⇒ PARTIAL, never DONE |
| TE-SAFE-05 | Failed/collided rename keeps proposal, never "success" | Cleared + counted success | proposal preserved; collision reported |
| TE-SAFE-06 | Structured per-file `ApplyOutcome` + batch result | `(str,bool)` + 3 ints | outcome carries stage/status/code/paths/retryable |
| TE-SAFE-07 | Write only proposed fields; preserve COMM/art/lyrics/RG/custom/multi-value | Rewrites effective known fields; COMM collapse (`:527-531`) | title-only edit preserves all others (mp3/flac/m4a) |
| TE-SAFE-08 | Verify changed fields **before** replacing the original | None | verify-fail leaves original untouched |
| TE-SAFE-09 | Operation coordination + reject late signals | Generation-less; path-keyed | stale worker cannot mutate a newer workspace |
| TE-SAFE-10 | Batch rename graph preflight (collision/case/cycle/reserved/root-escape/lock) | None | each hazard blocked or safely sequenced |
| TE-SAFE-11 | Durable Apply operation journal (PLANNED→…→COMPLETE); plan persisted pre-write; startup recovery | None | crash between any two transitions is recoverable |
| TE-SAFE-12 | Per-file media write: temp-copy → verify → atomic replace (verify BEFORE replace) | Writes original in place | verify-fail leaves original untouched; temp deleted |
| TE-SAFE-13 | Bounded, event-loop-safe worker shutdown; never destroy a running QThread | Unbounded/absent | app-close during Apply defers/refuses; no thread killed |

---

## R-BACKUP — Backup (TE-SAFE-01/02/03)
- **Preflight** `validate_backup_target(dir)`: dir exists/creatable, writable, probe free space;
  failure ⇒ **abort the batch before any write.**
- **Versioned schema (object form, schema 2):**
  `{"schema":2, "operation_id":<uuid>, "app_version":..., "created":<iso>, "root":..., "records":[
  {"original_path","intended_path","final_path"(post-op),"identity":{size,mtime_ns},
  "original":{…tags…},"result":<per-file>}]}`. **Loader accepts both** the legacy top-level list
  (schema 1) and the new object.
- **Collision-proof name:** `bananaflow_tag_backup_<yyyymmdd_hhmmss>_<operation_id[:8]>.json` (a
  second-precision timestamp alone is insufficient).
- **Atomic write of the backup JSON:** temp file **in the destination filesystem** → `flush()` →
  `os.fsync()` (where supported) → `os.replace()` → **re-open and parse to validate** → clean up temp
  on any failure. Any failure raises → batch aborts, **no media touched.**
- **Recovery strategy (justified):** for mp3/flac/m4a, Phase 1 uses **tag-snapshot backup +
  write-to-temp-and-atomic-replace** (R-ATOMIC). Full-file copies are rejected for Phase 1 (space on
  large libraries); complete format-preserving snapshots arrive with extended-field support (Phase 5).
  The manifest records `original_path`/`intended_path`/`final_path` so a **successful rename is
  restorable** (restore maps final→original).

## R-JOURNAL — Durable Apply operation journal (TE-SAFE-11) — NOT deferred to Phase 8
- The backup captures pre-write tag state, but `final_path` and outcomes are known only **after**
  disk changes; a crash between backup and completion must remain recoverable, so a **minimum durable
  journal is part of Phase 1.**
- **Journal file** `bananaflow_tag_apply_<operation_id>.journal.json` beside the backup, (re)written
  **atomically** (temp → flush → fsync → `os.replace`) on **every** state transition.
- **Persist the COMPLETE plan before the first disk modification:** for every file its
  `original_path`, `intended_path` (rename target or unchanged), a reference to its tag delta, and the
  full validated **rename graph/sequence** (R-RENAME).
- **Per-file state machine, atomically persisted at each transition:**
  `PLANNED → BACKED_UP → WRITTEN → VERIFIED → RENAMED → COMPLETE`
  (terminal `FAILED`/`PARTIAL`/`SKIPPED`/`CANCELLED`); batch-level
  `PLANNING → BACKING_UP → APPLYING → DONE`.
- **Startup recovery:** on launch, if an incomplete journal exists, offer **review-first** recovery —
  per file, use its last durable state to finish a safe step, roll back an unverified temp write, or
  restore from backup; reconcile `final_path` so a completed rename is restorable. **Never take an
  automatic destructive action.**
- **Cleanup:** the journal is marked COMPLETE (retained per retention policy) or removed only after
  the batch reaches DONE and is verified.

## R-ATOMIC — Per-file media atomic-write ordering (TE-SAFE-08/12) — verify BEFORE replace
For **each** media file, in this exact order:
1. Create the temporary copy **on the same filesystem** as the original (so the replace is atomic).
2. Preserve required permissions/attributes on the temp copy (mode; timestamps only if
   product-required; clear read-only as needed).
3. Write **only the proposed metadata delta** to the temporary copy — **never the original.**
4. Close all file handles on the temp copy.
5. **Read back and verify** the temp copy (each explicitly changed field equals its intended value;
   the container still parses).
6. `flush`/`fsync` the temp copy where supported.
7. **Only after successful verification**, atomically replace the original (`os.replace(temp, original)`);
   persist journal `WRITTEN → VERIFIED`.
8. On any write/verify failure: **delete the temp copy, leave the original untouched**, mark the file
   `FAILED` (stage WRITE|VERIFY), retryable.
- **Binding correction:** never replace the original first and then rely on the limited tag snapshot
  to recover from a verification failure — the original is only ever touched by a validated temp copy.
  The tag snapshot is a **secondary** net, not the primary recovery path.

## R-PRESERVE — Write preservation (TE-SAFE-07, fixes MP3 COMM collapse)
- **Write only explicitly proposed fields** (switch `write_tags` from "rewrite all effective known
  fields" to "apply the `ProposedTags` delta"), so untouched **COMM frames, artwork, lyrics,
  ReplayGain, custom TXXX/freeform, MusicBrainz IDs, and multi-value fields are never disturbed.**
- When a comment IS edited, **preserve other `COMM` frames** (match by `lang`/`desc`) instead of
  `delall("COMM")` (fixes `core/metadata_processor.py:527-531`).
- **Preservation tests:** title-only edit on mp3/flac/m4a fixtures that also carry a comment, artwork,
  lyrics, a custom tag, and (flac) multi-value artist ⇒ all survive except title.

## R-RENAME — Rename planning (TE-SAFE-04/05/10)
- **Preflight the whole batch rename graph before any disk change.** Detect/handle: destination
  collision; case-insensitive collision; **case-only** rename on Windows (temp hop); A→B/B→A and
  longer **cycles** (deterministic temp-name sequencing); reserved names (`CON`,`PRN`,`AUX`,`NUL`,
  `COM1-9`,`LPT1-9`); trailing dot/space; invalid chars `<>:"|?*`; paths escaping the root; locked
  source/destination.
- **Phase-1 policy:** cycles are supported via deterministic temp names **only if** the whole graph
  validates; any unresolved hazard ⇒ that rename is **blocked**, its proposal **preserved**, and
  reported — never silently dropped or counted as success.
- **On metadata-success + rename-failure:** update in-memory `original` to the verified on-disk
  values; clear only successfully-written **tag** proposals; **retain the filename proposal**; return
  `PARTIAL`; the journal stays at `VERIFIED` (not `RENAMED`) for that file.

## R-SHUTDOWN — Operation coordination + bounded shutdown (TE-SAFE-09/13)
- Controller gains `op_generation:int`, bumped on every new scan/root/workspace; each apply/restore
  worker captures it; panel finish-slots **reject** any signal whose generation ≠ current.
- **Bounded, event-loop-safe shutdown — never block the GUI thread indefinitely, never destroy a
  running QThread:**
  - Prevent root/workspace replacement while a disk-changing op (Apply/Restore) is active; no Restore
    during Apply and vice-versa.
  - Request cancellation only at **declared safe boundaries** (between backup / per-file write / verify
    / rename — never mid-atomic-replace).
  - Wait via **signals / event-loop-safe coordination**, not an unbounded `wait()`/`join()`.
  - Use a **bounded shutdown timeout**; if safe termination hasn't completed, **refuse or defer**
    application close (keep a "finishing safely…" state) rather than killing the thread.
  - Never call `terminate()`/destroy on a running QThread.

## R-RESULTS — Structured results (TE-SAFE-06)
- **Two levels:** `ApplyBatchResult` (backup/preflight/global failure, counts, backup path,
  operation_id) **and** per-file `ApplyOutcome(original_path, final_path, stage∈{BACKUP,WRITE,VERIFY,
  RENAME}, status∈{SUCCESS,FAILED,SKIPPED,CANCELLED,PARTIAL}, error_code, message_key, detail,
  retryable, fields_written)`. **Do not** fake a per-file "backup failure" to represent a batch-level
  abort — surface the batch failure distinctly.
- New signals: `MetadataApplyWorker.file_outcome(object)` + `finished(object)` (batch result);
  migrate `panel.py` + `metadata_controller.py` slots (drop the bare `(path,bool)`/3-int signals once
  no consumer needs them).

---

## Files (Phase 1 only)
`core/metadata_models.py` (result/enums/manifest + journal-state types); `core/metadata_processor.py`
(atomic/validated backup, versioned loader, **temp-copy delta-write + verify-before-replace**, COMM
fix, rename-graph helper, **journal read/write + startup recovery**); `ui/workers/metadata_worker.py`
(abort-on-backup-fail, journal transitions, separated rename accounting, per-file outcome,
op_generation, safe cancellation boundaries); `ui/controllers/metadata_controller.py` (op_generation,
coordination + bounded shutdown, restore↔apply mutual exclusion, journal-recovery entry point);
`ui/panels/metadata_editor/panel.py` (consume outcomes, render backup-blocked + partial-success +
rename-retry + recovery prompt, drop stale); `ui/i18n.py` (new EN+HE keys). New test file
`tests/test_apply_safety.py`.

## Tests (real fixtures via `tests/audio_fixtures.py` + targeted monkeypatch)
- **Backup fault injection:** unwritable dir · disk-full (patch `open`/`json.dump`) · invalid dir ·
  interrupted write · corrupt output ⇒ **zero media files modified.**
- **Rename:** metadata-ok+rename-fail · metadata-fail+no-rename · collision · locked dest · dest
  disappears · case-only · A→B/B→A cycle · reserved/invalid name · root-escape · cancel-after-write ⇒
  **proposal preserved, status PARTIAL/ERROR, never DONE-success.**
- **Preservation:** title-only edit keeps comment/art/lyrics/custom/multi-value (mp3/flac/m4a).
- **Atomic ordering:** induced write/verify failure ⇒ **temp copy deleted, original byte-identical**,
  status FAILED(WRITE|VERIFY), retryable.
- **Journal recovery:** simulate a crash after each transition (BACKED_UP/WRITTEN/VERIFIED/RENAMED) ⇒
  startup recovery restores a consistent, recoverable state; a completed rename maps back to origin.
- **Stale-op:** second scan mid-apply ⇒ stale outcomes dropped.
- **Bounded shutdown:** app-close during Apply ⇒ close deferred/refused, worker cancelled at a safe
  boundary, **no QThread destroyed.**
- **Structured result + i18n:** `ApplyOutcome`/`ApplyBatchResult` fields; new keys in EN+HE.

## Manual verification (run live)
Read-only backup dir ⇒ Apply **blocked**, message shown, **no file changed**; rename collision ⇒
partial-success + rename stays pending + retry works; kill the app mid-apply → relaunch → **recovery
offered**; induced verify-fail ⇒ original byte-identical; new scan mid-apply ⇒ no stale mutation;
edit only a title on a file with embedded art + a comment ⇒ art + comment intact.

## Acceptance (aggregate) · Rollback · Risk
- **Acceptance:** no media write without a validated backup; every media file written via
  temp-copy → verify → atomic replace (verify failure leaves the original untouched); a crash between
  any two journal transitions is recoverable and a completed rename remains restorable; no
  failed/collided rename counted as success or cleared; editing one field never drops unrelated tags;
  structured batch + per-file results on every apply; stale results cannot mutate a newer workspace;
  app close during a disk-changing op never destroys a running thread or corrupts a file; **focused
  Tag Editor suite stays green and `tests/test_apply_safety.py` passes.**
- **Rollback boundary:** self-contained to the apply/backup pipeline + result/journal types + panel
  finish-slots; revertable as one commit; no UI layout / state semantics / adjacent subsystem touched.
- **Dependencies:** none new. **Packaging:** none.
- **Risk: Medium–High** (rewrites the write pipeline that touches real files). Requires **independent
  diff review + focused-suite green + Windows rename smoke** before merge.
