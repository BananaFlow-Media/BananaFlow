"""Portable, preview-first transfer for existing Phase 9 custom presets."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import unicodedata
import uuid
from typing import Iterable, Mapping

from core.metadata_io import (
    CancellationToken, IOErrorInfo, IOErrorKind, MetadataIOError,
    SourceFileIdentity, atomic_write_bytes,
)
from core.tag_action_presets import (
    PresetStep, PresetStore, PresetStoreIdentity, TagActionPreset,
    preset_collection_hash,
)
from core.tag_actions import TagActionRegistry


TRANSFER_SCHEMA = "bananaflow.presets.transfer.v1"
PRODUCT_ID = "bananaflow"
MAX_PACKAGE_BYTES = 5 * 1024 * 1024


class PresetImportState(str, Enum):
    VALID = "valid"
    UNKNOWN_ACTION = "unknown_action"
    INVALID_PARAMETERS = "invalid_parameters"
    DUPLICATE_PACKAGE_ID = "duplicate_package_id"
    EXISTING_CUSTOM_CONFLICT = "existing_custom_conflict"
    BUILTIN_CONFLICT = "builtin_conflict"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    SKIPPED = "skipped"


class PresetConflictPolicy(str, Enum):
    SKIP = "skip"
    KEEP_BOTH = "keep_both"
    REPLACE_CUSTOM = "replace_custom"
    RENAME = "rename"


@dataclass(frozen=True)
class PresetConflictDecision:
    package_index: int
    preset_id: str
    policy: PresetConflictPolicy = PresetConflictPolicy.SKIP
    rename_to: str = ""


@dataclass(frozen=True)
class PresetTransferPackage:
    schema: str
    product: str
    exported_at: str
    presets: tuple[TagActionPreset, ...]


@dataclass(frozen=True)
class PresetImportItem:
    index: int
    preset: TagActionPreset | None
    state: PresetImportState
    diagnostic: str = ""


@dataclass(frozen=True)
class PresetImportPreview:
    source: SourceFileIdentity
    package: PresetTransferPackage | None
    items: tuple[PresetImportItem, ...]
    existing_ids: frozenset[str]
    builtin_ids: frozenset[str]
    store_identity: PresetStoreIdentity


@dataclass(frozen=True)
class PresetImportAcceptance:
    accepted: bool
    imported: int = 0
    skipped: int = 0
    error: IOErrorInfo | None = None


def _normalized_preset(preset: TagActionPreset, registry: TagActionRegistry) -> TagActionPreset:
    if preset.builtin:
        raise ValueError("builtin_preset_immutable")
    if not preset.id.strip() or not preset.name.strip() or preset.version < 1:
        raise ValueError("invalid_preset_identity")
    steps = []
    for step in preset.normalized_steps():
        action_id = registry.resolve_id(step.action_id)
        parameters = PresetStore._sanitize_parameters(step.parameters)
        action = registry.get(action_id)
        known = {parameter.id: parameter for parameter in action.parameters}
        if set(parameters) - set(known):
            raise ValueError("unknown_parameter")
        for parameter in action.parameters:
            parameter.validate(parameters.get(parameter.id, parameter.default))
        steps.append(PresetStep(action_id, parameters))
    return TagActionPreset(preset.id.strip(), preset.name.strip(),
                           steps[0].action_id if steps else "", {}, False,
                           int(preset.version), tuple(steps))


def build_transfer_package(presets: Iterable[TagActionPreset],
                           registry: TagActionRegistry) -> PresetTransferPackage:
    normalized = tuple(_normalized_preset(preset, registry)
                       for preset in presets if not preset.builtin)
    return PresetTransferPackage(
        TRANSFER_SCHEMA, PRODUCT_ID,
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        normalized,
    )


def render_transfer_package(package: PresetTransferPackage) -> bytes:
    payload = {
        "schema": package.schema,
        "product": package.product,
        "exported_at": package.exported_at,
        "presets": [asdict(preset) for preset in package.presets],
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def export_transfer_package(package: PresetTransferPackage, destination: Path,
                            *, overwrite: bool = False,
                            cancellation: CancellationToken | None = None):
    data = render_transfer_package(package)

    def validate(path: Path) -> bool:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("schema") == TRANSFER_SCHEMA and raw.get("product") == PRODUCT_ID

    return atomic_write_bytes(destination, data, overwrite=overwrite,
                              validator=validate, cancellation=cancellation)


def _decode_preset(raw: object) -> TagActionPreset:
    if not isinstance(raw, dict):
        raise ValueError("invalid_preset")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("invalid_steps")
    steps = []
    for step in steps_raw:
        if not isinstance(step, dict) or not isinstance(step.get("action_id"), str):
            raise ValueError("invalid_step")
        parameters = step.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("invalid_parameters")
        steps.append(PresetStep(step["action_id"], parameters))
    preset_id, name = raw.get("id"), raw.get("name")
    if not isinstance(preset_id, str) or not isinstance(name, str):
        raise ValueError("invalid_identity")
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("invalid_version")
    return TagActionPreset(preset_id, name, steps[0].action_id, {}, False,
                           version, tuple(steps))


def preview_preset_import(path: Path, *, registry: TagActionRegistry,
                          store: PresetStore | None = None,
                          existing_custom: Iterable[TagActionPreset] = (),
                          builtins: Iterable[TagActionPreset] | None = None) -> PresetImportPreview:
    source = SourceFileIdentity.capture(Path(path), maximum_bytes=MAX_PACKAGE_BYTES)
    try:
        raw = json.loads(source.path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT)) from exc
    if store is not None:
        current_custom, diagnostic = store.load()
        if diagnostic == "preset_store_corrupt":
            raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT))
        if diagnostic == "preset_store_unsupported":
            raise MetadataIOError(IOErrorInfo(IOErrorKind.UNSUPPORTED_SCHEMA))
        try:
            store_identity = store.identity(current_custom)
        except ValueError as exc:
            kind = (IOErrorKind.UNSUPPORTED_SCHEMA if str(exc) == "preset_store_unsupported"
                    else IOErrorKind.STALE_PREVIEW if str(exc) == "preset_store_changed"
                    else IOErrorKind.INVALID_FORMAT)
            raise MetadataIOError(IOErrorInfo(kind)) from exc
    else:
        current_custom = list(existing_custom)
        store_identity = PresetStoreIdentity(
            False, 2, 0, 0, "", preset_collection_hash(current_custom), False)
    existing_ids = frozenset(preset.id for preset in current_custom)
    builtin_ids = frozenset(preset.id for preset in (builtins or PresetStore.builtins()))
    if not isinstance(raw, dict) or raw.get("schema") != TRANSFER_SCHEMA:
        item = PresetImportItem(0, None, PresetImportState.UNSUPPORTED_SCHEMA,
                                "unsupported_schema")
        return PresetImportPreview(source, None, (item,), existing_ids, builtin_ids,
                                   store_identity)
    if raw.get("product") != PRODUCT_ID or not isinstance(raw.get("presets"), list):
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT))
    items: list[PresetImportItem] = []
    decoded: list[TagActionPreset] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw["presets"]):
        try:
            preset = _decode_preset(entry)
        except ValueError as exc:
            items.append(PresetImportItem(index, None, PresetImportState.INVALID_PARAMETERS, str(exc)))
            continue
        if preset.id in seen:
            items.append(PresetImportItem(index, preset, PresetImportState.DUPLICATE_PACKAGE_ID,
                                          "duplicate_package_id"))
            continue
        seen.add(preset.id)
        if preset.id in builtin_ids:
            state, diagnostic, normalized = PresetImportState.BUILTIN_CONFLICT, "builtin_conflict", preset
        else:
            try:
                normalized = _normalized_preset(preset, registry)
            except KeyError:
                state, diagnostic, normalized = PresetImportState.UNKNOWN_ACTION, "unknown_action", preset
            except ValueError as exc:
                state, diagnostic, normalized = PresetImportState.INVALID_PARAMETERS, str(exc), preset
            else:
                state = (PresetImportState.EXISTING_CUSTOM_CONFLICT
                         if normalized.id in existing_ids else PresetImportState.VALID)
                diagnostic = "existing_custom_conflict" if state is PresetImportState.EXISTING_CUSTOM_CONFLICT else ""
        decoded.append(normalized)
        items.append(PresetImportItem(index, normalized, state, diagnostic))
    package = PresetTransferPackage(TRANSFER_SCHEMA, PRODUCT_ID,
                                    str(raw.get("exported_at") or ""), tuple(decoded))
    return PresetImportPreview(source, package, tuple(items), existing_ids, builtin_ids,
                               store_identity)


def _store_identity_matches(expected: PresetStoreIdentity,
                            actual: PresetStoreIdentity) -> bool:
    if expected.persistent:
        return expected == actual
    return expected.content_sha256 == actual.content_sha256


def accept_preset_import(preview: PresetImportPreview, *, store: PresetStore,
                         decisions: Iterable[PresetConflictDecision] | None = None,
                         existing_custom: Iterable[TagActionPreset] = (),
                         policy: PresetConflictPolicy = PresetConflictPolicy.SKIP,
                         policy_by_id: Mapping[str, PresetConflictPolicy] | None = None,
                         rename_by_id: Mapping[str, str] | None = None) -> PresetImportAcceptance:
    if not preview.source.is_current():
        return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.SOURCE_CHANGED))
    current, diagnostic = store.load()
    if diagnostic == "preset_store_corrupt":
        return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.INVALID_FORMAT))
    if diagnostic == "preset_store_unsupported":
        return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.UNSUPPORTED_SCHEMA))
    try:
        current_identity = store.identity(current)
    except ValueError as exc:
        kind = (IOErrorKind.UNSUPPORTED_SCHEMA if str(exc) == "preset_store_unsupported"
                else IOErrorKind.STALE_PREVIEW if str(exc) == "preset_store_changed"
                else IOErrorKind.INVALID_FORMAT)
        return PresetImportAcceptance(False, error=IOErrorInfo(kind))
    if not _store_identity_matches(preview.store_identity, current_identity):
        return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
    imported = skipped = 0
    rename_by_id = dict(rename_by_id or {})
    policy_by_id = dict(policy_by_id or {})
    decision_by_index: dict[int, PresetConflictDecision] = {}
    if decisions is not None:
        for decision in decisions:
            if (not isinstance(decision, PresetConflictDecision)
                    or decision.package_index in decision_by_index):
                return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.INVALID_MAPPING))
            decision_by_index[decision.package_index] = decision
    elif policy_by_id or policy is not PresetConflictPolicy.SKIP:
        # Compatibility adapter: production UI uses immutable package-index
        # decisions, but older callers remain safely keyed to the preview item.
        for item in preview.items:
            if item.preset is None or item.state is not PresetImportState.EXISTING_CUSTOM_CONFLICT:
                continue
            item_policy = policy_by_id.get(item.preset.id, policy)
            try:
                item_policy = (item_policy if isinstance(item_policy, PresetConflictPolicy)
                               else PresetConflictPolicy(str(item_policy)))
            except ValueError:
                return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.INVALID_MAPPING))
            decision_by_index[item.index] = PresetConflictDecision(
                item.index, item.preset.id, item_policy,
                str(rename_by_id.get(item.preset.id, "")),
            )

    item_by_index = {item.index: item for item in preview.items}
    for index, decision in decision_by_index.items():
        item = item_by_index.get(index)
        if item is None or item.preset is None or item.preset.id != decision.preset_id:
            return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
        if item.state is not PresetImportState.EXISTING_CUSTOM_CONFLICT:
            return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.INVALID_MAPPING))

    current_ids = {preset.id for preset in current}
    for item in preview.items:
        preset = item.preset
        if preset is None or item.state in {
            PresetImportState.UNKNOWN_ACTION, PresetImportState.INVALID_PARAMETERS,
            PresetImportState.DUPLICATE_PACKAGE_ID, PresetImportState.BUILTIN_CONFLICT,
            PresetImportState.UNSUPPORTED_SCHEMA, PresetImportState.SKIPPED,
        }:
            skipped += 1
            continue
        conflict_now = preset.id in current_ids
        if conflict_now != (item.state is PresetImportState.EXISTING_CUSTOM_CONFLICT):
            return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
        if conflict_now:
            decision = decision_by_index.get(item.index)
            if decision is None:
                skipped += 1
                continue
            try:
                item_policy = (decision.policy if isinstance(decision.policy, PresetConflictPolicy)
                               else PresetConflictPolicy(str(decision.policy)))
            except ValueError:
                return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.INVALID_MAPPING))
            if item_policy is PresetConflictPolicy.SKIP:
                skipped += 1
                continue
            if item_policy is PresetConflictPolicy.KEEP_BOTH:
                new_id = uuid.uuid4().hex
                while new_id in current_ids or new_id in preview.builtin_ids:
                    new_id = uuid.uuid4().hex
                preset = TagActionPreset(new_id, preset.name, preset.action_id,
                                         preset.parameters, False, preset.version, preset.steps)
            elif item_policy is PresetConflictPolicy.REPLACE_CUSTOM:
                current = [value for value in current if value.id != preset.id]
                current_ids.discard(preset.id)
            elif item_policy is PresetConflictPolicy.RENAME:
                name = unicodedata.normalize("NFC", str(decision.rename_to)).strip()
                if not name:
                    skipped += 1
                    continue
                new_id = uuid.uuid4().hex
                while new_id in current_ids or new_id in preview.builtin_ids:
                    new_id = uuid.uuid4().hex
                preset = TagActionPreset(new_id, name, preset.action_id,
                                         preset.parameters, False, preset.version, preset.steps)
        current.append(preset)
        current_ids.add(preset.id)
        imported += 1
    if imported:
        try:
            store.save(current)
        except PermissionError:
            return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.PERMISSION_DENIED))
        except (OSError, ValueError):
            return PresetImportAcceptance(False, error=IOErrorInfo(IOErrorKind.WRITE_FAILED))
    return PresetImportAcceptance(True, imported, skipped)
