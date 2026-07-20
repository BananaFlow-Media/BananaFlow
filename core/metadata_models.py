"""
core/metadata_models.py  –  Data classes for the Tag Editor feature
====================================================================
Pure Python — zero Qt imports. All classes are mutable dataclasses
that can be serialised to / from plain dicts for JSON backup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import base64
import hmac
import math
from pathlib import Path
import re
from typing import Optional


# Only fields with real multi-value mappings use this contract.  UI/legacy
# callers may supply a semicolon-delimited string; the canonical form is an
# ordered tuple and never depends on separator spacing.
MULTI_VALUE_FIELDS = frozenset({"artist", "album_artist", "genre", "composer"})

LYRICS_FIELD = "lyrics"
ARTWORK_FIELD = "artwork"
REPLAYGAIN_TRACK_GAIN = "replaygain_track_gain"
REPLAYGAIN_TRACK_PEAK = "replaygain_track_peak"
REPLAYGAIN_ALBUM_GAIN = "replaygain_album_gain"
REPLAYGAIN_ALBUM_PEAK = "replaygain_album_peak"
REPLAYGAIN_REFERENCE_LOUDNESS = "replaygain_reference_loudness"
REPLAYGAIN_FIELDS = frozenset({
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_REFERENCE_LOUDNESS,
})


def normalize_lyrics_text(value: object) -> str:
    """Normalize transport line endings without trimming meaningful whitespace."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\x00")


@dataclass(frozen=True)
class LyricsEntry:
    """One embedded lyrics variant.

    ``synchronized`` entries are represented so the Inspector can explain
    their presence, but Phase 6 never edits or deletes them. ``source`` is a
    container-local key (for example ``LYRICS``) used only to preserve the
    original mapping on write.
    """

    text: str = ""
    language: str = "und"
    description: str = ""
    synchronized: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_lyrics_text(self.text))
        language = str(self.language or "und").strip().lower()
        object.__setattr__(self, "language", language or "und")
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "source", str(self.source or ""))

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "language": self.language,
            "description": self.description,
            "synchronized": self.synchronized,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object) -> "LyricsEntry":
        if isinstance(value, LyricsEntry):
            return value
        if not isinstance(value, dict):
            return cls(text=normalize_lyrics_text(value))
        return cls(
            text=value.get("text", ""),
            language=value.get("language", "und"),
            description=value.get("description", ""),
            synchronized=bool(value.get("synchronized", False)),
            source=value.get("source", ""),
        )


@dataclass(frozen=True)
class LyricsValue:
    """All embedded lyrics variants plus a deterministic displayed primary."""

    entries: tuple[LyricsEntry, ...] = ()
    primary_index: int | None = None

    def __post_init__(self) -> None:
        entries = tuple(LyricsEntry.from_dict(entry) for entry in self.entries)
        object.__setattr__(self, "entries", entries)
        primary = self.primary_index
        if primary is None or not (0 <= primary < len(entries)) or entries[primary].synchronized:
            candidates = [
                (0 if entry.description.casefold() in {"", "lyrics"} else 1,
                 0 if entry.language == "eng" else 1,
                 entry.language, entry.description.casefold(), index)
                for index, entry in enumerate(entries) if not entry.synchronized
            ]
            primary = min(candidates)[-1] if candidates else None
        object.__setattr__(self, "primary_index", primary)

    @property
    def primary(self) -> LyricsEntry | None:
        return self.entries[self.primary_index] if self.primary_index is not None else None

    @property
    def has_unsynchronized(self) -> bool:
        return any(not entry.synchronized for entry in self.entries)

    @property
    def has_synchronized(self) -> bool:
        return any(entry.synchronized for entry in self.entries)

    @property
    def secondary_count(self) -> int:
        return max(0, sum(not entry.synchronized for entry in self.entries) - (1 if self.primary else 0))

    def replace_primary(
        self,
        text: object,
        *,
        language: str | None = None,
        description: str | None = None,
        source: str | None = None,
    ) -> "LyricsValue":
        current = self.primary
        replacement = LyricsEntry(
            text=normalize_lyrics_text(text),
            language=language if language is not None else (current.language if current else "und"),
            description=description if description is not None else (current.description if current else "Lyrics"),
            source=source if source is not None else (current.source if current else ""),
        )
        entries = list(self.entries)
        if self.primary_index is None:
            insert_at = next((i for i, entry in enumerate(entries) if entry.synchronized), len(entries))
            entries.insert(insert_at, replacement)
            return LyricsValue(tuple(entries), insert_at)
        entries[self.primary_index] = replacement
        return LyricsValue(tuple(entries), self.primary_index)

    def clear_unsynchronized(self) -> "LyricsValue":
        """Explicit Clear removes unsynchronized lyrics; timed lyrics survive."""
        return LyricsValue(tuple(entry for entry in self.entries if entry.synchronized))

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "primary_index": self.primary_index,
        }

    @classmethod
    def from_dict(cls, value: object) -> "LyricsValue":
        if isinstance(value, LyricsValue):
            return value
        if value in (None, ""):
            return cls()
        if isinstance(value, str):
            return cls((LyricsEntry(text=value),))
        if not isinstance(value, dict):
            return cls()
        return cls(
            tuple(LyricsEntry.from_dict(entry) for entry in value.get("entries", ())),
            value.get("primary_index"),
        )


