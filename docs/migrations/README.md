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
