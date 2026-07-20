"""Atomic, bounded persistence for unapplied ChangeSet proposals.

Location (finding F-13)
-----------------------
Drafts live under ``utils.paths.get_app_data_dir()`` — the module that declares
itself the single source of truth for the app-data location, and which every
other persistent store already delegates to. The draft store used to hardcode
``Path.home() / ".bananaflow"`` instead, which is only *one* of that function's
branches: on Windows it ignored ``%APPDATA%`` entirely, so a normal install
split user data across two directories and the packaged smoke could not isolate
itself from a developer's real draft.

Moving a persisted artefact is not just an edit — an existing user has a pending
draft at the old path, and orphaning it would silently lose their unapplied
work. ``migrate_legacy_draft`` therefore adopts it losslessly. See
``docs/architecture/tag-editor-persistence-migrations.md`` for the recorded migration entry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
import hashlib
import json
import logging
import os
import tempfile

from core.change_sets import ChangeOperation, ChangeOrigin, ChangeRecord, ChangeSetSnapshot
from core.metadata_models import ArtworkValue, LyricsValue
from utils.paths import get_app_data_dir


logger = logging.getLogger(__name__)

DRAFT_SCHEMA_VERSION = 1

#: Relative layout, preserved exactly across the move so only the root changes.
DRAFT_DIR_NAME = "tag_drafts"
DRAFT_FILE_NAME = "tag_editor_pending.json"


class DraftError(ValueError):
    pass


def _encode(value: object) -> object:
    if isinstance(value, ArtworkValue):
        return {"$type": "artwork", "value": value.to_dict()}
    if isinstance(value, LyricsValue):
        return {"$type": "lyrics", "value": value.to_dict()}
    if isinstance(value, tuple):
        return {"$type": "tuple", "value": [_encode(item) for item in value]}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    raise DraftError(f"unsupported draft value: {type(value).__name__}")


def _decode(value: object) -> object:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("$type")
    if kind == "artwork":
        return ArtworkValue.from_dict(value.get("value"), verify_integrity=True, require_integrity=True)
    if kind == "lyrics":
        return LyricsValue.from_dict(value.get("value"))
    if kind == "tuple":
        raw = value.get("value")
        if not isinstance(raw, list):
            raise DraftError("invalid tuple")
        return tuple(_decode(item) for item in raw)
    return {key: _decode(item) for key, item in value.items()}


class DraftStore:
    """A single safe draft file inside the configured application draft root."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, snapshot: ChangeSetSnapshot, *, root: Path | None,
             session_id: str, app_version: str = "",
             targets: dict[int, dict[str, object]] | None = None) -> None:
        records = []
        for record in snapshot.records:
            records.append({
                "item_id": record.item_id, "field": record.field,
                "original_value": _encode(record.original_value),
                "previous_value": _encode(record.previous_value),
                "proposed_value": _encode(record.proposed_value),
                "operation": record.operation.value, "origin": record.origin.value,
                "revision": record.revision, "excluded": record.excluded_from_apply,
                "capability": record.capability, "diagnostic": record.diagnostic,
                "source_provider": record.source_provider,
                "source_attribution": record.source_attribution,
                "source_url": record.source_url,
            })
        payload = {
            "schema": DRAFT_SCHEMA_VERSION, "created": datetime.now().isoformat(timespec="seconds"),
            "root": str(root) if root else None, "session_id": session_id,
            "app_version": app_version, "generation": snapshot.generation,
            "revision": snapshot.revision, "excluded_ids": sorted(snapshot.excluded_ids),
            "targets": {str(key): _encode(value) for key, value in (targets or {}).items()},
            "records": records,
        }
        self._write_atomic(payload)
        # A successful return is an integrity contract, not merely evidence that
        # os.replace() did not raise.  Re-read the published file and ensure the
        # exact session/revision survived before the caller discards a workspace.
        verified, restored = self.load()
        if (verified.get("session_id") != session_id
                or restored.revision != snapshot.revision
                or restored.records != snapshot.records):
            raise DraftError("published draft failed integrity verification")

    def load(self) -> tuple[dict, ChangeSetSnapshot]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DraftError(str(exc)) from exc
        if not isinstance(raw, dict) or raw.get("schema") != DRAFT_SCHEMA_VERSION:
            raise DraftError("unsupported draft schema")
        if not isinstance(raw.get("records"), list):
            raise DraftError("invalid draft records")
        records: list[ChangeRecord] = []
        for item in raw["records"]:
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), int) or not isinstance(item.get("field"), str):
                raise DraftError("invalid draft record")
            try:
                records.append(ChangeRecord(
                    item_id=item["item_id"], field=item["field"],
                    original_value=_decode(item.get("original_value")),
                    previous_value=_decode(item.get("previous_value")),
                    proposed_value=_decode(item.get("proposed_value")),
                    operation=ChangeOperation(item["operation"]), origin=ChangeOrigin(item["origin"]),
                    revision=int(item["revision"]), excluded_from_apply=bool(item.get("excluded", False)),
                    capability=str(item.get("capability", "")), diagnostic=str(item.get("diagnostic", "")),
                    source_provider=str(item.get("source_provider", "")),
                    source_attribution=str(item.get("source_attribution", "")),
                    source_url=str(item.get("source_url", "")),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise DraftError("invalid draft record") from exc
        excluded = raw.get("excluded_ids", [])
        if not isinstance(excluded, list) or not all(isinstance(value, int) for value in excluded):
            raise DraftError("invalid excluded ids")
        targets = raw.get("targets", {})
        if not isinstance(targets, dict):
            raise DraftError("invalid draft targets")
        decoded_targets: dict[str, object] = {}
        for key, value in targets.items():
            if not isinstance(key, str) or not key.isdigit() or not isinstance(value, dict):
                raise DraftError("invalid draft target")
            decoded_targets[key] = _decode(value)
        raw["targets"] = decoded_targets
        return raw, ChangeSetSnapshot(int(raw.get("generation", 0)), int(raw.get("revision", 0)), tuple(records), frozenset(excluded))

    def discard(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise DraftError(str(exc)) from exc

    def _write_atomic(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".bananaflow_draft_", dir=str(self.path.parent))
        tmp = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    pass
            json.loads(tmp.read_text(encoding="utf-8"))
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


# ──────────────────────────────────────────────────────────────────────────────
# Canonical location and legacy migration (F-13)
# ──────────────────────────────────────────────────────────────────────────────


def get_draft_dir() -> Path:
    """Return the canonical draft directory under the app-data root."""
    return get_app_data_dir() / DRAFT_DIR_NAME


def get_canonical_draft_path() -> Path:
    """Return the canonical pending-draft path. The only location ever written."""
    return get_draft_dir() / DRAFT_FILE_NAME


def get_legacy_draft_path() -> Path:
    """Return the pre-0.2.0 draft path, defined explicitly rather than derived.

    This is the location the draft store hardcoded before F-13 was fixed. It is
    only ever *read* (and then retired), never written.
    """
    return Path.home() / ".bananaflow" / DRAFT_DIR_NAME / DRAFT_FILE_NAME


class DraftMigration(str, Enum):
    """The outcome of one idempotent legacy-adoption attempt."""

    #: Nothing to adopt, or the two locations are the same directory.
    NOT_NEEDED = "not_needed"
    #: The legacy draft was copied to the canonical path and verified.
    MIGRATED = "migrated"
    #: Both existed and were byte-identical; the legacy duplicate was retired.
    DUPLICATE_RETIRED = "duplicate_retired"
    #: Both existed and differed; canonical stays active, legacy preserved.
    CONFLICT_PRESERVED = "conflict_preserved"
    #: The legacy file is not a draft this build understands; left untouched.
    LEGACY_INVALID = "legacy_invalid"
    #: Migration could not complete; every original file is intact.
    FAILED = "failed"


@dataclass(frozen=True)
class DraftMigrationResult:
    outcome: DraftMigration
    canonical: Path
    legacy: Path
    #: Where a retired or conflicting legacy copy was preserved, when applicable.
    preserved_copy: Path | None = None
    #: Redacted, user-safe reason. Never a raw path or exception text.
    detail: str = ""

    @property
    def needs_user_attention(self) -> bool:
        """True when a human has to decide something we must not decide for them."""
        return self.outcome in {DraftMigration.CONFLICT_PRESERVED, DraftMigration.FAILED}


def _redact(path: Path) -> str:
    """Render a path for a log line without exposing the user's directory tree."""
    return f"<app-data>/{DRAFT_DIR_NAME}/{path.name}"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _preserve(source: Path, kind: str) -> Path:
    """Rename a legacy draft aside so it survives but is no longer active.

    A rename, not a delete: the draft represents unapplied user work, and the
    whole point of the migration is that nothing is ever discarded. The active
    legacy path disappears, which is what makes re-running idempotent.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = source.with_name(f"{source.stem}.{kind}-{stamp}{source.suffix}")
    counter = 1
    while target.exists():
        target = source.with_name(f"{source.stem}.{kind}-{stamp}-{counter}{source.suffix}")
        counter += 1
    os.replace(source, target)
    return target


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so a rename survives power loss on POSIX."""
    if os.name == "nt":
        return  # Windows has no directory handle to fsync.
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _copy_atomic_verified(source_bytes: bytes, target: Path) -> None:
    """Publish bytes to ``target`` atomically, then prove they arrived intact.

    Raises ``DraftError`` without leaving a partial file behind. The caller only
    retires the legacy copy after this returns, so an interruption at any point
    leaves the legacy draft as the surviving source of truth.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bananaflow_draft_mig_", dir=str(target.parent))
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(source_bytes)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
        _fsync_dir(target.parent)
        # Read back from the published path, not from memory: the contract is
        # that the file on disk is the draft, not that write() returned.
        written = target.read_bytes()
        if _digest(written) != _digest(source_bytes):
            raise DraftError("migrated draft failed readback verification")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def migrate_legacy_draft(canonical: Path, legacy: Path) -> DraftMigrationResult:
    """Adopt a pre-0.2.0 draft into the canonical location, losslessly.

    Idempotent: safe to call on every startup. The conflict policy is
    deliberately deterministic and never destructive —

    * legacy only            -> copy, verify, then retire the legacy copy;
    * canonical only         -> nothing to do;
    * both, byte-identical   -> canonical wins, the duplicate is retired;
    * both, different        -> **canonical stays active, legacy is preserved**
      under a timestamped name. The two are never merged and neither is ever
      discarded: a draft is unapplied user work, and guessing which edits they
      meant to keep is not a decision this code may make. Canonical wins rather
      than "newest wins" so the outcome cannot depend on a clock that may be
      wrong; the preserved copy keeps the alternative recoverable.

    On any failure every original file is left exactly as it was.
    """
    if canonical == legacy or canonical.parent == legacy.parent:
        return DraftMigrationResult(DraftMigration.NOT_NEEDED, canonical, legacy,
                                    detail="canonical and legacy resolve to the same directory")
    if not legacy.exists():
        return DraftMigrationResult(DraftMigration.NOT_NEEDED, canonical, legacy)

    try:
        legacy_bytes = legacy.read_bytes()
    except OSError as exc:
        logger.warning("[drafts] Legacy draft is unreadable (%s); leaving it untouched.",
                       type(exc).__name__)
        return DraftMigrationResult(DraftMigration.FAILED, canonical, legacy,
                                    detail="the previous draft could not be read")

    # Only adopt something this build can actually restore. A corrupt legacy
    # file is left exactly where it is rather than propagated to the new home.
    try:
        DraftStore(legacy).load()
    except DraftError as exc:
        logger.warning("[drafts] Legacy draft at %s is not a supported draft (%s); "
                       "leaving it in place.", _redact(legacy), exc)
        return DraftMigrationResult(DraftMigration.LEGACY_INVALID, canonical, legacy,
                                    detail="the previous draft is unreadable or from an "
                                           "unsupported version")

    try:
        if canonical.exists():
            if _digest(canonical.read_bytes()) == _digest(legacy_bytes):
                preserved = _preserve(legacy, "migrated")
                logger.info("[drafts] Retired an identical legacy draft duplicate.")
                return DraftMigrationResult(DraftMigration.DUPLICATE_RETIRED, canonical,
                                            legacy, preserved)
            preserved = _preserve(legacy, "conflict")
            logger.warning("[drafts] A legacy draft differs from the current one; the "
                           "current draft stays active and the older copy was preserved "
                           "as %s.", _redact(preserved))
            return DraftMigrationResult(
                DraftMigration.CONFLICT_PRESERVED, canonical, legacy, preserved,
                detail="a second, different draft was found from an earlier version")

        _copy_atomic_verified(legacy_bytes, canonical)
        preserved = _preserve(legacy, "migrated")
        logger.info("[drafts] Migrated the pending draft to the canonical app-data "
                    "location; a backup of the original was kept.")
        return DraftMigrationResult(DraftMigration.MIGRATED, canonical, legacy, preserved)
    except (DraftError, OSError) as exc:
        logger.warning("[drafts] Draft migration failed (%s); the previous draft is "
                       "intact and still readable.", type(exc).__name__)
        return DraftMigrationResult(DraftMigration.FAILED, canonical, legacy,
                                    detail="the previous draft could not be moved")


def resolve_draft_store() -> tuple[DraftStore, DraftMigrationResult]:
    """Return the store for the canonical draft, migrating a legacy one first.

    Reads only when there is nothing to adopt, so constructing a controller on a
    clean machine touches no disk state.

    The internal packaged smoke is deliberately excluded from the migration. It
    runs against a throwaway ``APPDATA`` but inherits the launching user's real
    home directory, so adopting a legacy draft would move that user's genuine
    unapplied work into a scratch directory that is deleted moments later. The
    smoke gets the canonical path and nothing else; only a real startup adopts.
    """
    from core.runtime_mode import is_internal_smoke

    canonical = get_canonical_draft_path()
    legacy = get_legacy_draft_path()
    if is_internal_smoke():
        return DraftStore(canonical), DraftMigrationResult(
            DraftMigration.NOT_NEEDED, canonical, legacy,
            detail="internal smoke: legacy adoption is not performed")
    return DraftStore(canonical), migrate_legacy_draft(canonical, legacy)
