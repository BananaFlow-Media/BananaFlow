# Tag Editor persistence migrations

Status: **Current migration record**

This file records persisted Tag Editor artifacts that are not `config.json`. General migration policy is in [`../migrations/README.md`](../migrations/README.md); `config.json` schema changes are handled by `config_migrate.py`.

A persisted change belongs here when an existing installation can already have a Tag Editor file on disk and a newer release changes its path/schema/meaning.

## Draft-store location migration

### Artifact

Pending Tag Editor proposal draft: `tag_drafts/tag_editor_pending.json`.

### Change

The payload schema stayed the same; only the storage root changed from the legacy home-directory fallback to the canonical BananaFlow app-data directory returned by `utils.paths.get_app_data_dir()`.

On normal Windows installations this fixed a split where config/history/log state lived under `%APPDATA%\.bananaflow` while the draft store used `~/.bananaflow`.

### Migration behavior

Migration is idempotent and performs no write when no legacy artifact exists.

| State | Result |
|---|---|
| Neither file exists | no-op |
| Canonical only | canonical used |
| Legacy only | copy to canonical → fsync/verify → retire legacy only after verified success |
| Both byte-identical | canonical used; duplicate legacy retired |
| Both differ | canonical remains active; legacy preserved under conflict name for manual recovery |
| Legacy unreadable/unsupported | legacy preserved; not propagated |
| Copy/verify failure | legacy preserved; valid canonical state not destroyed |
| Legacy/canonical resolve to same location | no-op; never self-retire |

### Conflict policy

Canonical active state wins; the different legacy copy is preserved rather than merged/discarded. Drafts represent unapplied user work, so the migration must not guess based on timestamps or silently destroy one version.

### Interruption safety

Ordering is copy → flush/fsync → read-back/hash verification → retire legacy. Temporary-copy + atomic replacement ensures a partial destination is not published. An interruption before retirement leaves the legacy source available; the next run can reconcile duplicate state.

### Internal-smoke isolation

Packaged internal smoke runs use throwaway app-data. They must never adopt a real user's legacy draft from the launching user's home directory into that throwaway location. Production startup still performs the migration.

### Tests

Migration behavior is covered by dedicated draft-location migration tests plus packaging/internal-smoke isolation coverage. Changes to this behavior must keep conflict/failure/interruption tests and update this record.
