# Persistence migrations

`MASTER_PLAN.md` §"Config migration policy" requires every persisted-schema change to ship a
migration entry plus a test. That policy is written around `config.json`, whose version lives in
`config_migrate.CURRENT_VERSION`. This file is the equivalent record for persisted artefacts that
are **not** `config.json` and therefore do not move that counter.

The rule of thumb: if an existing installation has a file on disk and a release changes where it
lives, what it contains, or how it is read, it belongs here — with the migration, the test that
proves it, and the failure behaviour.

---

## 0.2.0 — Tag Editor draft moves to the canonical app-data directory

| | |
|---|---|
| **Artefact** | Tag Editor pending proposal draft |
| **Finding** | F-13 (tag-editor final audit) |
| **Payload schema** | **unchanged** — `core.change_drafts.DRAFT_SCHEMA_VERSION` stays `1` |
| **`config_migrate.CURRENT_VERSION`** | **not bumped** — `config.json` is untouched |
| **Code** | `core/change_drafts.py` — `migrate_legacy_draft`, `resolve_draft_store` |
| **Tests** | `tests/test_draft_location_migration.py` (24), `tests/test_packaging.py::test_internal_smoke_ignores_a_draft_outside_its_appdata` |

### What changed

Only the **location**. The file's name, its relative layout (`tag_drafts/tag_editor_pending.json`)
and every byte of its content are the same.

| | Path |
|---|---|
| Legacy (≤ 0.1.0) | `~/.bananaflow/tag_drafts/tag_editor_pending.json` |
| Canonical (≥ 0.2.0) | `utils.paths.get_app_data_dir()/tag_drafts/tag_editor_pending.json` |

`utils/paths.py` declares itself the single source of truth for the app-data location and resolves
to `%APPDATA%\.bananaflow` on Windows, falling back to `~/.bananaflow` only when `APPDATA` is unset. The
draft store had hardcoded that *fallback*, so on a normal Windows install user data split across two
directories, and the packaged smoke could not isolate itself from the developer's real drafts even
though it controlled `APPDATA`.

On Linux/macOS, and on Windows with no `APPDATA`, the two paths can resolve to the same directory.
That is detected and treated as a no-op.

### Migration behaviour

Runs on every `MetadataController` construction via `resolve_draft_store()`. Idempotent; performs no
writes and creates no directories when there is nothing to adopt.

| State on disk | Outcome | Result |
|---|---|---|
| Neither exists | `NOT_NEEDED` | nothing touched |
| Canonical only | `NOT_NEEDED` | canonical used normally |
| Legacy only | `MIGRATED` | copied → fsynced → **read back and hash-verified** → legacy retired to `tag_editor_pending.migrated-<ts>.json` |
| Both, byte-identical | `DUPLICATE_RETIRED` | canonical used; duplicate retired to `…migrated-<ts>.json` |
| Both, different | `CONFLICT_PRESERVED` | **canonical stays active; legacy preserved as `…conflict-<ts>.json`** |
| Legacy unreadable / unsupported schema | `LEGACY_INVALID` | legacy left exactly as-is; nothing propagated |
| Copy or verify failed | `FAILED` | legacy intact; any valid canonical intact |
| Same directory | `NOT_NEEDED` | no self-retirement |

### Conflict policy

**Canonical wins as the active draft; the legacy copy is preserved under a timestamped name.**

Neither copy is ever merged or discarded. A draft is unapplied user work, and guessing which of two
sets of edits someone meant to keep is not a decision this code is entitled to make. "Canonical
wins" rather than "newest wins" so the outcome cannot depend on a clock that may be wrong or on
filesystem timestamps that may be preserved by a copy; the preserved file keeps the alternative
recoverable either way.

The user is told, in their own language, through the draft recovery dialog they are already
answering (`meta_draft_legacy_conflict`), including the path of the preserved copy. That is the
actionable recovery route: open it, or ignore it.

### Failure and interruption

* Ordering is: **copy → fsync → read back → verify hash → only then retire the legacy copy.** An
  interruption at any point leaves the legacy draft as the surviving source of truth.
* An interruption after the copy but before the retire leaves both files identical, which the next
  startup resolves as `DUPLICATE_RETIRED`. Verified by
  `test_interrupted_migration_recovers_on_the_next_startup`.
* The copy goes through a temp file plus `os.replace`, so a torn write can never be published.
* Diagnostics are redacted: `_redact()` renders `<app-data>/tag_drafts/<name>`. Ordinary users never
  see raw private paths or exception text.

### Internal smoke exclusion

`resolve_draft_store()` deliberately **skips the migration** when
`core.runtime_mode.is_internal_smoke()` is set. The packaged smoke runs against a throwaway
`APPDATA` but inherits the launching user's real home directory, so adopting a legacy draft there
would move genuine unapplied work into a scratch directory deleted seconds later — the migration
becoming the data loss it exists to prevent. This was caught by
`test_internal_smoke_ignores_a_draft_outside_its_appdata` during Phase 15 closure, not in review.

Covered by `test_internal_smoke_never_adopts_a_real_users_legacy_draft` and, in the other direction,
`test_production_startup_still_adopts_the_legacy_draft` — so the exclusion cannot quietly disable
migration for real users.

### Not performed

The legacy directory itself (`~/.bananaflow/tag_drafts/`) is **not** removed, and the retired backups
are **not** garbage-collected. Both are deliberate: the directory may hold copies a user still
wants, and no automated cleanup of user data ships without a human deciding it should.
