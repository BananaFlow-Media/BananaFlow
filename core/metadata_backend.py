"""Canonical metadata capability, normalization, and write backend.

This module is deliberately Qt-free.  It is the one place that knows how a
canonical field maps to Mutagen's ID3, Vorbis, and MP4 representations.  The
Tag Editor continues to stage ``ProposedTags`` in memory; its Phase-1 atomic
writer calls this backend only on its temporary copy.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterable

from core.metadata_models import (
    ARTWORK_FIELD,
    ArtworkEntry,
    ArtworkDiagnostic,
    ArtworkReadState,
    ArtworkValue,
    CanonicalMetadata,
    ChangeAction,
    FieldChange,
    LyricsEntry,
    LyricsValue,
    MetadataDelta,
    OriginalTags,
    ProposedTags,
    ReplayGainValues,
    LYRICS_FIELD,
    MULTI_VALUE_FIELDS,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_FIELDS,
    REPLAYGAIN_REFERENCE_LOUDNESS,
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
    normalize_multi_value,
    parse_replaygain_number,
)


class CapabilityLevel(str, Enum):
    FULL = "full"
    LIMITED = "limited"
    READ_ONLY = "read_only"
    UNSUPPORTED = "unsupported"
    FUTURE = "future"


CORE_FIELDS = frozenset({"title", "artist", "album", "album_artist", "track_num", "track_total", "disc_num", "disc_total", "year", "genre", "comment"})
EXTENDED_FIELDS = frozenset({"composer", "publisher", "copyright", "bpm", "isrc", "grouping", "sort_title", "sort_artist", "sort_album", "sort_album_artist"})
AUXILIARY_FIELDS = frozenset({LYRICS_FIELD}) | REPLAYGAIN_FIELDS
ARTWORK_FORMATS = frozenset({"mp3", "wav", "flac", "m4a"})
ID3_FIELDS = CORE_FIELDS | EXTENDED_FIELDS | AUXILIARY_FIELDS

_REPLAYGAIN_TAGS = {
    REPLAYGAIN_TRACK_GAIN: "REPLAYGAIN_TRACK_GAIN",
    REPLAYGAIN_TRACK_PEAK: "REPLAYGAIN_TRACK_PEAK",
    REPLAYGAIN_ALBUM_GAIN: "REPLAYGAIN_ALBUM_GAIN",
    REPLAYGAIN_ALBUM_PEAK: "REPLAYGAIN_ALBUM_PEAK",
    REPLAYGAIN_REFERENCE_LOUDNESS: "REPLAYGAIN_REFERENCE_LOUDNESS",
}


@dataclass(frozen=True)
class FormatCapability:
    format_id: str
    extensions: frozenset[str]
    level: CapabilityLevel
    editable_fields: frozenset[str] = frozenset()
    message_key: str = "meta_format_supported"
    details: str = ""
    field_notes: tuple[tuple[str, str], ...] = ()

    @property
    def writable(self) -> bool:
        return self.level in {CapabilityLevel.FULL, CapabilityLevel.LIMITED} and bool(self.editable_fields)

    def supports_field(self, field_name: str) -> bool:
        return self.writable and field_name in self.editable_fields

    def note_for(self, field_name: str) -> str:
        return dict(self.field_notes).get(field_name, "")


@dataclass(frozen=True)
class FormatDetection:
    format_id: str
    capability: FormatCapability
    detected_by: str
    detail: str = ""


class FormatCapabilityRegistry:
    """The authoritative supported-format policy; extensions are not support."""
    def __init__(self) -> None:
        self._items = (
            FormatCapability("mp3", frozenset({".mp3"}), CapabilityLevel.FULL, ID3_FIELDS | {ARTWORK_FIELD}, details="ID3v2 APIC via Mutagen"),
            FormatCapability("flac", frozenset({".flac"}), CapabilityLevel.FULL, ID3_FIELDS | {ARTWORK_FIELD}, details="Vorbis comments and native FLAC pictures via Mutagen"),
            FormatCapability("m4a", frozenset({".m4a", ".mp4", ".m4b", ".alac"}), CapabilityLevel.FULL, ID3_FIELDS | {ARTWORK_FIELD}, details="MP4 atoms/freeform atoms and covr via Mutagen"),
            FormatCapability(
                "opus", frozenset({".opus"}), CapabilityLevel.FULL, ID3_FIELDS,
                details="Opus Vorbis comments via Mutagen",
                field_notes=tuple((field_name, "Standard ReplayGain comments; native R128_* tags are preserved without conversion") for field_name in REPLAYGAIN_FIELDS),
            ),
            # Mutagen writes an ID3 chunk in RIFF/WAVE.  It intentionally does
            # not claim RIFF INFO/BWF editing, so existing non-ID3 WAV metadata
            # remains untouched rather than being translated or discarded.
            FormatCapability("wav", frozenset({".wav"}), CapabilityLevel.LIMITED, ID3_FIELDS | {ARTWORK_FIELD}, "meta_format_wav_limited", "ID3-in-WAV APIC only; RIFF INFO/BWF is read-only"),
            FormatCapability("ogg_vorbis", frozenset({".ogg"}), CapabilityLevel.FULL, ID3_FIELDS, details="Vorbis comments via Mutagen"),
            FormatCapability("aac", frozenset({".aac"}), CapabilityLevel.READ_ONLY, message_key="meta_format_read_only", details="ADTS metadata writing is not safely supported"),
            FormatCapability("aiff", frozenset({".aif", ".aiff"}), CapabilityLevel.READ_ONLY, message_key="meta_format_read_only", details="Detected; no safe writer enabled"),
            FormatCapability("wma", frozenset({".wma", ".asf"}), CapabilityLevel.READ_ONLY, message_key="meta_format_read_only", details="ASF writer not enabled"),
            FormatCapability("ape", frozenset({".ape"}), CapabilityLevel.FUTURE, message_key="meta_format_future", details="APEv2 support is future work"),
            FormatCapability("mpc", frozenset({".mpc"}), CapabilityLevel.FUTURE, message_key="meta_format_future", details="Musepack support is future work"),
        )
        self._by_id = {item.format_id: item for item in self._items}

    def all(self) -> tuple[FormatCapability, ...]:
        return self._items

    def by_id(self, format_id: str) -> FormatCapability:
        return self._by_id.get(format_id, FormatCapability("unknown", frozenset(), CapabilityLevel.UNSUPPORTED, message_key="meta_unsupported_format_tooltip"))

    def by_extension(self, extension: str) -> FormatCapability:
        extension = extension.lower()
        return next((item for item in self._items if extension in item.extensions), self.by_id("unknown"))


FORMAT_CAPABILITIES = FormatCapabilityRegistry()


class MetadataBackend:
    def __init__(self, registry: FormatCapabilityRegistry = FORMAT_CAPABILITIES) -> None:
        self.registry = registry

    def detect(self, path: Path, *, validate: bool = True) -> FormatDetection:
        """Resolve the real container before considering a cautious fallback.

        Public callers keep ``validate=True`` so a familiar extension or file
        signature never grants write capability to a malformed container.
        Canonical read/write paths pass ``validate=False`` because their one
        format-specific Mutagen open immediately performs the same validation;
        avoiding a redundant open matters on Windows workers and changes no
        capability decision.
        """
        extension_capability = self.registry.by_extension(path.suffix)
        try:
            with path.open("rb") as stream:
                header = stream.read(64)
        except OSError as exc:
            return FormatDetection("unknown", self.registry.by_id("unknown"), "unreadable", str(exc))
        try:
            if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
                if validate:
                    from mutagen.wave import WAVE; WAVE(str(path))
                return FormatDetection("wav", self.registry.by_id("wav"), "container")
            if header.startswith(b"OggS"):
                if b"OpusHead" in header:
                    if validate:
                        from mutagen.oggopus import OggOpus; OggOpus(str(path))
                    return FormatDetection("opus", self.registry.by_id("opus"), "container")
                if b"\x01vorbis" in header:
                    if validate:
                        from mutagen.oggvorbis import OggVorbis; OggVorbis(str(path))
                    return FormatDetection("ogg_vorbis", self.registry.by_id("ogg_vorbis"), "container")
                return FormatDetection("unknown", self.registry.by_id("unknown"), "unparseable", "unrecognized Ogg codec")
            if header.startswith(b"fLaC"):
                if validate:
                    from mutagen.flac import FLAC; FLAC(str(path))
                return FormatDetection("flac", self.registry.by_id("flac"), "container")
            if len(header) >= 8 and header[4:8] == b"ftyp":
                if validate:
                    from mutagen.mp4 import MP4; MP4(str(path))
                return FormatDetection("m4a", self.registry.by_id("m4a"), "container")
            # A tag-only MP3 is a valid editor input.  Do not bless arbitrary
            # .mp3 bytes: ID3 must parse before it receives MP3 capability.
            if header.startswith(b"ID3"):
                if validate:
                    from mutagen.id3 import ID3; ID3(str(path))
                return FormatDetection("mp3", self.registry.by_id("mp3"), "container")
        except Exception as exc:
            return FormatDetection("unknown", self.registry.by_id("unknown"), "unparseable", str(exc))
        # An extension may describe a future/read-only format, but may never
        # grant an unparseable file full or limited write capability.
        if extension_capability.level in {CapabilityLevel.READ_ONLY, CapabilityLevel.FUTURE}:
            return FormatDetection(extension_capability.format_id, extension_capability, "extension_fallback")
        return FormatDetection("unknown", self.registry.by_id("unknown"), "unparseable", "no reliable container result")

    def read(self, path: Path) -> CanonicalMetadata:
        detection = self.detect(path, validate=False)
        if detection.format_id in {"mp3", "wav"}:
            values = self._read_id3(path, wav=detection.format_id == "wav")
        elif detection.format_id in {"flac", "opus", "ogg_vorbis"}:
            values = self._read_vorbis(path, detection.format_id)
        elif detection.format_id == "m4a":
            values = self._read_mp4(path)
        else:
            values = {}
        invalid_replaygain = values.pop("__invalid_replaygain__", None)
        props = {
            "filename": path.name,
            "path": str(path),
            "extension": path.suffix.lower(),
            "format_id": detection.format_id,
            "capability_level": detection.capability.level.value,
            "capability_details": detection.capability.details,
        }
        if invalid_replaygain:
            props["invalid_replaygain"] = dict(invalid_replaygain)
        try:
            stat = path.stat()
            props["size_bytes"] = stat.st_size
            props["modified_time"] = stat.st_mtime
        except OSError:
            pass
        artwork = self._read_artwork(path, detection.format_id)
        return CanonicalMetadata(values=values, artwork=artwork, file_properties=props)

    def read_legacy(self, path: Path) -> OriginalTags:
        canonical = self.read(path)
        values = canonical.values
        data: dict[str, object] = {}
        for name in OriginalTags.__dataclass_fields__:
            if name in {"lyrics", "replay_gain", "file_properties"}:
                continue
            value = values.get(name)
            if isinstance(value, tuple): value = "; ".join(value)
            data[name] = value if value is not None else (None if name in {"track_num", "track_total", "disc_num", "disc_total", "bpm"} else "")
        data["lyrics"] = LyricsValue.from_dict(values.get(LYRICS_FIELD))
        data["artwork"] = canonical.artwork
        data["artwork_captured"] = True
        data["replay_gain"] = ReplayGainValues.from_dict(
            {field_name: values.get(field_name) for field_name in REPLAYGAIN_FIELDS}
        )
        data["file_properties"] = dict(canonical.file_properties)
        return OriginalTags(**data)

    def proposal_delta(self, proposed: ProposedTags, original: OriginalTags) -> MetadataDelta:
        changes: dict[str, FieldChange] = {}
        effective = proposed.effective_tags(original)
        for name in proposed.changed_fields(original):
            value = effective.field_value(name)
            clear = (
                value is None
                or value == ""
                or (name == LYRICS_FIELD and not value.has_unsynchronized)
            )
            changes[name] = FieldChange(ChangeAction.CLEAR if clear else ChangeAction.SET, value)
        return MetadataDelta(changes)

    @staticmethod
    def values_equal(format_id: str, field_name: str, left, right) -> bool:
        """Semantic readback equality after container-specific normalization."""
        if field_name == ARTWORK_FIELD:
            left_value, right_value = ArtworkValue.from_dict(left), ArtworkValue.from_dict(right)
            if format_id == "m4a":
                # covr has no standardized type/description slots; verify the
                # exact image payload and format without rejecting that honest
                # container normalization.
                return tuple((e.content_hash, e.mime_type) for e in left_value.entries) == tuple(
                    (e.content_hash, e.mime_type) for e in right_value.entries
                )
            return left_value.semantically_equal(right_value)
        if field_name != LYRICS_FIELD:
            from core.metadata_models import metadata_values_equal
            return metadata_values_equal(field_name, left, right)
        left_value = LyricsValue.from_dict(left)
        right_value = LyricsValue.from_dict(right)
        if format_id in {"mp3", "wav"}:
            def entries(value):
                return tuple(
                    (entry.text, entry.language, entry.description, entry.synchronized)
                    for entry in value.entries
                )
        else:
            # Vorbis comments and the MP4 ©lyr atom preserve lyrics text and
            # variants, but have no language/descriptor slots.
            def entries(value):
                return tuple((entry.text, entry.synchronized) for entry in value.entries)
        return entries(left_value) == entries(right_value)

    def write_legacy(self, path: Path, effective: OriginalTags, changed: set[str], format_id: str | None = None) -> None:
        detection = self.detect(path, validate=False)
        if format_id is not None and format_id != detection.format_id:
            raise ValueError(f"Container is {detection.format_id}, not requested format {format_id}")
        format_id = detection.format_id
        capability = self.registry.by_id(format_id)
        unsupported = changed - capability.editable_fields
        if not capability.writable or unsupported:
            raise ValueError(f"Unsupported metadata fields for {format_id}: {', '.join(sorted(unsupported or changed))}")
        if format_id in {"mp3", "wav"}: self._write_id3(path, effective, changed, wav=format_id == "wav")
        elif format_id in {"flac", "opus", "ogg_vorbis"}: self._write_vorbis(path, effective, changed, format_id)
        elif format_id == "m4a": self._write_mp4(path, effective, changed)
        else: raise ValueError(f"Unsupported format for writing: {format_id}")

    @staticmethod
    def _read_artwork(path: Path, format_id: str) -> ArtworkValue:
        """Read actual container payloads; no filename or preview assumptions."""
        entries: list[ArtworkEntry] = []
        diagnostics: list[ArtworkDiagnostic] = []
        from core.artwork import ArtworkValidationError, validate_artwork_bytes
        def accept(data: bytes, mime: str, picture_type: int, description: str, source_id: str) -> None:
            try:
                entry = validate_artwork_bytes(data, description=description, picture_type=picture_type)
                # Actual bytes win over a container-declared MIME.  This also
                # rejects extension/MIME spoofing at the read boundary.
                entries.append(replace(entry, source_id=source_id))
            except ArtworkValidationError as exc:
                diagnostics.append(ArtworkDiagnostic(exc.key, source_id=source_id))
        try:
            if format_id in {"mp3", "wav"}:
                if format_id == "wav":
                    from mutagen.wave import WAVE
                    tags = WAVE(str(path)).tags
                else:
                    from mutagen.id3 import ID3
                    tags = ID3(str(path))
                for frame in tags.getall("APIC") if tags else ():
                    accept(frame.data, frame.mime, frame.type, frame.desc, "APIC")
            elif format_id == "flac":
                from mutagen.flac import FLAC
                for picture in FLAC(str(path)).pictures:
                    accept(picture.data, picture.mime, picture.type, picture.desc, "FLAC_PICTURE")
            elif format_id == "m4a":
                from mutagen.mp4 import MP4, MP4Cover
                for index, cover in enumerate((MP4(str(path)).tags or {}).get("covr", ())):
                    mime = "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
                    accept(bytes(cover), mime, 3, "", f"covr:{index}")
        except Exception:
            return ArtworkValue((), read_state=ArtworkReadState.READ_FAILED,
                                diagnostics=(ArtworkDiagnostic("artwork_read_failed"),))
        if diagnostics:
            state = ArtworkReadState.PARTIAL if entries else ArtworkReadState.INVALID
            return ArtworkValue(tuple(entries), read_state=state, diagnostics=tuple(diagnostics))
        return ArtworkValue(tuple(entries), read_state=ArtworkReadState.VALID if entries else ArtworkReadState.EMPTY)

    def write_auxiliary(self, path: Path, namespace: str, values: dict[str, str]) -> None:
        """Shared extension point for Lyrics/ReplayGain without a UI dependency.

        It intentionally accepts only known namespaces so a later feature
        cannot overwrite arbitrary user tags by accident.  Normal Tag Editor
        writes remain delta/atomic through ``write_legacy``.
        """
        if namespace not in {"lyrics", "replaygain"}:
            raise ValueError(f"Unknown metadata namespace: {namespace}")
        original = self.read_legacy(path)
        proposed = ProposedTags()
        if namespace == "lyrics":
            proposed.set_lyrics(
                values.get("lyrics", ""),
                original=original.lyrics,
                language=values.get("language") or None,
                description=values.get("description") or None,
            )
        else:
            reverse = {stored: canonical for canonical, stored in _REPLAYGAIN_TAGS.items()}
            for key, value in values.items():
                field_name = reverse.get(key.upper(), key.lower())
                proposed.set_replay_gain(field_name, value)
        effective = proposed.effective_tags(original)
        self.write_legacy(path, effective, proposed.changed_fields(original))

    @staticmethod
    def _text(tags, frame: str) -> str:
        value = tags.get(frame)
        return str(value.text[0]).strip() if value is not None and getattr(value, "text", None) else ""

    def _read_id3(self, path: Path, wav: bool = False) -> dict[str, object]:
        from mutagen.id3 import ID3, ID3NoHeaderError
        if wav:
            from mutagen.wave import WAVE
            tags = WAVE(str(path)).tags
            if tags is None: return {}
        else:
            try: tags = ID3(str(path))
            except ID3NoHeaderError: return {}
        result = {"title": self._text(tags,"TIT2"), "artist": tuple(str(x) for x in getattr(tags.get("TPE1"), "text", []) if str(x)), "album": self._text(tags,"TALB"), "album_artist": tuple(str(x) for x in getattr(tags.get("TPE2"), "text", []) if str(x)), "year": self._text(tags,"TDRC"), "genre": tuple(str(x) for x in getattr(tags.get("TCON"), "text", []) if str(x)), "composer": self._text(tags,"TCOM"), "publisher": self._text(tags,"TPUB"), "copyright": self._text(tags,"TCOP"), "isrc": self._text(tags,"TSRC"), "grouping": self._text(tags,"TIT1"), "sort_title": self._text(tags,"TSOT"), "sort_artist": self._text(tags,"TSOP"), "sort_album": self._text(tags,"TSOA"), "sort_album_artist": self._text(tags,"TSO2")}
        for key, num, total in (("TRCK", "track_num", "track_total"), ("TPOS", "disc_num", "disc_total")):
            parts = self._text(tags, key).split("/")
            for part, target in zip(parts[:2], (num, total)):
                try: result[target] = int(part)
                except ValueError: pass
        try: result["bpm"] = int(self._text(tags, "TBPM"))
        except ValueError: pass
        for key in tags.keys():
            if key.startswith("COMM") and getattr(tags[key], "desc", "") == "":
                result["comment"] = str(tags[key].text[0]).strip(); break
        lyric_entries = [
            LyricsEntry(
                text=getattr(frame, "text", ""),
                language=getattr(frame, "lang", "und"),
                description=getattr(frame, "desc", ""),
                source="USLT",
            )
            for frame in tags.getall("USLT")
        ]
        lyric_entries.extend(
            LyricsEntry(
                language=getattr(frame, "lang", "und"),
                description=getattr(frame, "desc", ""),
                synchronized=True,
                source="SYLT",
            )
            for frame in tags.getall("SYLT")
        )
        if lyric_entries:
            result[LYRICS_FIELD] = LyricsValue(tuple(lyric_entries))
        for frame in tags.getall("TXXX"):
            field_name = next(
                (canonical for canonical, stored in _REPLAYGAIN_TAGS.items()
                 if str(getattr(frame, "desc", "")).casefold() == stored.casefold()),
                None,
            )
            text_values = getattr(frame, "text", ())
            if field_name and text_values:
                value = parse_replaygain_number(text_values[0], peak="peak" in field_name)
                if value is not None:
                    result[field_name] = value
                else:
                    result.setdefault("__invalid_replaygain__", {})[field_name] = str(text_values[0])
        return {key: value for key, value in result.items() if value not in ("", ())}

    def _read_vorbis(self, path: Path, format_id: str) -> dict[str, object]:
        from mutagen.flac import FLAC
        if format_id == "flac": audio = FLAC(str(path))
        elif format_id == "opus":
            from mutagen.oggopus import OggOpus; audio = OggOpus(str(path))
        else:
            from mutagen.oggvorbis import OggVorbis; audio = OggVorbis(str(path))
        mapping = {"title":"title", "artist":"artist", "album":"album", "album_artist":"albumartist", "year":"date", "genre":"genre", "comment":"comment", "composer":"composer", "publisher":"publisher", "copyright":"copyright", "bpm":"bpm", "isrc":"isrc", "grouping":"grouping", "sort_title":"titlesort", "sort_artist":"artistsort", "sort_album":"albumsort", "sort_album_artist":"albumartistsort"}
        result: dict[str, object] = {}
        for name, key in mapping.items():
            values = tuple(str(x).strip() for x in audio.get(key, []) if str(x).strip())
            if values: result[name] = values if name in MULTI_VALUE_FIELDS else values[0]
        for key, num, total in (("tracknumber", "track_num", "track_total"), ("discnumber", "disc_num", "disc_total")):
            values = audio.get(key, [])
            raw = str(values[0]) if values else ""
            parts = raw.split("/")
            for part, target in zip(parts[:2], (num, total)):
                try: result[target] = int(part)
                except ValueError: pass
        for key, target in (("totaltracks", "track_total"), ("tracktotal", "track_total"), ("totaldiscs", "disc_total"), ("disctotal", "disc_total")):
            if target not in result and audio.get(key):
                try: result[target] = int(audio[key][0])
                except ValueError: pass
        if "bpm" in result:
            try: result["bpm"] = int(str(result["bpm"]))
            except ValueError: result.pop("bpm")
        lyric_entries: list[LyricsEntry] = []
        for source in ("lyrics", "unsyncedlyrics"):
            for value in audio.get(source, []):
                lyric_entries.append(LyricsEntry(text=value, source=source.upper()))
        if lyric_entries:
            result[LYRICS_FIELD] = LyricsValue(tuple(lyric_entries))
        for field_name, stored in _REPLAYGAIN_TAGS.items():
            values = audio.get(stored, [])
            if values:
                value = parse_replaygain_number(values[0], peak="peak" in field_name)
                if value is not None:
                    result[field_name] = value
                else:
                    result.setdefault("__invalid_replaygain__", {})[field_name] = str(values[0])
        return result

    def _read_mp4(self, path: Path) -> dict[str, object]:
        from mutagen.mp4 import MP4
        tags = MP4(str(path)).tags or {}
        def text(key: str):
            values = tuple(str(value).strip() for value in tags.get(key, []) if str(value).strip())
            return values
        mapping = {"title":"\xa9nam", "artist":"\xa9ART", "album":"\xa9alb", "album_artist":"aART", "year":"\xa9day", "genre":"\xa9gen", "comment":"\xa9cmt", "composer":"\xa9wrt", "copyright":"cprt", "bpm":"tmpo", "grouping":"\xa9grp", "sort_title":"sonm", "sort_artist":"soar", "sort_album":"soal", "sort_album_artist":"soaa"}
        result = {
            name: (values if name in MULTI_VALUE_FIELDS else values[0])
            for name, key in mapping.items() if (values := text(key))
        }
        for key, num, total in (("trkn", "track_num", "track_total"), ("disk", "disc_num", "disc_total")):
            values = tags.get(key, [])
            if values:
                pair = values[0]
                if len(pair) > 0 and pair[0]: result[num] = int(pair[0])
                if len(pair) > 1 and pair[1]: result[total] = int(pair[1])
        for name, key in (("publisher", "LABEL"), ("isrc", "ISRC")):
            values = tags.get(f"----:com.apple.iTunes:{key}", [])
            if values: result[name] = bytes(values[0]).decode(errors="replace").strip()
        if "bpm" in result:
            try: result["bpm"] = int(result["bpm"])
            except ValueError: result.pop("bpm")
        lyrics = tuple(
            LyricsEntry(text=value, source="\xa9lyr")
            for value in tags.get("\xa9lyr", []) if str(value) != ""
        )
        if lyrics:
            result[LYRICS_FIELD] = LyricsValue(lyrics)
        for field_name, stored in _REPLAYGAIN_TAGS.items():
            values = tags.get(f"----:com.apple.iTunes:{stored}", [])
            if values:
                try:
                    raw = bytes(values[0]).decode(errors="replace")
                except Exception:
                    raw = str(values[0])
                value = parse_replaygain_number(raw, peak="peak" in field_name)
                if value is not None:
                    result[field_name] = value
                else:
                    result.setdefault("__invalid_replaygain__", {})[field_name] = raw
        return result

    def _write_id3(self, path: Path, tags: OriginalTags, changed: set[str], wav: bool = False) -> None:
        from mutagen.id3 import ID3, ID3NoHeaderError, Encoding, TXXX, USLT, APIC
        from mutagen.id3 import TIT2,TPE1,TALB,TPE2,TRCK,TPOS,COMM,TDRC,TCON,TCOM,TPUB,TCOP,TBPM,TSRC,TIT1,TSOT,TSOP,TSOA,TSO2
        if wav:
            from mutagen.wave import WAVE
            audio = WAVE(str(path))
            if audio.tags is None: audio.add_tags()
            id3 = audio.tags
        else:
            try: id3 = ID3(str(path))
            except ID3NoHeaderError: id3 = ID3()
        mapping = {"title":("TIT2",TIT2),"artist":("TPE1",TPE1),"album":("TALB",TALB),"album_artist":("TPE2",TPE2),"year":("TDRC",TDRC),"genre":("TCON",TCON),"composer":("TCOM",TCOM),"publisher":("TPUB",TPUB),"copyright":("TCOP",TCOP),"bpm":("TBPM",TBPM),"isrc":("TSRC",TSRC),"grouping":("TIT1",TIT1),"sort_title":("TSOT",TSOT),"sort_artist":("TSOP",TSOP),"sort_album":("TSOA",TSOA),"sort_album_artist":("TSO2",TSO2)}
        for name, (frame, cls) in mapping.items():
            if name in changed:
                id3.delall(frame); value = getattr(tags, name)
                if value not in (None, ""): id3.add(cls(encoding=Encoding.UTF8, text=str(value)))
        for name, frame, cls, total in (("track_num","TRCK",TRCK,"track_total"),("disc_num","TPOS",TPOS,"disc_total")):
            if name in changed or total in changed:
                id3.delall(frame); value = getattr(tags,name); maximum = getattr(tags,total)
                if value is not None: id3.add(cls(encoding=Encoding.UTF8, text=f"{value}/{maximum}" if maximum else str(value)))
        if "comment" in changed:
            for key in [key for key in id3.keys() if key.startswith("COMM") and getattr(id3[key], "desc", "") == ""]: del id3[key]
            if tags.comment: id3.add(COMM(encoding=Encoding.UTF8, lang="xxx", desc="", text=tags.comment))
        if LYRICS_FIELD in changed:
            # USLT is the Phase 6 editable contract. SYLT frames are deliberately
            # left untouched because timed lyrics are display-only.
            id3.delall("USLT")
            for entry in tags.lyrics.entries:
                if entry.synchronized:
                    continue
                language = (entry.language or "und")[:3].ljust(3, "x")
                id3.add(USLT(
                    encoding=Encoding.UTF8,
                    lang=language,
                    desc=entry.description,
                    text=entry.text,
                ))
        for field_name in changed & REPLAYGAIN_FIELDS:
            stored = _REPLAYGAIN_TAGS[field_name]
            for key in list(id3.keys()):
                frame = id3[key]
                if key.startswith("TXXX") and str(getattr(frame, "desc", "")).casefold() == stored.casefold():
                    del id3[key]
            value = tags.replay_gain.field_value(field_name)
            if value is not None:
                id3.add(TXXX(
                    encoding=Encoding.UTF8,
                    desc=stored,
                    text=self._format_replaygain(field_name, value),
                ))
        if ARTWORK_FIELD in changed:
            id3.delall("APIC")
            for entry in tags.artwork.entries:
                id3.add(APIC(encoding=Encoding.UTF8, mime=entry.mime_type,
                    type=entry.picture_type, desc=entry.description, data=entry.data))
        if wav: audio.save()
        else: id3.save(str(path))

    def _write_vorbis(self, path: Path, tags: OriginalTags, changed: set[str], format_id: str) -> None:
        from mutagen.flac import FLAC
        if format_id == "flac": audio = FLAC(str(path))
        elif format_id == "opus":
            from mutagen.oggopus import OggOpus; audio = OggOpus(str(path))
        else:
            from mutagen.oggvorbis import OggVorbis; audio = OggVorbis(str(path))
        mapping = {"title":"title", "artist":"artist", "album":"album", "album_artist":"albumartist", "year":"date", "genre":"genre", "comment":"comment", "composer":"composer", "publisher":"publisher", "copyright":"copyright", "bpm":"bpm", "isrc":"isrc", "grouping":"grouping", "sort_title":"titlesort", "sort_artist":"artistsort", "sort_album":"albumsort", "sort_album_artist":"albumartistsort"}
        for name,key in mapping.items():
            if name in changed:
                value = getattr(tags,name)
                if value in (None, ""): audio.pop(key, None)
                else: audio[key] = list(normalize_multi_value(value)) if name in MULTI_VALUE_FIELDS else str(value)
        for name,key,total,total_key in (("track_num","tracknumber","track_total","totaltracks"),("disc_num","discnumber","disc_total","totaldiscs")):
            if name in changed or total in changed:
                value, maximum = getattr(tags,name), getattr(tags,total)
                if value is None: audio.pop(key,None)
                else: audio[key] = str(value)
                if maximum is None: audio.pop(total_key,None)
                else: audio[total_key] = str(maximum)
        if LYRICS_FIELD in changed:
            audio.pop("lyrics", None)
            audio.pop("unsyncedlyrics", None)
            grouped: dict[str, list[str]] = {}
            for entry in tags.lyrics.entries:
                if entry.synchronized:
                    continue
                source = entry.source.casefold() if entry.source.casefold() in {"lyrics", "unsyncedlyrics"} else "lyrics"
                grouped.setdefault(source, []).append(entry.text)
            for source, values in grouped.items():
                audio[source] = values
        for field_name in changed & REPLAYGAIN_FIELDS:
            stored = _REPLAYGAIN_TAGS[field_name]
            value = tags.replay_gain.field_value(field_name)
            if value is None:
                audio.pop(stored, None)
            else:
                audio[stored] = self._format_replaygain(field_name, value)
        if ARTWORK_FIELD in changed:
            if format_id != "flac":
                raise ValueError("Artwork writes are not enabled for this Ogg container")
            from mutagen.flac import Picture
            audio.clear_pictures()
            for entry in tags.artwork.entries:
                picture = Picture()
                picture.data, picture.mime = entry.data, entry.mime_type
                picture.type, picture.desc = entry.picture_type, entry.description
                picture.width, picture.height = entry.width, entry.height
                picture.depth, picture.colors = entry.depth, entry.colors
                audio.add_picture(picture)
        audio.save()

    def _write_mp4(self, path: Path, tags: OriginalTags, changed: set[str]) -> None:
        from mutagen.mp4 import MP4, MP4FreeForm, MP4Cover
        audio = MP4(str(path));
        if audio.tags is None: audio.add_tags()
        data = audio.tags
        mapping = {"title":"\xa9nam", "artist":"\xa9ART", "album":"\xa9alb", "album_artist":"aART", "year":"\xa9day", "genre":"\xa9gen", "comment":"\xa9cmt", "composer":"\xa9wrt", "copyright":"cprt", "bpm":"tmpo", "grouping":"\xa9grp", "sort_title":"sonm", "sort_artist":"soar", "sort_album":"soal", "sort_album_artist":"soaa"}
        for name,key in mapping.items():
            if name in changed:
                value=getattr(tags,name)
                if value in (None, ""): data.pop(key,None)
                elif name in MULTI_VALUE_FIELDS: data[key] = list(normalize_multi_value(value))
                else: data[key]=[int(value) if name == "bpm" else str(value)]
        for name,key,total in (("track_num","trkn","track_total"),("disc_num","disk","disc_total")):
            if name in changed or total in changed:
                # ``effective`` already carries unchanged values.  Retain
                # both components even when the user changed only one.
                value, maximum = getattr(tags,name), getattr(tags,total)
                if value is None and maximum is None: data.pop(key,None)
                else: data[key]=[(value or 0, maximum or 0)]
        for name,key in (("publisher","LABEL"),("isrc","ISRC")):
            if name in changed:
                atom=f"----:com.apple.iTunes:{key}"; value=getattr(tags,name)
                if not value: data.pop(atom,None)
                else: data[atom]=[MP4FreeForm(str(value).encode())]
        if LYRICS_FIELD in changed:
            values = [entry.text for entry in tags.lyrics.entries if not entry.synchronized]
            if values:
                data["\xa9lyr"] = values
            else:
                data.pop("\xa9lyr", None)
        for field_name in changed & REPLAYGAIN_FIELDS:
            atom = f"----:com.apple.iTunes:{_REPLAYGAIN_TAGS[field_name]}"
            value = tags.replay_gain.field_value(field_name)
            if value is None:
                data.pop(atom, None)
            else:
                data[atom] = [MP4FreeForm(self._format_replaygain(field_name, value).encode("utf-8"))]
        if ARTWORK_FIELD in changed:
            covers = []
            for entry in tags.artwork.entries:
                if entry.mime_type == "image/png":
                    imageformat = MP4Cover.FORMAT_PNG
                elif entry.mime_type in {"image/jpeg", "image/jpg"}:
                    imageformat = MP4Cover.FORMAT_JPEG
                else:
                    raise ValueError("MP4 artwork must be JPEG or PNG")
                covers.append(MP4Cover(entry.data, imageformat=imageformat))
            if covers:
                data["covr"] = covers
            else:
                data.pop("covr", None)
        audio.save()

    @staticmethod
    def _format_replaygain(field_name: str, value: float) -> str:
        if field_name in {REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_ALBUM_GAIN}:
            return f"{float(value):+.2f} dB"
        if field_name == REPLAYGAIN_REFERENCE_LOUDNESS:
            return f"{float(value):.1f} dB"
        return f"{float(value):.8f}".rstrip("0").rstrip(".")


METADATA_BACKEND = MetadataBackend()
