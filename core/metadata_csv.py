"""Canonical, bounded metadata CSV export/import for the Tag Editor.

The module is Qt-free.  Parsing and dry-run are immutable and proposal-free;
``accept_import_preview`` is the one explicit bridge to the existing Change Set.
It never writes media or changes paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import codecs
import csv
import io
import json
from pathlib import Path, PurePath
import re
from typing import Iterable, Mapping

from core.change_sets import ChangeOrigin
from core.metadata_backend import FORMAT_CAPABILITIES
from core.metadata_io import (
    CancellationToken, IOErrorInfo, IOErrorKind, IORequestIdentity,
    IOScope, MetadataIOError, MetadataValueSource, SourceFileIdentity,
    atomic_write_bytes,
)
from core.metadata_models import (
    ARTWORK_FIELD, LYRICS_FIELD, MULTI_VALUE_FIELDS, REPLAYGAIN_FIELDS,
    LyricsValue, MetadataField, parse_replaygain_number,
)


CSV_SCHEMA = "bananaflow.metadata.csv.v1"
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 256
MAX_CELL_CHARS = 1024 * 1024
SNIFF_BYTES = 64 * 1024

#: Explicit, versioned declaration of whether a file's cells were passed
#: through :func:`formula_safe_value`.  A cell is only unescaped on import
#: when the file itself declares ``spreadsheet-safe-v1`` -- an apostrophe
#: at the start of a cell in an arbitrary third-party CSV is data, not an
#: escape, and must never be silently stripped.
CELL_ENCODING_COLUMN = "bananaflow_cell_encoding"


class CellEncoding(str, Enum):
    SPREADSHEET_SAFE_V1 = "spreadsheet-safe-v1"
    NONE = "none"


IDENTITY_COLUMNS = (
    "bananaflow_schema", "bananaflow_cell_encoding", "value_source", "relative_path",
    "filename", "extension", "format_id", "size_bytes", "modified_time_ns",
)
ARTWORK_COLUMNS = (
    "artwork_count", "artwork_primary_mime", "artwork_primary_width",
    "artwork_primary_height", "artwork_primary_hash", "artwork_read_state",
)
CANONICAL_FIELDS = tuple(dict.fromkeys(field.value for field in MetadataField if field.value != ARTWORK_FIELD))
ROUNDTRIP_HEADERS = IDENTITY_COLUMNS + CANONICAL_FIELDS + ARTWORK_COLUMNS


class CsvEncoding(str, Enum):
    UTF8_BOM = "utf_8_bom"
    UTF8 = "utf_8"
    UTF16_LE = "utf_16_le"
    UTF16_BE = "utf_16_be"
    WINDOWS_1255 = "windows_1255"
    WINDOWS_1252 = "windows_1252"

    @property
    def codec(self) -> str:
        return {
            CsvEncoding.UTF8_BOM: "utf-8-sig",
            CsvEncoding.UTF8: "utf-8",
            CsvEncoding.UTF16_LE: "utf-16-le",
            CsvEncoding.UTF16_BE: "utf-16-be",
            CsvEncoding.WINDOWS_1255: "cp1255",
            CsvEncoding.WINDOWS_1252: "cp1252",
        }[self]


class CsvDelimiter(str, Enum):
    COMMA = ","
    SEMICOLON = ";"
    TAB = "\t"


@dataclass(frozen=True)
class CsvEncodingSpec:
    encoding: CsvEncoding = CsvEncoding.UTF8_BOM


@dataclass(frozen=True)
class CsvDialectSpec:
    delimiter: CsvDelimiter = CsvDelimiter.COMMA


@dataclass(frozen=True)
class MetadataExportRow:
    item_id: int
    values: tuple[tuple[str, object], ...]

    def mapping(self) -> dict[str, object]:
        return dict(self.values)


@dataclass(frozen=True)
class MetadataExportPlan:
    identity: IORequestIdentity
    root: Path
    scope: IOScope
    value_source: MetadataValueSource
    fields: tuple[str, ...]
    rows: tuple[MetadataExportRow, ...]
    encoding: CsvEncodingSpec = CsvEncodingSpec()
    dialect: CsvDialectSpec = CsvDialectSpec()
    include_absolute_paths: bool = False
    #: Safe by default: an ordinary user opens an exported CSV in Excel, and the
    #: tag values in it came from files the application did not author.
    spreadsheet_safe: bool = True

    @property
    def headers(self) -> tuple[str, ...]:
        headers = list(IDENTITY_COLUMNS)
        if self.include_absolute_paths:
            headers.append("absolute_path")
        headers.extend(field_name for field_name in self.fields if field_name in CANONICAL_FIELDS)
        headers.extend(ARTWORK_COLUMNS)
        return tuple(headers)


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _cell_value(field_name: str, value: object) -> object:
    if field_name in MULTI_VALUE_FIELDS:
        if isinstance(value, str):
            values = tuple(part.strip() for part in value.split(";") if part.strip())
        else:
            values = tuple(value or ())
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if field_name == LYRICS_FIELD:
        lyrics = LyricsValue.from_dict(value)
        return json.dumps(lyrics.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return value


def build_metadata_export_plan(workspace, *, root: Path, item_ids: Iterable[int],
                               scope: IOScope, value_source: MetadataValueSource = MetadataValueSource.EFFECTIVE,
                               fields: Iterable[str] = CANONICAL_FIELDS,
                               encoding: CsvEncodingSpec = CsvEncodingSpec(),
                               dialect: CsvDialectSpec = CsvDialectSpec(),
                               include_absolute_paths: bool = False,
                               spreadsheet_safe: bool = True) -> MetadataExportPlan:
    ids = tuple(dict.fromkeys(int(value) for value in item_ids))
    selected_fields = tuple(dict.fromkeys(str(value) for value in fields if str(value) in CANONICAL_FIELDS))
    rows: list[MetadataExportRow] = []
    for identity in ids:
        item = workspace.track_for_id(identity)
        if item is None:
            continue
        tags = item.original if value_source is MetadataValueSource.ORIGINAL else item.proposed.effective_tags(item.original)
        baseline = item.baseline_identity
        artwork = tags.artwork
        primary = artwork.primary
        values: dict[str, object] = {
            "bananaflow_schema": CSV_SCHEMA,
            "bananaflow_cell_encoding": (CellEncoding.SPREADSHEET_SAFE_V1.value
                                     if spreadsheet_safe else CellEncoding.NONE.value),
            "value_source": value_source.value,
            "relative_path": _relative_path(item.path, root),
            "filename": item.path.name,
            "extension": item.path.suffix.lower(),
            "format_id": item.format_id or item.ext.lstrip(".").lower(),
            "size_bytes": getattr(baseline, "size", tags.file_properties.get("size_bytes", "")),
            "modified_time_ns": getattr(baseline, "mtime_ns", ""),
            "artwork_count": len(artwork.entries),
            "artwork_primary_mime": primary.mime_type if primary else "",
            "artwork_primary_width": primary.width if primary else "",
            "artwork_primary_height": primary.height if primary else "",
            "artwork_primary_hash": primary.content_hash if primary else "",
            "artwork_read_state": artwork.read_state.value,
        }
        if include_absolute_paths:
            values["absolute_path"] = str(item.path)
        for field_name in selected_fields:
            values[field_name] = _cell_value(field_name, tags.field_value(field_name))
        rows.append(MetadataExportRow(identity, tuple(values.items())))
    request = IORequestIdentity.create("metadata_csv_export", workspace.generation,
                                       workspace.change_set.revision, tuple(row.item_id for row in rows),
                                       content_revision=workspace.content_revision)
    return MetadataExportPlan(request, root.resolve(), scope, value_source, selected_fields,
                              tuple(rows), encoding, dialect, include_absolute_paths,
                              bool(spreadsheet_safe))


#: Characters that make a spreadsheet treat a cell as a formula rather than
#: text.  Tag values come from arbitrary media files, so they are untrusted.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def formula_safe_value(value: str) -> str:
    """Neutralize a spreadsheet formula lead, exactly reversibly.

    A downloaded file whose title is ``=cmd|'/c calc'!A1`` must not execute
    when the exported CSV is opened in Excel.  The escape is a single leading
    apostrophe applied whenever the value starts with a formula lead *or*
    already starts with an apostrophe itself -- escaping every leading
    apostrophe unconditionally, rather than only one already followed by a
    formula lead, is what makes the encoding bijective for every possible
    string, including one with several leading apostrophes:
    ``=cmd`` -> ``'=cmd``; ``'=cmd`` -> ``''=cmd``; ``''=cmd`` -> ``'''=cmd``.
    :func:`formula_unsafe_value` always undoes exactly this by stripping one
    leading apostrophe, so the CSV round trip stays lossless.
    """
    head = value[:1]
    if head in _FORMULA_LEAD or head == "'":
        return "'" + value
    return value


def formula_unsafe_value(value: str) -> str:
    """Undo exactly the escaping :func:`formula_safe_value` applies."""
    if value[:1] == "'":
        return value[1:]
    return value


def render_metadata_csv(plan: MetadataExportPlan,
                        cancellation: CancellationToken | None = None) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=plan.dialect.delimiter.value,
                        quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(plan.headers)
    escape = formula_safe_value if plan.spreadsheet_safe else (lambda value: value)
    for row in plan.rows:
        if cancellation:
            cancellation.raise_if_cancelled()
        values = row.mapping()
        writer.writerow([escape(str(values.get(header, ""))) for header in plan.headers])
    text = stream.getvalue()
    try:
        if plan.encoding.encoding is CsvEncoding.UTF8_BOM:
            return codecs.BOM_UTF8 + text.encode("utf-8", errors="strict")
        if plan.encoding.encoding is CsvEncoding.UTF16_LE:
            return codecs.BOM_UTF16_LE + text.encode("utf-16-le", errors="strict")
        if plan.encoding.encoding is CsvEncoding.UTF16_BE:
            return codecs.BOM_UTF16_BE + text.encode("utf-16-be", errors="strict")
        return text.encode(plan.encoding.encoding.codec, errors="strict")
    except UnicodeError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_ENCODING)) from exc


def export_metadata_csv(plan: MetadataExportPlan, destination: Path, *, overwrite: bool = False,
                        cancellation: CancellationToken | None = None):
    data = render_metadata_csv(plan, cancellation)

    def validate(path: Path) -> bool:
        parsed = parse_csv_file(path, encoding=plan.encoding.encoding,
                                delimiter=plan.dialect.delimiter)
        return parsed.headers == plan.headers and len(parsed.rows) == len(plan.rows)

    return atomic_write_bytes(destination, data, overwrite=overwrite,
                              validator=validate, cancellation=cancellation)


@dataclass(frozen=True)
class ParsedCsv:
    source: SourceFileIdentity
    encoding: CsvEncoding
    delimiter: CsvDelimiter
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def detect_encoding(data: bytes,
                    cancellation: CancellationToken | None = None) -> CsvEncoding:
    if data.startswith(codecs.BOM_UTF8):
        return CsvEncoding.UTF8_BOM
    if data.startswith(codecs.BOM_UTF16_LE):
        return CsvEncoding.UTF16_LE
    if data.startswith(codecs.BOM_UTF16_BE):
        return CsvEncoding.UTF16_BE
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        for offset in range(0, len(data), 64 * 1024):
            if cancellation:
                cancellation.raise_if_cancelled()
            decoder.decode(data[offset:offset + 64 * 1024], final=False)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_ENCODING,
                                          arguments=(("byte", exc.start),))) from exc
    return CsvEncoding.UTF8


def detect_delimiter(text: str) -> CsvDelimiter:
    sample = text[:SNIFF_BYTES]
    try:
        proposed = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        proposed = ","
    return CsvDelimiter(proposed)


def _read_source_bytes(path: Path, cancellation: CancellationToken | None) -> bytes:
    data = bytearray()
    try:
        with path.open("rb") as stream:
            while True:
                if cancellation:
                    cancellation.raise_if_cancelled()
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
    except OSError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.SOURCE_MISSING)) from exc
    if cancellation:
        cancellation.raise_if_cancelled()
    return bytes(data)


def _decode_payload(payload: bytes, encoding: CsvEncoding,
                    cancellation: CancellationToken | None) -> str:
    decoder = codecs.getincrementaldecoder(encoding.codec)(errors="strict")
    pieces: list[str] = []
    try:
        for offset in range(0, len(payload), 64 * 1024):
            if cancellation:
                cancellation.raise_if_cancelled()
            pieces.append(decoder.decode(payload[offset:offset + 64 * 1024], final=False))
        pieces.append(decoder.decode(b"", final=True))
    except UnicodeDecodeError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_ENCODING,
                                          arguments=(("byte", exc.start),))) from exc
    if cancellation:
        cancellation.raise_if_cancelled()
    return "".join(pieces)


def parse_csv_file(path: Path, *, encoding: CsvEncoding | None = None,
                   delimiter: CsvDelimiter | None = None,
                   cancellation: CancellationToken | None = None) -> ParsedCsv:
    if cancellation:
        cancellation.raise_if_cancelled()
    identity = SourceFileIdentity.capture(
        Path(path), maximum_bytes=MAX_FILE_BYTES, cancellation=cancellation)
    data = _read_source_bytes(identity.path, cancellation)
    after_read = SourceFileIdentity.capture(
        identity.path, maximum_bytes=MAX_FILE_BYTES, cancellation=cancellation)
    if after_read != identity:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.SOURCE_CHANGED))
    chosen_encoding = encoding or detect_encoding(data, cancellation)
    payload = data
    if chosen_encoding is CsvEncoding.UTF8_BOM and payload.startswith(codecs.BOM_UTF8):
        payload = payload[len(codecs.BOM_UTF8):]
    elif chosen_encoding is CsvEncoding.UTF16_LE and payload.startswith(codecs.BOM_UTF16_LE):
        payload = payload[len(codecs.BOM_UTF16_LE):]
    elif chosen_encoding is CsvEncoding.UTF16_BE and payload.startswith(codecs.BOM_UTF16_BE):
        payload = payload[len(codecs.BOM_UTF16_BE):]
    text = _decode_payload(payload, chosen_encoding, cancellation)
    if cancellation:
        cancellation.raise_if_cancelled()
    chosen_delimiter = delimiter or detect_delimiter(text)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=chosen_delimiter.value,
                            strict=True)
        if cancellation:
            cancellation.raise_if_cancelled()
        header = next(reader)
        if not header or len(header) > MAX_COLUMNS:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.RESOURCE_LIMIT))
        if len(set(header)) != len(header):
            raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT))
        rows: list[tuple[str, ...]] = []
        for line_number, row in enumerate(reader, 2):
            if cancellation:
                cancellation.raise_if_cancelled()
            if len(rows) >= MAX_ROWS or len(row) > MAX_COLUMNS:
                raise MetadataIOError(IOErrorInfo(IOErrorKind.RESOURCE_LIMIT, line=line_number))
            if any(len(cell) > MAX_CELL_CHARS for cell in row):
                raise MetadataIOError(IOErrorInfo(IOErrorKind.RESOURCE_LIMIT, line=line_number))
            padded = list(row) + [""] * (len(header) - len(row))
            rows.append(tuple(padded))
        # Undo the spreadsheet-safe escape, but only when the file itself
        # explicitly declares it was applied.  An apostrophe at the start of
        # a cell in an arbitrary third-party CSV -- or in a BananaFlow export
        # from before this column existed -- is data, not an escape, and
        # must never be guessed away.
        if _resolve_cell_encoding(tuple(header), rows) is CellEncoding.SPREADSHEET_SAFE_V1:
            rows = [tuple(formula_unsafe_value(cell) for cell in row) for row in rows]
    except MetadataIOError:
        raise
    except (csv.Error, StopIteration) as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT)) from exc
    return ParsedCsv(identity, chosen_encoding, chosen_delimiter, tuple(header), tuple(rows))


def _resolve_cell_encoding(headers: tuple[str, ...],
                           rows: list[tuple[str, ...]]) -> CellEncoding:
    """Determine the one, file-wide cell encoding a parsed CSV declares.

    Returns :attr:`CellEncoding.NONE` when the column is absent or blank in
    every row (an older export, or a third-party file).  A malformed or
    internally contradictory marker is a structured import error, never a
    guess.
    """
    if CELL_ENCODING_COLUMN not in headers:
        return CellEncoding.NONE
    index = headers.index(CELL_ENCODING_COLUMN)
    values = {row[index] for row in rows if index < len(row) and row[index]}
    if not values:
        return CellEncoding.NONE
    if len(values) > 1:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT))
    try:
        return CellEncoding(next(iter(values)))
    except ValueError:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT)) from None


class CsvIdentityRole(str, Enum):
    RELATIVE_PATH = "relative_path"
    ABSOLUTE_PATH = "absolute_path"
    FILENAME = "filename"


@dataclass(frozen=True)
class CsvColumnMapping:
    source_column: str
    target_field: str = ""
    identity_role: CsvIdentityRole | None = None
    ignored: bool = False


@dataclass(frozen=True)
class CsvIdentityMapping:
    column: str
    role: CsvIdentityRole


class BlankValuePolicy(str, Enum):
    NO_CHANGE = "no_change"
    CLEAR = "clear"


class ImportResultState(str, Enum):
    MATCHED = "matched"
    CHANGE = "change"
    NO_OP = "no_op"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    DUPLICATE_TARGET = "duplicate_target"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    READ_ONLY = "read_only"
    STALE_IDENTITY = "stale_identity"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ImportCellChange:
    id: str
    row_number: int
    item_id: int | None
    source_column: str
    field: str
    original_value: object
    effective_value: object
    imported_value: object
    operation: str
    state: ImportResultState
    capability: str = ""
    diagnostic: str = ""

    @property
    def selectable(self) -> bool:
        return self.state is ImportResultState.CHANGE


@dataclass(frozen=True)
class ImportRowResult:
    id: str
    row_number: int
    target_text: str
    item_id: int | None
    state: ImportResultState
    changes: tuple[ImportCellChange, ...] = ()
    diagnostic: str = ""


@dataclass(frozen=True)
class MetadataImportPreview:
    identity: IORequestIdentity
    source: SourceFileIdentity
    root: Path
    scope: IOScope
    mapping: tuple[CsvColumnMapping, ...]
    identity_mapping: CsvIdentityMapping
    blank_policy: BlankValuePolicy
    rows: tuple[ImportRowResult, ...]
    target_identities: tuple[tuple[int, int, int], ...]
    mapping_identity: str

    @property
    def safe_change_ids(self) -> tuple[str, ...]:
        return tuple(change.id for row in self.rows for change in row.changes if change.selectable)


@dataclass(frozen=True)
class ImportAcceptanceResult:
    accepted: bool
    selected_cells: int = 0
    changed_items: int = 0
    error: IOErrorInfo | None = None


def app_generated_mapping(headers: Iterable[str]) -> tuple[tuple[CsvColumnMapping, ...], CsvIdentityMapping]:
    values = tuple(headers)
    identity = (CsvIdentityMapping("relative_path", CsvIdentityRole.RELATIVE_PATH)
                if "relative_path" in values else
                CsvIdentityMapping("filename", CsvIdentityRole.FILENAME))
    mapping = []
    for header in values:
        if header == identity.column:
            mapping.append(CsvColumnMapping(header, identity_role=identity.role))
        elif header in CANONICAL_FIELDS:
            mapping.append(CsvColumnMapping(header, target_field=header))
        else:
            mapping.append(CsvColumnMapping(header, ignored=True))
    return tuple(mapping), identity


def _mapping_identity(mapping: Iterable[CsvColumnMapping], identity: CsvIdentityMapping,
                      blank_policy: BlankValuePolicy) -> str:
    import hashlib
    payload = json.dumps({
        "mapping": [(m.source_column, m.target_field,
                     m.identity_role.value if m.identity_role else "", m.ignored) for m in mapping],
        "identity": (identity.column, identity.role.value),
        "blank": blank_policy.value,
    }, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def import_mapping_identity(mapping: Iterable[CsvColumnMapping], identity: CsvIdentityMapping,
                            blank_policy: BlankValuePolicy) -> str:
    """Return the stable dry-run mapping identity carried by worker requests."""
    return _mapping_identity(mapping, identity, blank_policy)


def _parse_import_value(field_name: str, raw: str) -> object:
    if field_name in MULTI_VALUE_FIELDS:
        if raw.startswith("["):
            value = json.loads(raw)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("invalid_multi_value")
            return tuple(value)
        return (raw,)
    if field_name in {"track_num", "track_total", "disc_num", "disc_total", "bpm"}:
        if not re.fullmatch(r"[1-9]\d*", raw.strip()):
            raise ValueError("invalid_positive_integer")
        return int(raw)
    if field_name in REPLAYGAIN_FIELDS:
        value = parse_replaygain_number(raw, peak="peak" in field_name)
        if value is None:
            raise ValueError("invalid_replaygain")
        return value
    if field_name == LYRICS_FIELD:
        if raw.startswith("{"):
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid_lyrics")
            return LyricsValue.from_dict(value)
        return LyricsValue.from_dict(raw)
    if field_name == "isrc" and raw.strip() and not re.fullmatch(r"[A-Za-z]{2}[A-Za-z0-9]{3}\d{7}", raw.strip()):
        raise ValueError("invalid_isrc")
    return raw


def _safe_relative_target(root: Path, raw: str) -> Path | None:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in PurePath(raw.replace("\\", "/")).parts:
        return None
    try:
        resolved = (root / candidate).resolve()
        resolved.relative_to(root.resolve())
        return resolved
    except (OSError, ValueError):
        return None


def build_metadata_import_preview(workspace, parsed: ParsedCsv, *, root: Path,
                                  item_ids: Iterable[int], scope: IOScope,
                                  mapping: Iterable[CsvColumnMapping],
                                  identity_mapping: CsvIdentityMapping,
                                  blank_policy: BlankValuePolicy = BlankValuePolicy.NO_CHANGE,
                                  cancellation: CancellationToken | None = None) -> MetadataImportPreview:
    if cancellation:
        cancellation.raise_if_cancelled()
    ids = tuple(dict.fromkeys(int(value) for value in item_ids))
    mappings = tuple(mapping)
    headers = parsed.headers
    by_header = {header: index for index, header in enumerate(headers)}
    if identity_mapping.column not in by_header:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_MAPPING))
    targets = {}
    for identity in ids:
        if cancellation:
            cancellation.raise_if_cancelled()
        targets[identity] = workspace.track_for_id(identity)
    targets = {identity: item for identity, item in targets.items() if item is not None}
    identity_mappings = [entry for entry in mappings if entry.identity_role]
    mapped_columns = [entry.source_column for entry in mappings]
    if (len(identity_mappings) != 1
            or identity_mappings[0].source_column != identity_mapping.column
            or identity_mappings[0].identity_role is not identity_mapping.role
            or len(mapped_columns) != len(set(mapped_columns))
            or any(column not in by_header for column in mapped_columns)
            or any(sum(bool(value) for value in (
                entry.target_field, entry.identity_role, entry.ignored)) != 1
                   for entry in mappings)):
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_MAPPING))
    field_mappings = [entry for entry in mappings if entry.target_field]
    target_fields = [entry.target_field for entry in field_mappings]
    if (len(target_fields) != len(set(target_fields))
            or any(entry.source_column not in by_header for entry in field_mappings)
            or any(field_name not in CANONICAL_FIELDS for field_name in target_fields)):
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_MAPPING))
    by_path = {item.path.resolve(): identity for identity, item in targets.items()}
    by_name: dict[str, list[int]] = {}
    for identity, item in targets.items():
        if cancellation:
            cancellation.raise_if_cancelled()
        by_name.setdefault(item.path.name.casefold(), []).append(identity)
    target_identities: list[tuple[int, int, int]] = []
    current_stats = {}
    for identity, item in targets.items():
        if cancellation:
            cancellation.raise_if_cancelled()
        try:
            stat = item.path.stat()
            current_stats[identity] = stat
            target_identities.append((identity, stat.st_size, stat.st_mtime_ns))
        except OSError:
            target_identities.append((identity, -1, -1))
    results: list[ImportRowResult] = []
    target_rows: dict[int, list[int]] = {}
    identity_index = by_header[identity_mapping.column]
    for row_number, cells in enumerate(parsed.rows, 2):
        if cancellation:
            cancellation.raise_if_cancelled()
        target_text = cells[identity_index] if identity_index < len(cells) else ""
        matched: int | None = None
        state = ImportResultState.MATCHED
        diagnostic = ""
        if identity_mapping.role is CsvIdentityRole.RELATIVE_PATH:
            path = _safe_relative_target(root, target_text)
            if path is None:
                state, diagnostic = ImportResultState.BLOCKED, "root_escape"
            else:
                matched = by_path.get(path)
                if matched is None:
                    state = ImportResultState.UNMATCHED
        elif identity_mapping.role is CsvIdentityRole.ABSOLUTE_PATH:
            try:
                path = Path(target_text).resolve()
                path.relative_to(root.resolve())
            except (OSError, ValueError):
                state, diagnostic = ImportResultState.BLOCKED, "outside_root"
            else:
                matched = by_path.get(path)
                if matched is None:
                    state = ImportResultState.UNMATCHED
        else:
            found = by_name.get(Path(target_text).name.casefold(), [])
            if len(found) == 1:
                matched = found[0]
            elif len(found) > 1:
                state = ImportResultState.AMBIGUOUS
            else:
                state = ImportResultState.UNMATCHED
        changes: list[ImportCellChange] = []
        if matched is not None:
            item = targets[matched]
            target_rows.setdefault(matched, []).append(len(results))
            tags = item.proposed.effective_tags(item.original)
            capability = FORMAT_CAPABILITIES.by_id(item.format_id)
            stale_diagnostic = ""
            stat = current_stats.get(matched)
            evidence = {header: cells[index] for index, header in enumerate(headers)}
            if stat is None:
                stale_diagnostic = "source_media_missing"
            else:
                expected = {
                    "extension": item.path.suffix.lower(),
                    "format_id": item.format_id or item.ext.lstrip(".").lower(),
                    "size_bytes": str(stat.st_size),
                    "modified_time_ns": str(stat.st_mtime_ns),
                }
                if any(evidence.get(key, "") not in {"", value}
                       for key, value in expected.items()):
                    stale_diagnostic = "stale_identity_evidence"
            schema_value = evidence.get("bananaflow_schema", "")
            unsupported_schema = bool(schema_value and schema_value != CSV_SCHEMA)
            for entry in field_mappings:
                if cancellation:
                    cancellation.raise_if_cancelled()
                raw = cells[by_header[entry.source_column]]
                cell_id = f"r{row_number}:{entry.source_column}"
                original = item.original.field_value(entry.target_field)
                effective = tags.field_value(entry.target_field)
                operation = "set"
                if raw == "":
                    if blank_policy is BlankValuePolicy.NO_CHANGE:
                        changes.append(ImportCellChange(cell_id, row_number, matched,
                            entry.source_column, entry.target_field, original, effective, None,
                            "skip", ImportResultState.SKIPPED))
                        continue
                    imported, operation = None, "clear"
                else:
                    try:
                        imported = _parse_import_value(entry.target_field, raw)
                    except (ValueError, TypeError, json.JSONDecodeError) as exc:
                        changes.append(ImportCellChange(cell_id, row_number, matched,
                            entry.source_column, entry.target_field, original, effective, raw,
                            "set", ImportResultState.INVALID, diagnostic=str(exc)))
                        continue
                if not item.metadata_editable:
                    cell_state = ImportResultState.READ_ONLY
                elif not capability.supports_field(entry.target_field):
                    cell_state = ImportResultState.UNSUPPORTED
                else:
                    from core.metadata_models import metadata_values_equal
                    compare = "" if imported is None and isinstance(effective, str) else imported
                    cell_state = (ImportResultState.NO_OP if metadata_values_equal(entry.target_field, effective, compare)
                                  else ImportResultState.CHANGE)
                changes.append(ImportCellChange(cell_id, row_number, matched,
                    entry.source_column, entry.target_field, original, effective, imported,
                    operation, cell_state,
                    capability="" if capability.supports_field(entry.target_field) else capability.level.value))
            if unsupported_schema or stale_diagnostic:
                forced_state = (ImportResultState.BLOCKED if unsupported_schema
                                else ImportResultState.STALE_IDENTITY)
                forced_diagnostic = "unsupported_schema" if unsupported_schema else stale_diagnostic
                changes = [ImportCellChange(**{
                    **change.__dict__, "state": forced_state,
                    "diagnostic": forced_diagnostic,
                }) for change in changes]
            selectable = any(change.state is ImportResultState.CHANGE for change in changes)
            invalid = any(change.state in {ImportResultState.INVALID, ImportResultState.UNSUPPORTED,
                                           ImportResultState.READ_ONLY, ImportResultState.STALE_IDENTITY,
                                           ImportResultState.BLOCKED} for change in changes)
            if selectable:
                state = ImportResultState.CHANGE
            elif invalid:
                state = next(change.state for change in changes if change.state in {
                    ImportResultState.INVALID, ImportResultState.UNSUPPORTED, ImportResultState.READ_ONLY,
                    ImportResultState.STALE_IDENTITY, ImportResultState.BLOCKED})
            else:
                state = ImportResultState.NO_OP
        results.append(ImportRowResult(f"row:{row_number}", row_number, target_text,
                                       matched, state, tuple(changes), diagnostic))
    for identity, row_indexes in target_rows.items():
        if cancellation:
            cancellation.raise_if_cancelled()
        if len(row_indexes) <= 1:
            continue
        for index in row_indexes:
            row = results[index]
            results[index] = ImportRowResult(row.id, row.row_number, row.target_text, row.item_id,
                ImportResultState.DUPLICATE_TARGET,
                tuple(ImportCellChange(**{**change.__dict__, "state": ImportResultState.DUPLICATE_TARGET,
                                          "diagnostic": "duplicate_csv_target"}) for change in row.changes),
                "duplicate_csv_target")
    request = IORequestIdentity.create("metadata_csv_import", workspace.generation,
                                       workspace.change_set.revision, ids,
                                       content_revision=workspace.content_revision)
    if cancellation:
        cancellation.raise_if_cancelled()
    return MetadataImportPreview(request, parsed.source, root.resolve(), scope, mappings,
        identity_mapping, blank_policy, tuple(results), tuple(target_identities),
        _mapping_identity(mappings, identity_mapping, blank_policy))


def accept_import_preview(workspace, preview: MetadataImportPreview,
                          selected_cell_ids: Iterable[str]) -> ImportAcceptanceResult:
    selected = set(selected_cell_ids)
    if (not preview.identity.current_for(workspace)
            or not preview.source.is_current()
            or preview.mapping_identity != _mapping_identity(
                preview.mapping, preview.identity_mapping, preview.blank_policy)):
        kind = IOErrorKind.SOURCE_CHANGED if not preview.source.is_current() else IOErrorKind.STALE_PREVIEW
        return ImportAcceptanceResult(False, error=IOErrorInfo(kind))
    safe = {change.id: change for row in preview.rows for change in row.changes if change.selectable}
    if not selected or not selected <= set(safe):
        return ImportAcceptanceResult(False, error=IOErrorInfo(IOErrorKind.INVALID_MAPPING))
    expected_identities = dict((identity, (size, mtime))
                               for identity, size, mtime in preview.target_identities)
    from core.metadata_models import metadata_values_equal
    pending = []
    for cell_id in sorted(selected):
        change = safe[cell_id]
        item = workspace.track_for_id(change.item_id)
        if item is None or not item.metadata_editable:
            return ImportAcceptanceResult(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
        try:
            stat = item.path.stat()
        except OSError:
            return ImportAcceptanceResult(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
        if expected_identities.get(change.item_id) != (stat.st_size, stat.st_mtime_ns):
            return ImportAcceptanceResult(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
        capability = FORMAT_CAPABILITIES.by_id(item.format_id)
        if not capability.supports_field(change.field):
            return ImportAcceptanceResult(False, error=IOErrorInfo(IOErrorKind.INVALID_MAPPING))
        effective = item.proposed.effective_tags(item.original).field_value(change.field)
        if not metadata_values_equal(change.field, effective, change.effective_value):
            return ImportAcceptanceResult(False, error=IOErrorInfo(IOErrorKind.STALE_PREVIEW))
        pending.append((change, item))
    before = workspace.proposal_checkpoint()
    touched: dict[int, object] = {}
    for change, item in pending:
        value = change.imported_value
        if change.field == LYRICS_FIELD:
            if change.operation == "clear":
                item.proposed.clear_lyrics()
            else:
                item.proposed.set_lyrics(value, original=item.original.lyrics)
        elif change.field in REPLAYGAIN_FIELDS:
            if change.operation == "clear":
                item.proposed.clear_replay_gain({change.field})
            else:
                item.proposed.set_replay_gain(change.field, value)
        else:
            if change.operation == "clear":
                value = -1 if change.field in {"track_num", "track_total", "disc_num", "disc_total", "bpm"} else ""
            setattr(item.proposed, change.field, value)
        touched[change.item_id] = item
    source = {"provider": "csv_import", "attribution": preview.source.path.name, "url": ""}
    workspace.capture_proposals(list(touched.values()), ChangeOrigin.IMPORT,
                                label="metadata csv import", before=before, source=source)
    return ImportAcceptanceResult(True, len(selected), len(touched))
