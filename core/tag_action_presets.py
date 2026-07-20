"""Versioned, atomic persistence for Phase 9 custom action presets."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid
import re

from core.tag_actions import LEGACY_ACTION_IDS, TagActionRegistry

SCHEMA = 2
SUPPORTED_SCHEMAS = frozenset({1, 2})


@dataclass(frozen=True)
class PresetStep:
    action_id: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TagActionPreset:
    id: str
    name: str
    action_id: str
    parameters: dict[str, object] = field(default_factory=dict)
    builtin: bool = False
    version: int = 1
    steps: tuple[PresetStep, ...] = ()

    def normalized_steps(self) -> tuple[PresetStep, ...]:
        return self.steps or (PresetStep(self.action_id, dict(self.parameters)),)


@dataclass(frozen=True)
class PresetStoreIdentity:
    """Immutable identity of the persistent custom-preset store."""

    exists: bool
    schema: int
    size: int
    modified_time_ns: int
    file_sha256: str
    content_sha256: str
    persistent: bool = True


def preset_collection_hash(presets) -> str:
    ordered = sorted((preset for preset in presets if not preset.builtin),
                     key=lambda preset: (preset.name.casefold(), preset.id))
    payload = json.dumps([asdict(preset) for preset in ordered], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PresetStore:
    def __init__(self, path: Path, registry: TagActionRegistry | None = None) -> None:
        self.path = path
        self.registry = registry

    def load(self) -> tuple[list[TagActionPreset], str | None]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], None
        except (OSError, json.JSONDecodeError):
            return [], "preset_store_corrupt"
        if (not isinstance(raw, dict) or raw.get("schema") not in SUPPORTED_SCHEMAS
                or not isinstance(raw.get("presets"), list)):
            return [], "preset_store_unsupported"
        presets: list[TagActionPreset] = []
        seen: set[str] = set()
        for entry in raw["presets"]:
            if not isinstance(entry, dict):
                continue
            preset_id, name, action_id = entry.get("id"), entry.get("name"), entry.get("action_id")
            if (not all(isinstance(value, str) and value.strip() for value in (preset_id, name))
                    or not isinstance(action_id, str) or preset_id in seen):
                continue
            parameters = entry.get("parameters", {})
            try:
                parameters = self._sanitize_parameters(parameters)
            except ValueError:
                continue
            steps_raw = entry.get("steps", []) if raw.get("schema") == 2 else []
            steps: list[PresetStep] = []
            if isinstance(steps_raw, list):
                for step in steps_raw:
                    if not isinstance(step, dict) or not isinstance(step.get("action_id"), str):
                        continue
                    try:
                        step_parameters = self._sanitize_parameters(step.get("parameters", {}))
                    except ValueError:
                        continue
                    steps.append(PresetStep(step["action_id"], step_parameters))
            if not action_id.strip() and not steps:
                continue
            action_ids = [step.action_id for step in steps] or [action_id]
            if self.registry is not None:
                try:
                    canonical = [self.registry.resolve_id(registered_id) for registered_id in action_ids]
                except KeyError:
                    continue
                if steps:
                    steps = [replace(step, action_id=resolved)
                             for step, resolved in zip(steps, canonical)]
                else:
                    action_id = canonical[0]
            seen.add(preset_id)
            presets.append(TagActionPreset(
                preset_id, name.strip(), action_id, dict(parameters), False,
                max(1, int(entry.get("version", 1))), tuple(steps),
            ))
        presets.sort(key=lambda preset: (preset.name.casefold(), preset.id))
        diagnostic = "preset_store_migrated" if raw.get("schema") != SCHEMA else None
        return presets, diagnostic

    def identity(self, presets: list[TagActionPreset] | None = None) -> PresetStoreIdentity:
        """Capture stat, bytes and canonical content without trusting a UI list."""
        try:
            before = self.path.stat()
            raw = self.path.read_bytes()
        except FileNotFoundError:
            loaded = [] if presets is None else list(presets)
            return PresetStoreIdentity(
                False, SCHEMA, 0, 0, "", preset_collection_hash(loaded))
        except OSError as exc:
            raise ValueError("preset_store_corrupt") from exc
        loaded, diagnostic = self.load()
        if diagnostic in {"preset_store_corrupt", "preset_store_unsupported"}:
            raise ValueError(diagnostic)
        try:
            after = self.path.stat()
        except OSError as exc:
            raise ValueError("preset_store_corrupt") from exc
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("preset_store_changed")
        return PresetStoreIdentity(
            True, SCHEMA if diagnostic is None else 1, before.st_size,
            before.st_mtime_ns, hashlib.sha256(raw).hexdigest(),
            preset_collection_hash(loaded),
        )

    def save(self, presets: list[TagActionPreset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted((preset for preset in presets if not preset.builtin),
                         key=lambda preset: (preset.name.casefold(), preset.id))
        for preset in ordered:
            self._sanitize_parameters(preset.parameters)
            for step in preset.steps:
                self._sanitize_parameters(step.parameters)
            if self.registry is not None:
                action_ids = [step.action_id for step in preset.steps] or [preset.action_id]
                for action_id in action_ids:
                    self.registry.resolve_id(action_id)
        payload = {"schema": SCHEMA, "presets": [asdict(preset) for preset in ordered]}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            temp = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
        try:
            # Preserve the last readable custom store before replacement.
            if self.path.exists():
                try:
                    json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
                else:
                    shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
            temp.replace(self.path)
            # Readback validates the exact on-disk representation before the
            # save is reported successful.
            check = json.loads(self.path.read_text(encoding="utf-8"))
            if check.get("schema") != SCHEMA or not isinstance(check.get("presets"), list):
                raise OSError("preset_store_readback_failed")
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def create(name: str, action_id: str, parameters: dict[str, object]) -> TagActionPreset:
        if not name.strip() or not action_id.strip():
            raise ValueError("preset_name_and_action_required")
        return TagActionPreset(uuid.uuid4().hex, name.strip(), action_id,
                               PresetStore._sanitize_parameters(parameters))

    @staticmethod
    def rename(preset: TagActionPreset, name: str) -> TagActionPreset:
        PresetStore._ensure_custom(preset)
        if not name.strip():
            raise ValueError("preset_name_required")
        return replace(preset, name=name.strip(), version=preset.version + 1)

    @staticmethod
    def update(preset: TagActionPreset, *, action_id: str,
               parameters: dict[str, object], steps: tuple[PresetStep, ...] = ()) -> TagActionPreset:
        PresetStore._ensure_custom(preset)
        return replace(preset, action_id=action_id, parameters=dict(parameters), steps=tuple(steps),
                       version=preset.version + 1)

    @staticmethod
    def duplicate(preset: TagActionPreset, name: str | None = None) -> TagActionPreset:
        return replace(preset, id=uuid.uuid4().hex, name=(name or preset.name).strip(),
                       builtin=False, version=1)

    @staticmethod
    def delete(presets: list[TagActionPreset], preset_id: str) -> list[TagActionPreset]:
        target = next((preset for preset in presets if preset.id == preset_id), None)
        if target is not None:
            PresetStore._ensure_custom(target)
        return [preset for preset in presets if preset.id != preset_id]

    @staticmethod
    def _ensure_custom(preset: TagActionPreset) -> None:
        if preset.builtin:
            raise ValueError("builtin_preset_immutable")

    @staticmethod
    def _sanitize_parameters(parameters: object) -> dict[str, object]:
        if not isinstance(parameters, dict):
            raise ValueError("preset_parameters_invalid")

        def clean(value: object):
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, str):
                if len(value) > 4096:
                    raise ValueError("preset_value_too_long")
                if re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}|/)", value):
                    raise ValueError("preset_absolute_path_forbidden")
                return value
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                if not all(isinstance(key, str) for key in value):
                    raise ValueError("preset_parameter_key_invalid")
                return {key: clean(item) for key, item in value.items()}
            raise ValueError("preset_parameter_type_invalid")

        if not all(isinstance(key, str) for key in parameters):
            raise ValueError("preset_parameter_key_invalid")
        return {key: clean(value) for key, value in parameters.items()}

    @staticmethod
    def builtins() -> tuple[TagActionPreset, ...]:
        return (
            TagActionPreset("builtin.filename.artist-title.v1", "Artist - Title",
                            "template.tags_to_filename.v1", {"template": "{artist} - {title}", "sanitize": True}, True),
            TagActionPreset("builtin.filename.track-artist-title.v1", "Track - Artist - Title",
                            "template.tags_to_filename.v1", {"template": "{track_num:02} - {artist} - {title}", "sanitize": True}, True),
            TagActionPreset("builtin.tags.artist-title.v1", "Artist - Title to tags",
                            "template.filename_to_tags.v1", {"template": "{artist} - {title}", "overwrite": False}, True),
        )

    @staticmethod
    def migrate_legacy_auto_ops(ops: list[str]) -> TagActionPreset:
        steps: list[PresetStep] = []
        for legacy_id in ops:
            action_id = LEGACY_ACTION_IDS.get(legacy_id)
            if not action_id:
                continue
            parameters = ({"strip_numbering": False} if legacy_id == "title_full"
                          else {"strip_numbering": True} if legacy_id == "title_strip"
                          else {"field": "title"} if legacy_id == "normalize_spaces" else {})
            steps.append(PresetStep(action_id, parameters))
        return TagActionPreset(
            "builtin.legacy-auto-arrange.v1", "Auto Arrange", "", {}, True, 1, tuple(steps),
        )
