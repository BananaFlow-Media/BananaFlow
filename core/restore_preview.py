"""Read-only restore/Undo-Applied-Batch preview for schema-4 manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.metadata_processor import load_tag_backup, plan_renames
from core.operation_manifest import read_manifest


@dataclass(frozen=True)
class RestorePreviewItem:
    original_path: Path
    expected_current_path: Path
    current_path: Path | None
    needs_path_restore: bool
    status: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RestorePreview:
    manifest_path: Path
    operation_id: str
    items: tuple[RestorePreviewItem, ...]
    found: int
    missing: int
    needs_path_restore: int
    rename_plan: object


class _Move:
    def __init__(self, source: Path, destination: Path) -> None:
        self.path = source
        self.proposed_filename = destination.name


def preview_restore(manifest_path: Path) -> RestorePreview:
    manifest = read_manifest(manifest_path)
    items: list[RestorePreviewItem] = []
    moves: list[_Move] = []
    for record in manifest["records"]:
        original = Path(record["original_path"])
        expected = Path(record.get("final_path") or record.get("intended_path") or record["original_path"])
        current = expected if expected.exists() else (original if original.exists() else None)
        needs_path = current is not None and current != original
        if current is None:
            status = "missing"
        elif current == expected:
            status = "found"
        else:
            status = "found_at_original"
        if needs_path:
            moves.append(_Move(current, original))
        items.append(RestorePreviewItem(original, expected, current, needs_path, status,
                                        tuple(record.get("planned_fields", ()))))
    return RestorePreview(
        manifest_path, manifest["operation_id"], tuple(items),
        sum(item.current_path is not None for item in items),
        sum(item.current_path is None for item in items),
        sum(item.needs_path_restore for item in items), plan_renames(moves),
    )
