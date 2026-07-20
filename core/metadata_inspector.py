"""Pure state and proposal logic for the Tag Editor's unified Inspector.

The widget layer renders these snapshots but never infers that an empty or
mixed control means Clear. Only an explicit proposal method mutates tracks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from core.metadata_backend import FORMAT_CAPABILITIES, FormatCapabilityRegistry
from core.metadata_models import (
    ARTWORK_FIELD,
    ArtworkEntry,
    ArtworkValue,
    AudioTrackItem,
    ChangeAction,
    FieldChange,
    LYRICS_FIELD,
    LyricsValue,
    REPLAYGAIN_FIELDS,
    metadata_values_equal,
)


class ValueState(str, Enum):
    EMPTY = "empty"
    VALUE = "value"
    MIXED = "mixed"


class CapabilityCoverage(str, Enum):
    ALL = "all"
    SOME = "some"
    NONE = "none"


@dataclass(frozen=True)
class InspectorFieldState:
    field_name: str
    value_state: ValueState
    value: object | None
    pending_count: int
    supported_count: int
    total_count: int

    @property
    def capability(self) -> CapabilityCoverage:
        if self.supported_count == 0:
            return CapabilityCoverage.NONE
        if self.supported_count == self.total_count:
            return CapabilityCoverage.ALL
        return CapabilityCoverage.SOME

    @property
    def pending(self) -> bool:
        return self.pending_count > 0


@dataclass(frozen=True)
class InspectorSnapshot:
    fields: dict[str, InspectorFieldState]
    selected_count: int
    editable_count: int
    format_ids: tuple[str, ...]
    file_properties: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class InspectorToken:
    generation: int
    item_ids: tuple[int, ...]


@dataclass(frozen=True)
class ProposalResult:
    affected_count: int
    unsupported_count: int


class MetadataInspectorState:
    """Build snapshots and apply explicit proposals to a selection."""

    def __init__(self, registry: FormatCapabilityRegistry = FORMAT_CAPABILITIES) -> None:
        self.registry = registry

    @staticmethod
    def token(generation: int, tracks: Iterable[AudioTrackItem], item_id: Callable[[AudioTrackItem], int]) -> InspectorToken:
        return InspectorToken(generation, tuple(sorted(item_id(track) for track in tracks)))

    @staticmethod
    def token_is_current(
        token: InspectorToken,
        generation: int,
        tracks: Iterable[AudioTrackItem],
        item_id: Callable[[AudioTrackItem], int],
    ) -> bool:
        return token == MetadataInspectorState.token(generation, tracks, item_id)

    def snapshot(self, tracks: list[AudioTrackItem], field_names: Iterable[str]) -> InspectorSnapshot:
        fields = {name: self.field_state(tracks, name) for name in field_names}
        props = dict(tracks[0].original.file_properties) if len(tracks) == 1 else {}
        return InspectorSnapshot(
            fields=fields,
            selected_count=len(tracks),
            editable_count=sum(track.metadata_editable for track in tracks),
            format_ids=tuple(sorted({track.format_id or "unknown" for track in tracks})),
            file_properties=props,
        )

    def field_state(self, tracks: list[AudioTrackItem], field_name: str) -> InspectorFieldState:
        values = [self.effective_value(track, field_name) for track in tracks]
        if not values or all(self._empty(field_name, value) for value in values):
            state, common = ValueState.EMPTY, None
        elif all(metadata_values_equal(field_name, values[0], value) for value in values[1:]):
            state, common = ValueState.VALUE, values[0]
        else:
            state, common = ValueState.MIXED, None
        pending = sum(field_name in track.proposed.changed_fields(track.original) for track in tracks)
        supported = sum(self._field_supported(track, field_name) for track in tracks)
        return InspectorFieldState(field_name, state, common, pending, supported, len(tracks))

    def propose_set(self, tracks: list[AudioTrackItem], field_name: str, value: object) -> ProposalResult:
        return self._propose(tracks, field_name, ChangeAction.SET, value)

    def propose_add_artwork(self, tracks: list[AudioTrackItem], entry: ArtworkEntry) -> ProposalResult:
        affected = unsupported = 0
        for track in tracks:
            # covr cannot retain picture roles/descriptions, so do not offer a
            # deceptively destructive "Add" there.
            if not self._field_supported(track, ARTWORK_FIELD) or track.format_id == "m4a":
                unsupported += 1; continue
            track.proposed.add_artwork(entry, original=track.original.artwork)
            if ARTWORK_FIELD in track.proposed.changed_fields(track.original): affected += 1
        return ProposalResult(affected, unsupported)

    def propose_clear(self, tracks: list[AudioTrackItem], field_name: str) -> ProposalResult:
        return self._propose(tracks, field_name, ChangeAction.CLEAR, None)

    def revert(self, tracks: list[AudioTrackItem], field_names: Iterable[str]) -> None:
        for track in tracks:
            for field_name in field_names:
                if field_name == LYRICS_FIELD:
                    track.proposed.revert_lyrics()
                elif field_name == ARTWORK_FIELD:
                    track.proposed.revert_artwork()
                elif field_name in REPLAYGAIN_FIELDS:
                    track.proposed.revert_replay_gain({field_name})
                elif field_name in track.proposed.__dataclass_fields__:
                    setattr(track.proposed, field_name, None)

    def _propose(
        self,
        tracks: list[AudioTrackItem],
        field_name: str,
        action: ChangeAction,
        value: object,
    ) -> ProposalResult:
        affected = 0
        unsupported = 0
        for track in tracks:
            if not self._field_supported(track, field_name):
                unsupported += 1
                continue
            if field_name == LYRICS_FIELD:
                if action == ChangeAction.CLEAR:
                    track.proposed.clear_lyrics()
                else:
                    track.proposed.set_lyrics(value, original=track.original.lyrics)
            elif field_name == ARTWORK_FIELD:
                if action == ChangeAction.CLEAR:
                    track.proposed.remove_artwork(original=track.original.artwork)
                elif isinstance(value, ArtworkEntry):
                    track.proposed.set_artwork(value, original=track.original.artwork)
                else:
                    raise TypeError("Artwork proposal requires ArtworkEntry")
            elif field_name in REPLAYGAIN_FIELDS:
                if action == ChangeAction.CLEAR:
                    track.proposed.clear_replay_gain({field_name})
                else:
                    track.proposed.set_replay_gain(field_name, value)
            else:
                setattr(track.proposed, field_name, self._legacy_proposal_value(track, field_name, action, value))
            if field_name in track.proposed.changed_fields(track.original):
                affected += 1
        return ProposalResult(affected, unsupported)

    def effective_value(self, track: AudioTrackItem, field_name: str):
        return track.proposed.effective_tags(track.original).field_value(field_name)

    def _field_supported(self, track: AudioTrackItem, field_name: str) -> bool:
        return bool(
            track.metadata_editable
            and self.registry.by_id(track.format_id).supports_field(field_name)
        )

    @staticmethod
    def _legacy_proposal_value(
        track: AudioTrackItem,
        field_name: str,
        action: ChangeAction,
        value: object,
    ):
        if action == ChangeAction.CLEAR:
            return -1 if field_name in {"track_num", "track_total", "disc_num", "disc_total", "bpm"} else ""
        if field_name in {"track_num", "track_total", "disc_num", "disc_total", "bpm"}:
            return int(value)
        return str(value)

    @staticmethod
    def _empty(field_name: str, value: object) -> bool:
        if field_name == LYRICS_FIELD:
            return not LyricsValue.from_dict(value).has_unsynchronized
        if field_name == ARTWORK_FIELD:
            return not ArtworkValue.from_dict(value).entries
        if field_name in REPLAYGAIN_FIELDS:
            return value is None
        return value in (None, "", ())