class ArtworkOperation(str, Enum):
    """Explicit, user-visible artwork proposal kinds.

    The operation describes the proposal while ``ArtworkValue.entries`` is the
    complete intended embedded-picture set.  Keeping both avoids the historic
    ambiguity where an empty preview could mean no picture, mixed values, or a
    request to delete a picture.
    """
    UNCHANGED = "unchanged"
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"


class ArtworkReadState(str, Enum):
    """Read diagnostics; intentionally separate from proposed operations."""
    VALID = "valid"
    EMPTY = "empty"
    PARTIAL = "partial"
    INVALID = "invalid"
    READ_FAILED = "read_failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ArtworkDiagnostic:
    code: str
    detail: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class ArtworkEntry:
    """Immutable encoded embedded picture; deliberately independent of Qt."""
    data: bytes
    mime_type: str
    picture_type: int = 3
    description: str = ""
    width: int = 0
    height: int = 0
    depth: int = 0
    colors: int = 0
    source_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", bytes(self.data))
        object.__setattr__(self, "mime_type", str(self.mime_type or "").lower())
        object.__setattr__(self, "picture_type", int(self.picture_type))
        object.__setattr__(self, "description", str(self.description or ""))

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def identity(self) -> tuple[object, ...]:
        return (self.content_hash, self.mime_type, self.picture_type, self.description)

    def to_dict(self) -> dict[str, object]:
        return {"data": base64.b64encode(self.data).decode("ascii"),
                "mime_type": self.mime_type, "picture_type": self.picture_type,
                "description": self.description, "width": self.width,
                "height": self.height, "depth": self.depth, "colors": self.colors,
                "source_id": self.source_id, "content_hash": self.content_hash}


