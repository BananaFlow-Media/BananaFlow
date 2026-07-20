"""Validated, versioned operation manifests used by Phase 8 backups.

The manifest is deliberately JSON-shaped so it remains readable by the legacy
backup loader.  It records planning/outcome data around the authoritative
pre-operation tag backup; it never turns an unvalidated external path into a
disk operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import hmac
import json


MANIFEST_SCHEMA_VERSION = 4
INTEGRITY_ALGORITHM = "sha256"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManifestFile:
    original_path: str
    intended_path: str
    final_path: str | None
    identity: dict[str, object] | None
    original: dict[str, object]
    planned_fields: tuple[str, ...] = ()
    included: bool = True
    outcome: dict[str, object] | None = None


def build_operation_manifest(
    *, operation_id: str, root: Path | None, app_version: str,
    operation_type: str, records: list[dict[str, Any]], created: str,
) -> dict[str, Any]:
    if not operation_id:
        raise ManifestError("missing operation id")
    if operation_type not in {"apply", "restore", "undo_applied_batch"}:
        raise ManifestError("invalid operation type")
    manifest = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "operation_id": operation_id,
        "created": created,
        "app_version": app_version,
        "root": str(root) if root else None,
        "operation_type": operation_type,
        "status": "prepared",
        "records": records,
    }
    return _with_integrity(manifest)


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    """Stable bytes for the complete schema-4 payload, excluding its seal."""
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _with_integrity(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(payload)
    sealed["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "digest": hashlib.sha256(_canonical_payload(payload)).hexdigest(),
    }
    return sealed


def _verify_integrity(raw: dict[str, Any]) -> None:
    integrity = raw.get("integrity")
    if not isinstance(integrity, dict):
        raise ManifestError("missing schema-4 integrity record")
    algorithm, digest = integrity.get("algorithm"), integrity.get("digest")
    if algorithm != INTEGRITY_ALGORITHM:
        raise ManifestError("unsupported schema-4 integrity algorithm")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ManifestError("malformed schema-4 integrity digest")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ManifestError("malformed schema-4 integrity digest") from exc
    expected = hashlib.sha256(_canonical_payload(raw)).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise ManifestError("schema-4 manifest integrity mismatch")


def _auth_path(path: Path) -> Path:
    # Deliberately not ``*.json``: backup discovery historically treats every
    # matching JSON file as a user-visible backup manifest.
    return path.with_name(path.name + ".auth")


def _published_digest(raw: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload({"payload": raw})).hexdigest()


def _verify_external_binding(path: Path, raw: dict[str, Any]) -> None:
    """Require the operation record published beside this manifest as well.

    The payload seal catches accidental edits; the detached operation binding
    prevents an attacker from treating a recomputed in-manifest checksum as a
    replacement for the operation that originally published this backup.
    """
    try:
        binding = json.loads(_auth_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("missing or unreadable schema-4 operation binding") from exc
    if not isinstance(binding, dict) or binding.get("algorithm") != INTEGRITY_ALGORITHM:
        raise ManifestError("malformed schema-4 operation binding")
    if binding.get("backup_name") != path.name or binding.get("operation_id") != raw.get("operation_id"):
        raise ManifestError("schema-4 operation binding mismatch")
    digest = binding.get("manifest_digest")
    if not isinstance(digest, str) or not hmac.compare_digest(digest, _published_digest(raw)):
        raise ManifestError("schema-4 operation binding mismatch")


def validate_manifest(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError("manifest is not an object")
    schema = raw.get("schema")
    if schema != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema")
    _verify_integrity(raw)
    if not isinstance(raw.get("operation_id"), str) or not raw["operation_id"]:
        raise ManifestError("missing operation id")
    if raw.get("operation_type") not in {"apply", "restore", "undo_applied_batch"}:
        raise ManifestError("invalid operation type")
    records = raw.get("records")
    if not isinstance(records, list):
        raise ManifestError("invalid records")
    for record in records:
        if not isinstance(record, dict):
            raise ManifestError("invalid record")
        if not isinstance(record.get("original_path"), str):
            raise ManifestError("missing original path")
        if not isinstance(record.get("original"), dict):
            raise ManifestError("missing original metadata")
    return raw


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(str(exc)) from exc
    manifest = validate_manifest(raw)
    _verify_external_binding(path, manifest)
    return manifest


def finalize_manifest(path: Path, outcomes: list[object], *, status: str) -> dict[str, Any]:
    """Persist verified Apply outcomes without rewriting historical schemas."""
    manifest = read_manifest(path)
    by_original = {str(getattr(outcome, "original_path", "")): outcome for outcome in outcomes}
    for record in manifest["records"]:
        outcome = by_original.get(record["original_path"])
        if outcome is None:
            continue
        final_path = getattr(outcome, "final_path", None)
        record["final_path"] = str(final_path) if final_path else None
        if final_path:
            try:
                stat = Path(final_path).stat()
                record["expected_post_identity"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            except OSError:
                record["expected_post_identity"] = None
        record["result"] = {
            "status": str(getattr(outcome, "status", "")),
            "stage": str(getattr(outcome, "stage", "")),
            "error_code": str(getattr(outcome, "error_code", "")),
            "fields_written": list(getattr(outcome, "fields_written", []) or []),
            "rename_pending": bool(getattr(outcome, "rename_pending", False)),
        }
    manifest["status"] = status
    write_manifest(path, manifest)
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Atomically publish a sealed manifest and its detached operation binding."""
    sealed = _with_integrity(manifest)
    _write_atomic(path, sealed)
    _write_atomic(_auth_path(path), {
        "algorithm": INTEGRITY_ALGORITHM,
        "operation_id": sealed["operation_id"],
        "backup_name": path.name,
        "manifest_digest": _published_digest(sealed),
    }, validator=lambda _raw: None)
    return sealed


def _write_atomic(path: Path, payload: dict[str, Any], *, validator=validate_manifest) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bananaflow_manifest_", dir=str(path.parent))
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        # Verify the exact replacement candidate before publishing it.
        validator(json.loads(temp.read_text(encoding="utf-8")))
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
