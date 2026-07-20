"""Safe discovery, retention, and deletion policy for tag-operation backups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import json
import shutil

from core.metadata_processor import load_tag_backup


class BackupManagerError(ValueError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created: str = ""
    root: str | None = None
    status: str = ""
    operation_id: str = ""
    operation_type: str = "apply"
    schema: int | None = None
    affected_files: int = 0
    app_version: str = ""
    size_bytes: int = 0
    valid: bool = False
    error: str = ""
    protected: bool = False
    interrupted: bool = False


class BackupManager:
    """Owns only the configured backup directory; never follows arbitrary paths."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _assert_managed(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise BackupManagerError("outside backup root") from exc
        if resolved.parent != self.root:
            raise BackupManagerError("backup must be directly under backup root")
        return resolved

    def journal_references(self) -> set[Path]:
        """Return only in-root backups referenced by retained journals.

        A malformed journal is deliberately left alone, but it cannot cause an
        arbitrary path outside the backup root to be trusted or protected.
        """
        references: set[Path] = set()
        try:
            journals = self.root.glob("bananaflow_tag_*_*.journal.json")
            for journal in journals:
                try:
                    raw = json.loads(journal.read_text(encoding="utf-8"))
                    candidate = raw.get("backup_path") if isinstance(raw, dict) else None
                    if isinstance(candidate, str):
                        references.add(self._assert_managed(Path(candidate)))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        except OSError:
            pass
        return references

    def list_backups(self, *, protected_paths: set[Path] | None = None,
                     journal_paths: set[Path] | None = None) -> list[BackupInfo]:
        protected = {path.resolve() for path in (protected_paths or set())}
        journal_refs = self.journal_references()
        journal_refs.update(path.resolve() for path in (journal_paths or set()))
        try:
            paths = sorted(self.root.glob("bananaflow_tag_backup_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        return [self.inspect(path, protected=path.resolve() in protected,
                             interrupted=path.resolve() in journal_refs) for path in paths]

    def inspect(self, path: Path, *, protected: bool = False, interrupted: bool = False) -> BackupInfo:
        path = self._assert_managed(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # This validates legacy/schema 2/3/schema 4 metadata, including
            # artwork integrity for the newer formats.
            records = load_tag_backup(path)
            if isinstance(raw, dict):
                schema = raw.get("schema") if isinstance(raw.get("schema"), int) else None
                created = str(raw.get("created") or "")
                root = raw.get("root") if isinstance(raw.get("root"), str) else None
                op_id = str(raw.get("operation_id") or "")
                op_type = str(raw.get("operation_type") or "apply")
                version = str(raw.get("app_version") or "")
            else:
                schema, created, root, op_id, op_type, version = 1, "", None, "", "apply", ""
            status = str(raw.get("status") or "") if isinstance(raw, dict) else ""
            return BackupInfo(path, created, root, status, op_id, op_type, schema, len(records), version,
                              path.stat().st_size, True, "", protected, interrupted)
        except Exception as exc:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return BackupInfo(path, size_bytes=size, valid=False, error=str(exc),
                              protected=protected, interrupted=interrupted)

    def delete(self, path: Path, *, protected_paths: set[Path] | None = None,
               journal_paths: set[Path] | None = None) -> None:
        path = self._assert_managed(path)
        protected = {candidate.resolve() for candidate in (protected_paths or set())}
        referenced = self.journal_references()
        referenced.update(candidate.resolve() for candidate in (journal_paths or set()))
        if path in protected or path in referenced:
            raise BackupManagerError("backup is protected by an active operation or journal")
        try:
            path.unlink()
        except OSError as exc:
            raise BackupManagerError(str(exc)) from exc

    def export(self, path: Path, destination: Path) -> Path:
        path = self._assert_managed(path)
        if not destination.parent.exists() or not destination.parent.is_dir():
            raise BackupManagerError("export destination parent is unavailable")
        try:
            shutil.copy2(path, destination)
        except OSError as exc:
            raise BackupManagerError(str(exc)) from exc
        return destination

    def apply_retention(self, *, max_valid: int = 30, max_age_days: int | None = 180,
                        protected_paths: set[Path] | None = None,
                        journal_paths: set[Path] | None = None) -> list[Path]:
        """Delete only old valid unprotected manifests directly under ``root``."""
        infos = self.list_backups(protected_paths=protected_paths, journal_paths=journal_paths)
        cutoff = datetime.now() - timedelta(days=max_age_days) if max_age_days is not None else None
        deleted: list[Path] = []
        valid_seen = 0
        for info in infos:
            if not info.valid:
                continue
            valid_seen += 1
            old = False
            if cutoff and info.created:
                try:
                    old = datetime.fromisoformat(info.created) < cutoff
                except ValueError:
                    old = False
            if valid_seen <= max_valid and not old:
                continue
            if info.protected or info.interrupted:
                continue
            self.delete(info.path, protected_paths=protected_paths, journal_paths=journal_paths)
            deleted.append(info.path)
        return deleted
