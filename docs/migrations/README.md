# Persistence and migration policy

Status: **Current / normative**

Any persisted schema, file location or meaning that can exist on an already-installed user's machine is a compatibility surface.

## Persisted surfaces include

- `config.json` and its schema version/migrations;
- SQLite/history schema and indexes;
- update-state files;
- queue/cache state where retained across launches;
- Tag Editor drafts, backups, journals, saved workflows/presets and recovery state;
- protected cookie/sign-in store and dedicated browser-profile location;
- any future persisted file introduced under app data.

## Rules

1. Never assume existing state can be discarded because a development checkout is clean.
2. Forward migration must be deterministic, idempotent where practical and covered by tests.
3. A failed migration must preserve the last recoverable user data; do not convert a compatibility problem into data loss.
4. Destructive cleanup requires an explicit product decision and user-safe behavior.
5. Path migrations must consider cross-platform locations and internal-smoke/test isolation.
6. Persisted behavior changes require a documentation entry identifying old state, new state, trigger, conflict policy and failure/interruption behavior.

`config.json` migrations are implemented by `config_migrate.py`. Tag Editor non-config migrations currently have detailed records in [`../architecture/tag-editor-persistence-migrations.md`](../architecture/tag-editor-persistence-migrations.md). Future cross-feature migrations should be indexed from this directory so they are discoverable.

## 2026-08 filename-numbering request split

- **Old state:** persisted paused download requests stored `forced_index`, which controlled both the embedded track-number tag and the physical filename prefix.
- **New state:** requests also store `filename_index`; `forced_index` now describes only authoritative embedded track metadata.
- **Trigger:** deserializing a paused request that predates the new field.
- **Compatibility behavior:** when `filename_index` is absent, the loader copies the existing `forced_index` into it so the resumed job keeps its previously selected filename. An explicitly stored `filename_index: null` remains unnumbered.
- **Conflict/failure behavior:** the migration is additive and in-memory, does not rewrite the saved record, and retains the existing tolerant defaults for truncated or version-skewed requests.

The same additive release stores `filename_include_artist` in paused requests and `collection_title`/`disc_total` in general queue state. Missing values keep the legacy filename body and conservative no-invented-folder behavior. New queue items use `collection_title` to distinguish the source collection from the track's album tag and `disc_total` to retain known multi-disc layout when only part of a release is selected; no existing record is rewritten.
