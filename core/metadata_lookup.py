"""Qt-free contracts for explicit, review-first online metadata lookup."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
from typing import Protocol, runtime_checkable


ONLINE_METADATA_FIELDS = frozenset({
    "title", "artist", "album", "album_artist", "track_num", "track_total",
    "disc_num", "disc_total", "year", "genre", "isrc", "publisher", "artwork",
})


class LookupMode(str, Enum):
    TRACK = "track"
    ALBUM = "album"


class LookupState(str, Enum):
    READY = "ready"
    NO_RESULTS = "no_results"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    OFFLINE = "offline"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class ProviderErrorKind(str, Enum):
    OFFLINE = "offline"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    CANCELLED = "cancelled"
    INVALID_ARTWORK = "invalid_artwork"
    NO_ARTWORK = "no_artwork"
    INVALID_MIME = "invalid_mime"
    ARTWORK_TOO_LARGE = "artwork_too_large"
    UNKNOWN = "unknown"


class FieldDifference(str, Enum):
    CHANGE = "change"
    NO_OP = "no_op"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


class AlbumMappingState(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class ProviderAttribution:
    provider_id: str
    display_name_key: str
    text: str
    url: str


@dataclass(frozen=True)
class ProviderError:
    kind: ProviderErrorKind
    message_key: str
    retryable: bool = False
    retry_after_s: float | None = None


@dataclass(frozen=True)
class LocalTrackSnapshot:
    item_id: int
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_num: int | None = None
    track_total: int | None = None
    disc_num: int | None = None
    disc_total: int | None = None
    date: str = ""
    genre: str = ""
    isrc: str = ""
    publisher: str = ""
    duration_ms: int | None = None
    # A frozen copy of the Phase 5 capability decision.  Lookup code must not
    # rediscover capabilities from a filename or extension.
    format_id: str = ""
    metadata_editable: bool = True
    editable_fields: frozenset[str] = field(default_factory=lambda: ONLINE_METADATA_FIELDS)

    def supports(self, field_name: str) -> bool:
        return self.metadata_editable and field_name in self.editable_fields


@dataclass(frozen=True)
class LookupRequest:
    request_id: str
    workspace_generation: int
    change_revision: int
    item_ids: tuple[int, ...]
    provider_id: str
    mode: LookupMode
    title: str = ""
    artist: str = ""
    album: str = ""
    local_tracks: tuple[LocalTrackSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.item_ids))) != self.item_ids:
            raise ValueError("item_ids must be unique and sorted")
        if tuple(track.item_id for track in self.local_tracks) != self.item_ids:
            raise ValueError("local_tracks must align with item_ids")


@dataclass(frozen=True)
class ArtworkReference:
    release_id: str
    available: bool = False
    source_url: str = ""


@dataclass(frozen=True)
class ReleaseTrack:
    recording_id: str = ""
    title: str = ""
    artist: str = ""
    track_num: int | None = None
    track_total: int | None = None
    disc_num: int | None = None
    disc_total: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class ScoreEvidence:
    component: str
    weight: int
    similarity: float
    detail: str


@dataclass(frozen=True)
class MetadataCandidate:
    provider_id: str
    candidate_id: str
    recording_id: str = ""
    release_id: str = ""
    release_group_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_num: int | None = None
    track_total: int | None = None
    disc_num: int | None = None
    disc_total: int | None = None
    date: str = ""
    genre: str = ""
    isrc: str = ""
    publisher: str = ""
    country: str = ""
    duration_ms: int | None = None
    release_status: str = ""
    release_type: str = ""
    artwork: ArtworkReference | None = None
    attribution: ProviderAttribution | None = None
    source_url: str = ""
    tracks: tuple[ReleaseTrack, ...] = ()
    score: float = 0.0
    evidence: tuple[ScoreEvidence, ...] = ()

    def field_value(self, field_name: str):
        mapping = {
            "year": self.date,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
        }
        return mapping.get(field_name, getattr(self, field_name, None))


@dataclass(frozen=True)
class LookupResult:
    request: LookupRequest
    state: LookupState
    candidates: tuple[MetadataCandidate, ...] = ()
    error: ProviderError | None = None
    partial: bool = False
    from_cache: bool = False


@dataclass(frozen=True)
class ReleaseDetailRequest:
    """Identity for an explicit, selected-release expansion request."""
    detail_request_id: str
    parent_request: LookupRequest
    candidate_id: str
    release_id: str


@dataclass(frozen=True)
class ReleaseDetailResult:
    request: ReleaseDetailRequest
    state: LookupState
    candidate: MetadataCandidate | None = None
    error: ProviderError | None = None
    from_cache: bool = False


@dataclass(frozen=True)
class FieldComparison:
    item_id: int
    field: str
    local_value: object
    online_value: object
    difference: FieldDifference
    recommended: bool = False


@dataclass(frozen=True)
class AlbumTrackMapping:
    item_id: int
    state: AlbumMappingState
    track: ReleaseTrack | None = None
    evidence: tuple[ScoreEvidence, ...] = ()


@dataclass(frozen=True)
class MatchPreview:
    request: LookupRequest
    candidate: MetadataCandidate
    comparisons: tuple[FieldComparison, ...]
    album_mappings: tuple[AlbumTrackMapping, ...] = ()
    artwork_supported_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AcceptedFieldSelection:
    selected: frozenset[tuple[int, str]] = field(default_factory=frozenset)
    artwork_item_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ArtworkCandidate:
    provider_id: str
    release_id: str
    image_id: str
    types: tuple[str, ...]
    front: bool
    back: bool
    mime_type: str
    thumbnail_url: str
    image_url: str
    source_url: str
    attribution: ProviderAttribution


@dataclass(frozen=True)
class ArtworkRequest:
    """Immutable identity binding CAA work to one chosen metadata result."""
    artwork_request_id: str
    parent_request_id: str
    workspace_generation: int
    change_revision: int
    item_ids: tuple[int, ...]
    candidate_id: str
    release_id: str

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.item_ids))) != self.item_ids:
            raise ValueError("item_ids must be unique and sorted")


@dataclass(frozen=True)
class ArtworkResult:
    request: ArtworkRequest
    state: LookupState
    candidates: tuple[ArtworkCandidate, ...] = ()
    selected: ArtworkCandidate | None = None
    data: bytes = b""
    error: ProviderError | None = None


class CancellationToken:
    """Small cooperative token usable from Core and QThread workers."""
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(max(0.0, timeout))


@runtime_checkable
class MetadataLookupProvider(Protocol):
    provider_id: str
    display_name_key: str
    attribution: ProviderAttribution
    supported_modes: frozenset[LookupMode]

    def lookup(self, request: LookupRequest, cancellation: CancellationToken) -> LookupResult: ...
    def lookup_release_detail(self, request: ReleaseDetailRequest, cancellation: CancellationToken) -> ReleaseDetailResult: ...


@runtime_checkable
class ArtworkLookupProvider(Protocol):
    provider_id: str
    display_name_key: str
    attribution: ProviderAttribution

    def list_artwork(self, release_id: str, cancellation: CancellationToken) -> tuple[ArtworkCandidate, ...]: ...
    def download_preview(self, candidate: ArtworkCandidate, cancellation: CancellationToken) -> bytes: ...
    def download_full(self, candidate: ArtworkCandidate, cancellation: CancellationToken) -> bytes: ...


@runtime_checkable
class FingerprintProvider(Protocol):
    """Deferred AcoustID/Chromaprint extension point; no implementation in Phase 11."""
    provider_id: str

    def fingerprint(self, item_id: int, cancellation: CancellationToken) -> str: ...
