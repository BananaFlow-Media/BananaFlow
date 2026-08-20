# Tag Editor — Undo and Rollback Guarantees

Status: **Current / normative contributor reference**

The binding disk-safety requirements are in [`tag-editor-safety.md`](tag-editor-safety.md). This page explains the two user-visible undo/recovery concepts and must stay consistent with the implementation.

## 1. Proposal Undo / Redo — before Apply

Editing a tag, running an action or accepting a template creates a proposed change. Ctrl+Z/Ctrl+Y operate on the in-memory proposal history. No media file has been changed yet, so this is ordinary workspace undo rather than filesystem recovery.

## 2. Undo Applied Batch — after Apply

Undo Applied Batch is a separate disk-level restore operation. It uses the completed Apply operation's verified backup/manifest and shows a restore preview before disk changes. If a target file changed externally since the recorded Apply, restore refuses unsafe overwrite rather than guessing that the old backup should win.

## What Apply guarantees first

Before either undo path matters, Apply provides the invariants in [`tag-editor-safety.md`](tag-editor-safety.md):

- backup succeeds before any media mutation;
- a durable operation plan/journal exists before the first write;
- each media write is performed on a same-filesystem temporary copy and read-back verified before atomic replacement;
- only explicit proposed metadata fields are changed;
- the batch rename graph is preflighted;
- rename failure remains partial with intent preserved;
- an interrupted Apply can be detected/reviewed through the persisted journal.

## Recovery after interruption

An incomplete journal can trigger startup recovery/review. Recovery uses the last durable operation state to determine safe next actions. It does not silently perform destructive guesses.

## Boundaries

Undo Applied Batch restores what the verified Apply backup captured and the documented path mapping. It is not a full filesystem snapshot and cannot promise to roll back unrelated changes another program made later.

Format-specific read/write capability also matters: a format that BananaFlow treats as read-only/refused has no metadata write to undo.