@dataclass(frozen=True)
class ArtworkValue:
    """All pictures plus deterministic primary selection and proposal state."""
    entries: tuple[ArtworkEntry, ...] = ()
    primary_index: int | None = None
    operation: ArtworkOperation = ArtworkOperation.UNCHANGED
    read_state: ArtworkReadState = ArtworkReadState.EMPTY
    diagnostics: tuple[ArtworkDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        object.__setattr__(self, "entries", entries)
        primary = self.primary_index
        if primary is None or not 0 <= primary < len(entries):
            # Front cover wins; otherwise stable container/order fallback.
            primary = next((i for i, entry in enumerate(entries) if entry.picture_type == 3), None)
            if primary is None and entries:
                primary = 0
        object.__setattr__(self, "primary_index", primary)
        if not isinstance(self.operation, ArtworkOperation):
            object.__setattr__(self, "operation", ArtworkOperation(str(self.operation)))
        diagnostics = tuple(
            diagnostic if isinstance(diagnostic, ArtworkDiagnostic) else ArtworkDiagnostic(**diagnostic)
            for diagnostic in self.diagnostics
        )
        object.__setattr__(self, "diagnostics", diagnostics)
        if not isinstance(self.read_state, ArtworkReadState):
            object.__setattr__(self, "read_state", ArtworkReadState(str(self.read_state)))
        if self.read_state is ArtworkReadState.EMPTY and entries:
            object.__setattr__(self, "read_state", ArtworkReadState.PARTIAL if diagnostics else ArtworkReadState.VALID)
        elif self.read_state is ArtworkReadState.EMPTY and diagnostics:
            object.__setattr__(self, "read_state", ArtworkReadState.INVALID)

    @property
    def primary(self) -> ArtworkEntry | None:
        return self.entries[self.primary_index] if self.primary_index is not None else None

    def semantically_equal(self, other: object) -> bool:
        other = ArtworkValue.from_dict(other)
        return tuple(entry.identity for entry in self.entries) == tuple(entry.identity for entry in other.entries)

    def with_primary_replaced(self, entry: ArtworkEntry) -> "ArtworkValue":
        entries = list(self.entries)
        if self.primary_index is None:
            entries.insert(0, entry)
            return ArtworkValue(tuple(entries), 0, ArtworkOperation.REPLACE)
        entries[self.primary_index] = entry
        return ArtworkValue(tuple(entries), self.primary_index, ArtworkOperation.REPLACE)

    def with_added(self, entry: ArtworkEntry) -> "ArtworkValue":
        entries = (*self.entries, entry)
        return ArtworkValue(entries, self.primary_index, ArtworkOperation.ADD)

    def without_primary(self) -> "ArtworkValue":
        if self.primary_index is None:
            return ArtworkValue((), None, ArtworkOperation.REMOVE)
        entries = list(self.entries)
        del entries[self.primary_index]
        return ArtworkValue(tuple(entries), None, ArtworkOperation.REMOVE)

    def without_all(self) -> "ArtworkValue":
        return ArtworkValue((), None, ArtworkOperation.REMOVE)

    def to_dict(self) -> dict[str, object]:
        return {"captured": True, "entries": [entry.to_dict() for entry in self.entries],
                "primary_index": self.primary_index, "operation": self.operation.value,
                "read_state": self.read_state.value,
                "diagnostics": [diagnostic.__dict__ for diagnostic in self.diagnostics]}

    @classmethod
    def from_dict(cls, value: object, *, verify_integrity: bool = False, require_integrity: bool = False) -> "ArtworkValue":
        if isinstance(value, ArtworkValue):
            return value
        if not isinstance(value, dict):
            return cls()
        raw_entries = value.get("entries", ())
        if not isinstance(raw_entries, (list, tuple)):
            raise ValueError("invalid artwork entries")
        entries: list[ArtworkEntry] = []
        for item in raw_entries:
            if not isinstance(item, dict): raise ValueError("invalid artwork entry")
            encoded = item.get("data", "")
            if not isinstance(encoded, str): raise ValueError("invalid artwork payload")
            data = base64.b64decode(encoded, validate=True)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("artwork payload too large")
            stored_hash = item.get("content_hash")
            if require_integrity and (not isinstance(stored_hash, str) or len(stored_hash) != 64):
                raise ValueError("missing or malformed artwork integrity hash")
            entry = ArtworkEntry(data, item.get("mime_type", ""), item.get("picture_type", 3),
                item.get("description", ""), item.get("width", 0), item.get("height", 0),
                item.get("depth", 0), item.get("colors", 0), item.get("source_id", ""))
            if verify_integrity and not hmac.compare_digest(entry.content_hash, str(stored_hash).lower()):
                raise ValueError("artwork integrity hash mismatch")
            if verify_integrity:
                from core.artwork import validate_artwork_bytes
                validate_artwork_bytes(data, description=entry.description, picture_type=entry.picture_type)
            entries.append(entry)
        diagnostics = tuple(ArtworkDiagnostic(**item) for item in value.get("diagnostics", ()) if isinstance(item, dict))
        return cls(tuple(entries), value.get("primary_index"), value.get("operation", "unchanged"),
                   value.get("read_state", "empty"), diagnostics)


_REPLAYGAIN_DECIMAL = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_REPLAYGAIN_GAIN_RE = re.compile(
    rf"(?P<number>{_REPLAYGAIN_DECIMAL})\s+dB", re.IGNORECASE
)
_REPLAYGAIN_PEAK_RE = re.compile(rf"(?P<number>{_REPLAYGAIN_DECIMAL})")


def parse_replaygain_number(value: object, *, peak: bool = False) -> float | None:
    """Parse one complete canonical ReplayGain value.

    Stored gain/reference fields require a ``dB`` unit. Peak fields are
    unitless linear ratios. Numeric objects are accepted for trusted internal
    analysis results, while strings must match the complete normalized syntax;
    embedded numbers, non-finite values and locale-comma forms are rejected.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        match = (_REPLAYGAIN_PEAK_RE if peak else _REPLAYGAIN_GAIN_RE).fullmatch(text)
        if match is None:
            return None
        try:
            number = float(match.group("number"))
        except ValueError:
            return None
    if not math.isfinite(number) or (peak and number < 0):
        return None
    return number


@dataclass(frozen=True)
class ReplayGainValues:
    """Canonical ReplayGain values; gains are dB and peaks are linear ratios."""

    track_gain_db: float | None = None
    track_peak: float | None = None
    album_gain_db: float | None = None
    album_peak: float | None = None
    reference_loudness_db: float | None = None

    def field_value(self, field_name: str) -> float | None:
        return {
            REPLAYGAIN_TRACK_GAIN: self.track_gain_db,
            REPLAYGAIN_TRACK_PEAK: self.track_peak,
            REPLAYGAIN_ALBUM_GAIN: self.album_gain_db,
            REPLAYGAIN_ALBUM_PEAK: self.album_peak,
            REPLAYGAIN_REFERENCE_LOUDNESS: self.reference_loudness_db,
        }[field_name]

    def with_field(self, field_name: str, value: object) -> "ReplayGainValues":
        values = self.to_dict()
        values[field_name] = parse_replaygain_number(
            value, peak=field_name in {REPLAYGAIN_TRACK_PEAK, REPLAYGAIN_ALBUM_PEAK}
        )
        return ReplayGainValues.from_dict(values)

    def to_dict(self) -> dict[str, float | None]:
        return {
            REPLAYGAIN_TRACK_GAIN: self.track_gain_db,
            REPLAYGAIN_TRACK_PEAK: self.track_peak,
            REPLAYGAIN_ALBUM_GAIN: self.album_gain_db,
            REPLAYGAIN_ALBUM_PEAK: self.album_peak,
            REPLAYGAIN_REFERENCE_LOUDNESS: self.reference_loudness_db,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReplayGainValues":
        if isinstance(value, ReplayGainValues):
            return value
        data = value if isinstance(value, dict) else {}
        return cls(
            track_gain_db=parse_replaygain_number(data.get(REPLAYGAIN_TRACK_GAIN)),
            track_peak=parse_replaygain_number(data.get(REPLAYGAIN_TRACK_PEAK), peak=True),
            album_gain_db=parse_replaygain_number(data.get(REPLAYGAIN_ALBUM_GAIN)),
            album_peak=parse_replaygain_number(data.get(REPLAYGAIN_ALBUM_PEAK), peak=True),
            reference_loudness_db=parse_replaygain_number(data.get(REPLAYGAIN_REFERENCE_LOUDNESS)),
        )


def normalize_multi_value(value) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        values = value.split(";")
    elif isinstance(value, (tuple, list)):
        values = value
    else:
        values = (value,)
    return tuple(str(part).strip() for part in values if str(part).strip())


def metadata_values_equal(field_name: str, left, right) -> bool:
    """Semantic equality shared by legacy proposals and readback checks."""
    if field_name in MULTI_VALUE_FIELDS:
        return normalize_multi_value(left) == normalize_multi_value(right)
    if field_name in REPLAYGAIN_FIELDS:
        left_num = parse_replaygain_number(left, peak="peak" in field_name)
        right_num = parse_replaygain_number(right, peak="peak" in field_name)
        if left_num is None or right_num is None:
            return left_num is right_num
        return math.isclose(left_num, right_num, rel_tol=1e-7, abs_tol=1e-7)
    if field_name == LYRICS_FIELD:
        def semantic(value):
            lyrics = LyricsValue.from_dict(value)
            return tuple(
                (entry.text, entry.language, entry.description, entry.synchronized)
                for entry in lyrics.entries
            )
        return semantic(left) == semantic(right)
    if field_name == ARTWORK_FIELD:
        return ArtworkValue.from_dict(left).semantically_equal(right)
    return left == right


class MetadataField(str, Enum):
    """Stable, format-neutral identifiers used by the metadata backend."""
    TITLE = "title"; ARTIST = "artist"; ALBUM = "album"; ALBUM_ARTIST = "album_artist"
    TRACK_NUMBER = "track_num"; TRACK_TOTAL = "track_total"
    DISC_NUMBER = "disc_num"; DISC_TOTAL = "disc_total"
    DATE = "year"; GENRE = "genre"; COMMENT = "comment"; COMPOSER = "composer"
    PUBLISHER = "publisher"; COPYRIGHT = "copyright"; BPM = "bpm"; ISRC = "isrc"
    GROUPING = "grouping"; SORT_TITLE = "sort_title"; SORT_ARTIST = "sort_artist"
    SORT_ALBUM = "sort_album"; SORT_ALBUM_ARTIST = "sort_album_artist"
    LYRICS = LYRICS_FIELD
    ARTWORK = ARTWORK_FIELD
    REPLAYGAIN_TRACK_GAIN = REPLAYGAIN_TRACK_GAIN
    REPLAYGAIN_TRACK_PEAK = REPLAYGAIN_TRACK_PEAK
    REPLAYGAIN_ALBUM_GAIN = REPLAYGAIN_ALBUM_GAIN
    REPLAYGAIN_ALBUM_PEAK = REPLAYGAIN_ALBUM_PEAK
    REPLAYGAIN_REFERENCE_LOUDNESS = REPLAYGAIN_REFERENCE_LOUDNESS


class ChangeAction(str, Enum):
    """A delta is always explicit: unchanged, set, or clear."""
    UNCHANGED = "unchanged"
    SET = "set"
    CLEAR = "clear"


@dataclass(frozen=True)
class FieldChange:
    """One proposed operation; ``None`` is never used to mean clear."""
    action: ChangeAction = ChangeAction.UNCHANGED
    value: object | None = None


@dataclass(frozen=True)
class CanonicalMetadata:
    """Normalized metadata: absent fields are omitted; multi-values are tuples."""
    values: dict[str, object] = field(default_factory=dict)
    artwork: ArtworkValue = field(default_factory=ArtworkValue)
    file_properties: dict[str, object] = field(default_factory=dict)

    def get(self, field_name: MetadataField | str, default=None):
        return self.values.get(str(field_name.value if isinstance(field_name, MetadataField) else field_name), default)


@dataclass(frozen=True)
class MetadataDelta:
    """Format-neutral write plan input. Only SET and CLEAR can be written."""
    changes: dict[str, FieldChange] = field(default_factory=dict)

    @property
    def changed_fields(self) -> set[str]:
        return {name for name, change in self.changes.items() if change.action != ChangeAction.UNCHANGED}


# ──────────────────────────────────────────────────────────────────────────────
# Tag state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class OriginalTags:
    """Tags as they currently exist on disk."""
    title:        str = ""
    artist:       str = ""
    album:        str = ""
    album_artist: str = ""
    track_num:    Optional[int] = None
    track_total:  Optional[int] = None
    comment:      str = ""
    year:         str = ""
    genre:        str = ""
    disc_num:     Optional[int] = None
    disc_total:   Optional[int] = None
    composer:     str = ""
    publisher:    str = ""
    copyright:    str = ""
    bpm:          Optional[int] = None
    isrc:         str = ""
    grouping:     str = ""
    sort_title:   str = ""
    sort_artist:  str = ""
    sort_album:   str = ""
    sort_album_artist: str = ""
    lyrics: LyricsValue = field(default_factory=LyricsValue)
    artwork: ArtworkValue = field(default_factory=ArtworkValue)
    # ``False`` means a legacy backup did not authoritatively capture artwork.
    artwork_captured: bool = False
    replay_gain: ReplayGainValues = field(default_factory=ReplayGainValues)
    file_properties: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title":        self.title,
            "artist":       self.artist,
            "album":        self.album,
            "album_artist": self.album_artist,
            "track_num":    self.track_num,
            "track_total":  self.track_total,
            "comment":      self.comment,
            "year":         self.year,
            "genre":        self.genre,
            "disc_num": self.disc_num, "disc_total": self.disc_total,
            "composer": self.composer, "publisher": self.publisher,
            "copyright": self.copyright, "bpm": self.bpm, "isrc": self.isrc,
            "grouping": self.grouping, "sort_title": self.sort_title,
            "sort_artist": self.sort_artist, "sort_album": self.sort_album,
            "sort_album_artist": self.sort_album_artist,
            "lyrics": self.lyrics.to_dict(),
            "artwork": self.artwork.to_dict(),
            "artwork_captured": self.artwork_captured,
            "replay_gain": self.replay_gain.to_dict(),
            "file_properties": dict(self.file_properties),
        }

    @classmethod
    def from_dict(cls, value: object) -> "OriginalTags":
        if isinstance(value, OriginalTags):
            return value
        if not isinstance(value, dict):
            return cls()
        known = cls.__dataclass_fields__
        data = {key: item for key, item in value.items() if key in known}
        data["lyrics"] = LyricsValue.from_dict(data.get("lyrics"))
        data["artwork"] = ArtworkValue.from_dict(data.get("artwork"))
        data["artwork_captured"] = bool(data.get("artwork_captured", False))
        data["replay_gain"] = ReplayGainValues.from_dict(data.get("replay_gain"))
        props = data.get("file_properties")
        data["file_properties"] = dict(props) if isinstance(props, dict) else {}
        return cls(**data)

    def field_value(self, field_name: str):
        if field_name == LYRICS_FIELD:
            return self.lyrics
        if field_name == ARTWORK_FIELD:
            return self.artwork
        if field_name in REPLAYGAIN_FIELDS:
            return self.replay_gain.field_value(field_name)
        return getattr(self, field_name)


@dataclass
class ProposedTags:
    """
    Proposed changes — overlaid on OriginalTags at apply time.

    Convention:
      None  = "leave unchanged"
      ""    = "clear this field"
      value = "set to this value"
    """
    title:        Optional[str] = None
    artist:       Optional[str] = None
    album:        Optional[str] = None
    album_artist: Optional[str] = None
    track_num:    Optional[int] = None   # -1 means "clear"
    comment:      Optional[str] = None
    year:         Optional[str] = None
    genre:        Optional[str] = None
    track_total:  Optional[int] = None  # -1 explicitly clears
    disc_num:     Optional[int] = None  # -1 explicitly clears
    disc_total:   Optional[int] = None  # -1 explicitly clears
    composer:     Optional[str] = None
    publisher:    Optional[str] = None
    copyright:    Optional[str] = None
    bpm:          Optional[int] = None  # -1 explicitly clears
    isrc:         Optional[str] = None
    grouping:     Optional[str] = None
    sort_title:   Optional[str] = None
    sort_artist:  Optional[str] = None
    sort_album:   Optional[str] = None
    sort_album_artist: Optional[str] = None
    lyrics_change: FieldChange = field(default_factory=FieldChange)
    artwork_change: FieldChange = field(default_factory=FieldChange)
    replay_gain_changes: dict[str, FieldChange] = field(default_factory=dict)

    def has_changes(self, original: OriginalTags) -> bool:
        """True if any proposed field would actually change the original."""
        return bool(self.changed_fields(original))

    def changed_fields(self, original: OriginalTags) -> set[str]:
        """
        Return the set of tag-field names this proposal would actually change.

        This is the Phase-1 write scope: the atomic writer touches *only*
        these fields on the media file, so every other tag (COMM, artwork,
        lyrics, ReplayGain, custom TXXX/freeform, MusicBrainz IDs and
        multi-value fields) is preserved untouched (TE-SAFE-07 / R-PRESERVE).
        """
        changed: set[str] = set()
        checks = [
            ("title",        self.title,        original.title),
            ("artist",       self.artist,       original.artist),
            ("album",        self.album,        original.album),
            ("album_artist", self.album_artist, original.album_artist),
            ("comment",      self.comment,      original.comment),
            ("year",         self.year,         original.year),
            ("genre",        self.genre,        original.genre),
        ]
        for name, proposed, orig in checks:
            if proposed is not None and not metadata_values_equal(name, proposed, orig):
                changed.add(name)
        if self.track_num is not None:
            eff = None if self.track_num == -1 else self.track_num
            if eff != original.track_num:
                changed.add("track_num")
        for name in ("track_total", "disc_num", "disc_total", "bpm"):
            proposed = getattr(self, name)
            if proposed is not None and (None if proposed == -1 else proposed) != getattr(original, name):
                changed.add(name)
        for name in ("composer", "publisher", "copyright", "isrc", "grouping", "sort_title", "sort_artist", "sort_album", "sort_album_artist"):
            proposed = getattr(self, name)
            if proposed is not None and not metadata_values_equal(name, proposed, getattr(original, name)):
                changed.add(name)
        if self.lyrics_change.action != ChangeAction.UNCHANGED:
            effective_lyrics = self._effective_lyrics(original.lyrics)
            if not metadata_values_equal(LYRICS_FIELD, effective_lyrics, original.lyrics):
                changed.add(LYRICS_FIELD)
        if self.artwork_change.action != ChangeAction.UNCHANGED:
            if (not metadata_values_equal(ARTWORK_FIELD, self._effective_artwork(original.artwork), original.artwork)
                    or (self.artwork_change.action == ChangeAction.CLEAR and original.artwork.diagnostics)):
                changed.add(ARTWORK_FIELD)
        for name, change in self.replay_gain_changes.items():
            if name not in REPLAYGAIN_FIELDS or change.action == ChangeAction.UNCHANGED:
                continue
            value = None if change.action == ChangeAction.CLEAR else change.value
            if not metadata_values_equal(name, value, original.replay_gain.field_value(name)):
                changed.add(name)
        return changed

    def _effective_lyrics(self, original: LyricsValue) -> LyricsValue:
        change = self.lyrics_change
        if change.action == ChangeAction.UNCHANGED:
            return original
        if change.action == ChangeAction.CLEAR:
            return original.clear_unsynchronized()
        if isinstance(change.value, LyricsValue):
            return change.value
        if isinstance(change.value, LyricsEntry):
            return original.replace_primary(
                change.value.text,
                language=change.value.language,
                description=change.value.description,
                source=change.value.source,
            )
        return original.replace_primary(change.value or "")

    def _effective_artwork(self, original: ArtworkValue) -> ArtworkValue:
        if self.artwork_change.action == ChangeAction.UNCHANGED:
            return original
        if isinstance(self.artwork_change.value, ArtworkValue):
            return self.artwork_change.value
        return original

    def set_artwork(self, entry: ArtworkEntry, *, original: ArtworkValue | None = None) -> None:
        self.artwork_change = FieldChange(ChangeAction.SET, (original or ArtworkValue()).with_primary_replaced(entry))

    def add_artwork(self, entry: ArtworkEntry, *, original: ArtworkValue | None = None) -> None:
        self.artwork_change = FieldChange(ChangeAction.SET, (original or ArtworkValue()).with_added(entry))

    def remove_artwork(self, *, original: ArtworkValue | None = None) -> None:
        self.artwork_change = FieldChange(ChangeAction.CLEAR, (original or ArtworkValue()).without_primary())

    def remove_all_artwork(self, *, original: ArtworkValue | None = None) -> None:
        self.artwork_change = FieldChange(ChangeAction.CLEAR, (original or ArtworkValue()).without_all())

    def revert_artwork(self) -> None:
        self.artwork_change = FieldChange()

    def set_lyrics(
        self,
        text: object,
        *,
        original: LyricsValue | None = None,
        language: str | None = None,
        description: str | None = None,
    ) -> None:
        base = original or LyricsValue()
        if isinstance(text, LyricsValue):
            self.lyrics_change = FieldChange(ChangeAction.SET, text)
            return
        if isinstance(text, LyricsEntry):
            language = text.language
            description = text.description
            source = text.source
            text = text.text
        else:
            source = None
        self.lyrics_change = FieldChange(
            ChangeAction.SET,
            base.replace_primary(
                text,
                language=language,
                description=description,
                source=source,
            ),
        )

    def clear_lyrics(self) -> None:
        self.lyrics_change = FieldChange(ChangeAction.CLEAR)

    def revert_lyrics(self) -> None:
        self.lyrics_change = FieldChange()

    def set_replay_gain(self, field_name: str, value: object) -> None:
        if field_name not in REPLAYGAIN_FIELDS:
            raise ValueError(f"Unknown ReplayGain field: {field_name}")
        number = parse_replaygain_number(value, peak="peak" in field_name)
        if number is None:
            raise ValueError(f"Invalid ReplayGain value for {field_name}: {value!r}")
        self.replay_gain_changes[field_name] = FieldChange(ChangeAction.SET, number)

    def clear_replay_gain(self, fields: set[str] | frozenset[str]) -> None:
        for field_name in fields:
            if field_name not in REPLAYGAIN_FIELDS:
                raise ValueError(f"Unknown ReplayGain field: {field_name}")
            self.replay_gain_changes[field_name] = FieldChange(ChangeAction.CLEAR)

    def revert_replay_gain(self, fields: set[str] | frozenset[str] | None = None) -> None:
        if fields is None:
            self.replay_gain_changes.clear()
            return
        for field_name in fields:
            self.replay_gain_changes.pop(field_name, None)

    def effective_tags(self, original: OriginalTags) -> OriginalTags:
        """Merge proposed values over original, returning the final result."""
        def pick(proposed, orig):
            return orig if proposed is None else proposed

        track = original.track_num
        if self.track_num is not None:
            track = None if self.track_num == -1 else self.track_num

        replay_gain = original.replay_gain
        for name, change in self.replay_gain_changes.items():
            if name in REPLAYGAIN_FIELDS and change.action != ChangeAction.UNCHANGED:
                replay_gain = replay_gain.with_field(
                    name, None if change.action == ChangeAction.CLEAR else change.value
                )

        return OriginalTags(
            title        = pick(self.title,        original.title),
            artist       = pick(self.artist,       original.artist),
            album        = pick(self.album,        original.album),
            album_artist = pick(self.album_artist, original.album_artist),
            track_num    = track,
            comment      = pick(self.comment,      original.comment),
            year         = pick(self.year,         original.year),
            genre        = pick(self.genre,        original.genre),
            track_total  = (original.track_total if self.track_total is None else (None if self.track_total == -1 else self.track_total)),
            disc_num     = (original.disc_num if self.disc_num is None else (None if self.disc_num == -1 else self.disc_num)),
            disc_total   = (original.disc_total if self.disc_total is None else (None if self.disc_total == -1 else self.disc_total)),
            composer=pick(self.composer, original.composer), publisher=pick(self.publisher, original.publisher),
            copyright=pick(self.copyright, original.copyright),
            bpm=(original.bpm if self.bpm is None else (None if self.bpm == -1 else self.bpm)),
            isrc=pick(self.isrc, original.isrc), grouping=pick(self.grouping, original.grouping),
            sort_title=pick(self.sort_title, original.sort_title), sort_artist=pick(self.sort_artist, original.sort_artist),
            sort_album=pick(self.sort_album, original.sort_album), sort_album_artist=pick(self.sort_album_artist, original.sort_album_artist),
            lyrics=self._effective_lyrics(original.lyrics),
            artwork=self._effective_artwork(original.artwork),
            artwork_captured=original.artwork_captured,
            replay_gain=replay_gain,
            file_properties=dict(original.file_properties),
        )

    def clear(self) -> None:
        """Reset all proposed fields to None (revert state)."""
        self.title        = None
        self.artist       = None
        self.album        = None
        self.album_artist = None
        self.track_num    = None
        self.comment      = None
        self.year         = None
        self.genre        = None
        self.track_total = self.disc_num = self.disc_total = None
        self.composer = self.publisher = self.copyright = self.bpm = None
        self.isrc = self.grouping = self.sort_title = self.sort_artist = None
        self.sort_album = self.sort_album_artist = None
        self.lyrics_change = FieldChange()
        self.artwork_change = FieldChange()
        self.replay_gain_changes.clear()


# ──────────────────────────────────────────────────────────────────────────────
# Track item
# ──────────────────────────────────────────────────────────────────────────────

class TrackStatus:
    PENDING     = "pending"
    CHANGED     = "changed"
    DONE        = "done"
    ERROR       = "error"
    UNSUPPORTED = "unsupported"
    READ_ONLY   = "read_only"


@dataclass
class AudioTrackItem:
    """Represents one audio file in the tag-editor session."""
    path:      Path
    folder:    Path       # = path.parent, pre-computed for fast filtering
    ext:       str        # ".mp3" | ".flac" | ".m4a"
    original:  OriginalTags = field(default_factory=OriginalTags)
    proposed:  ProposedTags = field(default_factory=ProposedTags)
    proposed_filename: Optional[str] = None   # rename target (None = no rename)
    # Phase 2: an explicit Apply override.  It is meaningful only while the
    # item has a real proposal; TagEditorWorkspaceState clears stale values.
    excluded_from_apply: bool = False
    status:    str = TrackStatus.PENDING
    error_msg: str = ""
    format_id: str = ""
    metadata_editable: bool = True
    # Captured at scan time.  Phase 8 compares it immediately before a disk
    # operation so a proposal is never applied to a replacement at this path.
    baseline_identity: object | None = None
    # Phase 13 advisory-monitoring state.  Core values are stored here so the
    # existing mutable item remains the single model/workspace identity.
    external_state: str = "current"
    external_detail: str = ""
    external_conflict: object | None = None

    @property
    def display_name(self) -> str:
        return self.path.name

    @property
    def has_changes(self) -> bool:
        return (
            self.proposed.has_changes(self.original)
            or self.proposed_filename is not None
        )


# ──────────────────────────────────────────────────────────────────────────────
# Restore-from-backup result
# ──────────────────────────────────────────────────────────────────────────────

class RestoreStatus:
    RESTORED  = "restored"    # original tags written back to the file
    UNCHANGED = "unchanged"   # file already carries the backup's tags
    MISSING   = "missing"     # file no longer exists at the recorded path
    FAILED    = "failed"      # tag write failed (see error)
    CANCELLED = "cancelled"   # not attempted after a safe cancellation boundary


@dataclass
class RestoreOutcome:
    """Per-file result of restoring tags from a JSON backup."""
    path:   Path
    status: str  # one of RestoreStatus
    error:  str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Scan result + session
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    root:          Path
    tracks:        list[AudioTrackItem] = field(default_factory=list)
    skipped_count: int = 0
    folder_set:    set[Path] = field(default_factory=set)
    #: The scope the user actually scanned with.  Monitoring, Manual Refresh
    #: and every reconciliation reuse it, so a session can never widen itself.
    recursive:     bool = True

    @property
    def files_count(self) -> int:
        return len(self.tracks)

    @property
    def folders_count(self) -> int:
        return len(self.folder_set)


@dataclass
class TagEditSession:
    scan_result:   Optional[ScanResult] = None
    backup_path:   Optional[Path] = None
    apply_done:    int = 0
    apply_failed:  int = 0
    apply_skipped: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 — Apply safety: structured per-file + batch results (TE-SAFE-06)
# ──────────────────────────────────────────────────────────────────────────────

class ApplyStage:
    """Which stage of the per-file apply pipeline an outcome refers to."""
    BACKUP = "backup"
    WRITE  = "write"
    VERIFY = "verify"
    RENAME = "rename"


class ApplyStatus:
    """Terminal disposition of a single file within an apply batch."""
    SUCCESS   = "success"
    FAILED    = "failed"
    SKIPPED   = "skipped"
    CANCELLED = "cancelled"
    PARTIAL   = "partial"    # tags written+verified, but rename blocked/failed


class ApplyErrorCode:
    """Stable, non-localised error codes carried by ApplyOutcome.error_code."""
    NONE              = ""
    BACKUP_ABORTED    = "backup_aborted"
    WRITE_FAILED      = "write_failed"
    VERIFY_FAILED     = "verify_failed"
    RENAME_COLLISION  = "rename_collision"
    RENAME_RESERVED   = "rename_reserved"
    RENAME_INVALID    = "rename_invalid"
    RENAME_ESCAPE     = "rename_escape"
    RENAME_LOCKED     = "rename_locked"
    RENAME_FAILED     = "rename_failed"
    RENAME_BLOCKED_SIBLING = "rename_blocked_sibling"  # a component peer failed
    RENAME_ROLLBACK_FAILED = "rename_rollback_failed"  # temp-hop rollback failed
    CANCELLED         = "cancelled"
    JOURNAL_FAILED    = "journal_failed"
    UNSUPPORTED       = "unsupported"


@dataclass
class ApplyOutcome:
    """
    Structured result for a single file in an apply batch (TE-SAFE-06).

    Carries stage, status, a stable error_code, a localisation key, free-text
    detail, whether the failure is retryable and which tag fields were written.
    `final_path` is the on-disk path after the operation (== original_path when
    no rename happened or a rename was blocked).
    """
    original_path: Path
    final_path:    Path
    stage:         str = ApplyStage.WRITE
    status:        str = ApplyStatus.SUCCESS
    error_code:    str = ApplyErrorCode.NONE
    message_key:   str = ""
    detail:        str = ""
    retryable:     bool = False
    fields_written: list[str] = field(default_factory=list)
    # When a rename is blocked/failed the filename proposal is preserved so the
    # user can retry it; the panel reads this to keep the pending rename.
    rename_pending: bool = False


@dataclass
class ApplyBatchResult:
    """
    Batch-level result for one Apply operation (TE-SAFE-06).

    A batch-level abort (backup/preflight failure) is surfaced distinctly via
    `backup_ok`/`preflight_ok`/`global_error_key` — it is NEVER faked as a
    per-file backup failure.
    """
    operation_id: str
    backup_path:  Optional[Path] = None
    journal_path: Optional[Path] = None
    backup_ok:    bool = True
    preflight_ok: bool = True
    global_error_key: str = ""
    global_error_detail: str = ""
    # Fresh executable preflight blockers are batch-scoped evidence.  Keeping
    # them structured lets the UI direct the user back to Review Changes
    # without manufacturing per-file write outcomes.
    blocked_items: dict[str, object] = field(default_factory=dict)
    outcomes:     list[ApplyOutcome] = field(default_factory=list)
    success_count: int = 0
    failed_count:  int = 0
    skipped_count: int = 0
    partial_count: int = 0
    cancelled_count: int = 0
    # Set when durable journalling failed mid-batch (or a rename rollback failed):
    # the batch stopped at a safe boundary and the on-disk state must be
    # reconciled via journal recovery (TE-SAFE-11 / defect 1/3).
    recovery_required: bool = False

    @property
    def aborted(self) -> bool:
        """True when nothing was written because backup/preflight failed."""
        return not (self.backup_ok and self.preflight_ok)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 — Durable Apply operation journal (TE-SAFE-11)
# ──────────────────────────────────────────────────────────────────────────────

class JournalFileState:
    """Per-file state machine, atomically persisted at each transition."""
    PLANNED   = "planned"
    BACKED_UP = "backed_up"
    WRITTEN   = "written"
    VERIFIED  = "verified"
    RENAMED   = "renamed"
    COMPLETE  = "complete"
    # terminal error states
    FAILED    = "failed"
    PARTIAL   = "partial"
    SKIPPED   = "skipped"
    CANCELLED = "cancelled"
    # a step (rename rollback / journal write) left this file needing manual
    # reconciliation; recovery must inspect the recorded path mapping.
    UNRESOLVED = "unresolved"


class JournalBatchState:
    """Durable batch stages; legacy values remain readable."""
    PREPARING = "preparing"
    PREFLIGHT_COMPLETE = "preflight_complete"
    BACKUP_STARTED = "backup_started"
    BACKUP_COMPLETE = "backup_complete"
    METADATA_WRITING = "metadata_writing"
    METADATA_VERIFIED = "metadata_verified"
    PHYSICAL_PREPARING = "physical_preparing"
    TEMP_RENAME_IN_PROGRESS = "temp_rename_in_progress"
    PHYSICAL_COMPLETE = "physical_complete"
    RECONCILIATION_PENDING = "reconciliation_pending"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    # Existing Phase-1 journal values are retained for compatibility.
    PLANNING = "planning"
    BACKING_UP = "backing_up"
    APPLYING = "applying"
    DONE = "done"


class RenameLedgerState:
    """
    Per-step state in the owner-aware rename ledger (blocker: journal identity).

    Each ledger entry belongs to a single owner (its original file path) and a
    single rename component, carries a durable sequence number, and records the
    exact src/dst of one rename step. This lets recovery reconstruct where each
    *physical* file went without applying global move edges to every record.
    """
    INTENT      = "intent"       # persisted before os.replace
    COMPLETED   = "completed"    # os.replace succeeded, durably recorded
    ROLLED_BACK = "rolled_back"  # a completed step was reversed
    UNRESOLVED  = "unresolved"   # rollback/persist failed — both paths possible
