---
applyTo: "core/metadata_*.py,core/undo_applied_batch.py,core/restore_preview.py,core/change_drafts.py,core/tag_actions.py,ui/panels/metadata_editor/**/*.py,ui/workers/metadata_worker.py,ui/controllers/metadata_controller.py"
---

Before touching the Tag Editor, read `AGENTS.md`, `docs/architecture/tag-editor-safety.md`, `docs/architecture/tag-editor-undo-rollback-guarantees.md`, `docs/architecture/tag-editor-persistence-migrations.md`, and `docs/design/tag-editor/current-design.md`.

Apply/restore/rename/delete paths are data-safety boundaries. Preserve backup-before-write, verified temp-copy replacement, durable journaling, rename preflight, proposal preservation on failure, external-change safeguards and bounded worker shutdown. Any persisted-state change needs migration documentation and tests.
