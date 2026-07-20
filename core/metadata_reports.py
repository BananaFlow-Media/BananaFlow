"""Immutable Change Set and Problems report snapshots and safe renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import html
import io
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

from core.metadata_io import CancellationToken, IOScope, atomic_write_bytes
from core.metadata_models import ArtworkValue, LyricsValue


Translator = Callable[..., str]

_PROBLEM_TITLE_KEYS = {
    "metadata.title.required.v1": "meta_problem_title",
    "metadata.artist.required.v1": "meta_problem_artist",
    "numbering.track.invalid.v1": "meta_problem_track",
    "numbering.disc.invalid.v1": "meta_problem_disc",
    "pending.changed_excluded.v1": "meta_problem_excluded",
    "pending.proposal_capability.v1": "meta_problem_capability",
    "artwork.read_failed.v1": "meta_problem_artwork",
    "filesystem.external_change.v1": "meta_problem_external_change",
}


@dataclass(frozen=True)
class ChangeReportEntry:
    item_id: int
    filename: str
    relative_path: str
    absolute_path: str
    field: str
    original_value: object
    previous_value: object
    proposed_value: object
    effective_value: object
    operation: str
    origin: str
    included_in_apply: bool
    capability: str = ""
    diagnostic: str = ""
    source_provider: str = ""
    source_attribution: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class ChangeReportSnapshot:
    generated_at: str
    root_display_name: str
    root: Path
    scope: IOScope
    generation: int
    revision: int
    entries: tuple[ChangeReportEntry, ...]
    changed_files: int
    changed_fields: int
    included_files: int
    excluded_files: int
    include_absolute_paths: bool = False
    content_revision: int = 0


@dataclass(frozen=True)
class ProblemsReportEntry:
    issue_id: str
    filename: str
    relative_path: str
    absolute_path: str
    field: str
    title_key: str
    explanation_key: str
    message_args: tuple[tuple[str, object], ...]
    severity: str
    category: str
    state: str
    fixable: bool
    changed_excluded: bool
    source: str
    evidence: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class ProblemsReportSnapshot:
    generated_at: str
    root_display_name: str
    root: Path
    scope_label_key: str
    generation: int
    revision: int
    entries: tuple[ProblemsReportEntry, ...]
    severity_counts: tuple[tuple[str, int], ...]
    category_counts: tuple[tuple[str, int], ...]
    state_counts: tuple[tuple[str, int], ...]
    include_absolute_paths: bool = False
    content_revision: int = 0


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _root_display_name(root: Path) -> str:
    """Return a useful non-sensitive label, including for filesystem roots."""
    return root.name or root.drive.rstrip(":\\/") or "root"


def build_change_report_snapshot(workspace, *, root: Path, item_ids: Iterable[int],
                                 scope: IOScope,
                                 include_absolute_paths: bool = False) -> ChangeReportSnapshot:
    ids = tuple(dict.fromkeys(int(value) for value in item_ids))
    allowed = set(ids)
    records = workspace.change_set.records(item_ids=allowed)
    entries: list[ChangeReportEntry] = []
    for record in records:
        item = workspace.track_for_id(record.item_id)
        if item is None:
            continue
        effective = item.proposed.effective_tags(item.original)
        value = (item.proposed_filename if record.field == "filename"
                 else effective.field_value(record.field))
        entries.append(ChangeReportEntry(
            record.item_id, item.path.name, _relative(item.path, root),
            str(item.path.resolve()) if include_absolute_paths else "", record.field,
            record.original_value, record.previous_value, record.proposed_value, value,
            record.operation.value, record.origin.value, not item.excluded_from_apply,
            record.capability, record.diagnostic, record.source_provider,
            record.source_attribution, record.source_url,
        ))
    entries.sort(key=lambda entry: (ids.index(entry.item_id), entry.field))
    file_ids = {entry.item_id for entry in entries}
    excluded = {entry.item_id for entry in entries if not entry.included_in_apply}
    return ChangeReportSnapshot(
        _generated_at(), _root_display_name(root), root.resolve(), scope,
        workspace.generation, workspace.change_set.revision, tuple(entries),
        len(file_ids), len(entries), len(file_ids - excluded), len(excluded),
        bool(include_absolute_paths), workspace.content_revision,
    )


def build_problems_report_snapshot(workspace, validation_snapshot, *, root: Path,
                                   issue_ids: Iterable[str] | None = None,
                                   scope_label_key: str = "meta_report_scope_all_issues",
                                   include_absolute_paths: bool = False) -> ProblemsReportSnapshot:
    if not validation_snapshot.current_for(workspace):
        raise ValueError("stale_validation_snapshot")
    allowed = set(issue_ids) if issue_ids is not None else None
    entries: list[ProblemsReportEntry] = []
    severity: dict[str, int] = {}
    category: dict[str, int] = {}
    state: dict[str, int] = {}
    for issue in validation_snapshot.issues:
        if allowed is not None and issue.id not in allowed:
            continue
        path = Path(issue.display_paths[0]) if issue.display_paths else Path("")
        title_key = _PROBLEM_TITLE_KEYS.get(issue.rule_id, issue.message_key)
        entries.append(ProblemsReportEntry(
            issue.id, path.name, _relative(path, root),
            str(path.resolve()) if include_absolute_paths and issue.display_paths else "",
            issue.fields[0] if issue.fields else "",
            title_key, issue.message_key, issue.message_args,
            issue.severity.value, issue.category.value, issue.state.value,
            issue.fixable, issue.state.value == "changed_excluded", issue.source,
            issue.evidence,
        ))
        severity[issue.severity.value] = severity.get(issue.severity.value, 0) + 1
        category[issue.category.value] = category.get(issue.category.value, 0) + 1
        state[issue.state.value] = state.get(issue.state.value, 0) + 1
    return ProblemsReportSnapshot(
        _generated_at(), _root_display_name(root), root.resolve(), scope_label_key,
        validation_snapshot.generation, validation_snapshot.revision, tuple(entries),
        tuple(sorted(severity.items())), tuple(sorted(category.items())), tuple(sorted(state.items())),
        bool(include_absolute_paths), workspace.content_revision,
    )


def _value(value: object) -> str:
    if isinstance(value, ArtworkValue):
        primary = value.primary
        return f"{len(value.entries)}; {primary.mime_type if primary else ''}".strip("; ")
    if isinstance(value, LyricsValue):
        primary = value.primary
        return primary.text if primary else ""
    if isinstance(value, (tuple, list)):
        return "; ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _translate(translator: Translator, key: str, **kwargs) -> str:
    try:
        return translator(key, **kwargs)
    except Exception:
        try:
            return translator(key)
        except Exception:
            return key


def _formula_safe(value: str, enabled: bool) -> str:
    if enabled and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _scope_label(scope: IOScope, translator: Translator) -> str:
    suffix = "all" if scope is IOScope.ALL_LOADED else scope.value
    return _translate(translator, f"meta_io_scope_{suffix}")


def _context(root_display_name: str, root: Path, include_absolute_paths: bool,
             scope: str, generated_at: str, translator: Translator) -> str:
    root_value = str(root) if include_absolute_paths else root_display_name
    return _translate(translator, "meta_report_context", root=root_value,
                      scope=scope, generated=generated_at)


_REPORT_VALUE_KEYS = {
    "field": {
        "": "meta_report_value_not_applicable",
        "title": "meta_field_title", "artist": "meta_field_artist",
        "album": "meta_field_album", "album_artist": "meta_field_album_artist",
        "track_num": "meta_field_track_num", "track_total": "meta_field_track_total",
        "disc_num": "meta_field_disc_num", "disc_total": "meta_field_disc_total",
        "year": "meta_field_year", "genre": "meta_field_genre",
        "comment": "meta_field_comment", "composer": "meta_field_composer",
        "publisher": "meta_field_publisher", "copyright": "meta_field_copyright",
        "bpm": "meta_field_bpm", "isrc": "meta_field_isrc",
        "grouping": "meta_field_grouping", "sort_title": "meta_field_sort_title",
        "sort_artist": "meta_field_sort_artist", "sort_album": "meta_field_sort_album",
        "sort_album_artist": "meta_field_sort_album_artist", "filename": "meta_field_filename",
        "lyrics": "meta_report_field_lyrics", "artwork": "meta_report_field_artwork",
        "replaygain_track_gain": "meta_report_field_replaygain_track_gain",
        "replaygain_track_peak": "meta_report_field_replaygain_track_peak",
        "replaygain_album_gain": "meta_report_field_replaygain_album_gain",
        "replaygain_album_peak": "meta_report_field_replaygain_album_peak",
        "replaygain_reference_loudness": "meta_report_field_replaygain_reference_loudness",
    },
    "operation": {
        value: f"meta_change_operation_{value}"
        for value in ("set", "clear", "add", "replace", "remove", "rename", "move")
    },
    "origin": {
        value: f"meta_change_origin_{value}"
        for value in (
            "manual", "auto_arrange", "cleanup", "filename", "lyrics", "replaygain",
            "artwork_add", "artwork_replace", "artwork_remove", "restore", "recovery",
            "template", "online_metadata", "import",
        )
    },
    "severity": {
        value: f"meta_problems_severity_{value}"
        for value in ("information", "warning", "error", "blocker")
    },
    "category": {
        value: f"meta_problems_category_{value}"
        for value in (
            "basic_metadata", "numbering", "format_capability", "pending_changes",
            "artwork", "filename_path", "duplicates",
        )
    },
    "state": {
        value: f"meta_problems_state_{value}"
        for value in (
            "present", "present_on_disk", "resolved_by_pending", "introduced_by_pending",
            "pending_blocker", "changed_excluded",
        )
    },
    "capability": {
        "": "meta_report_value_not_applicable", "full": "meta_report_capability_full",
        "limited": "meta_report_capability_limited", "read_only": "meta_report_capability_read_only",
        "unsupported": "meta_report_capability_unsupported", "future": "meta_report_capability_future",
    },
    "source": {
        "": "meta_report_value_not_applicable", "validation": "meta_report_source_validation",
        "change_set": "meta_report_source_pending", "pending": "meta_report_source_pending",
        "artwork": "meta_report_source_artwork", "disk": "meta_report_source_disk",
        "online_metadata": "meta_report_source_online_metadata",
        "musicbrainz": "meta_report_source_musicbrainz",
        "cover_art_archive": "meta_report_source_cover_art_archive",
        "csv_import": "meta_report_source_csv_import",
    },
}


def _report_value(translator: Translator, family: str, value: str) -> str:
    key = _REPORT_VALUE_KEYS.get(family, {}).get(str(value))
    rendered = _translate(translator, key or "meta_report_value_unknown")
    return rendered.rstrip(":") if family == "field" else rendered


def render_change_report_csv(snapshot: ChangeReportSnapshot, translator: Translator,
                             *, include_technical_ids: bool = False,
                             spreadsheet_safe: bool = False) -> bytes:
    headers = ["filename", "relative_path", "field", "original", "previous",
               "proposed", "effective", "operation", "origin", "included",
               "capability", "warning", "source", "source_url"]
    if snapshot.include_absolute_paths:
        headers.insert(2, "absolute_path")
    if include_technical_ids:
        headers.insert(0, "item_id")
        headers.extend(("field_id", "operation_id", "origin_id", "capability_id", "source_id"))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow([_translate(translator, "meta_report_change_title")])
    writer.writerow([_context(snapshot.root_display_name, snapshot.root,
                              snapshot.include_absolute_paths,
                              _scope_label(snapshot.scope, translator),
                              snapshot.generated_at, translator)])
    writer.writerow([_translate(translator, "meta_report_change_summary",
                                files=snapshot.changed_files, fields=snapshot.changed_fields,
                                included=snapshot.included_files, excluded=snapshot.excluded_files)])
    writer.writerow([])
    writer.writerow([_translate(translator, f"meta_report_header_{header}") for header in headers])
    for entry in snapshot.entries:
        row = [entry.filename, entry.relative_path]
        if snapshot.include_absolute_paths:
            row.append(entry.absolute_path)
        row.extend([
            _report_value(translator, "field", entry.field),
            _value(entry.original_value), _value(entry.previous_value),
            _value(entry.proposed_value), _value(entry.effective_value),
            _report_value(translator, "operation", entry.operation),
            _report_value(translator, "origin", entry.origin),
            _translate(translator, "yes" if entry.included_in_apply else "no"),
            _report_value(translator, "capability", entry.capability),
            _translate(translator, "meta_report_warning_present") if entry.diagnostic else "",
            entry.source_attribution or _report_value(translator, "source", entry.source_provider),
            entry.source_url,
        ])
        if include_technical_ids:
            row.insert(0, str(entry.item_id))
            row.extend((entry.field, entry.operation, entry.origin,
                        entry.capability, entry.source_provider))
        writer.writerow([_formula_safe(str(value), spreadsheet_safe) for value in row])
    return codecs_bom_utf8(stream.getvalue())


def render_problems_report_csv(snapshot: ProblemsReportSnapshot, translator: Translator,
                               *, include_technical_ids: bool = False,
                               spreadsheet_safe: bool = False) -> bytes:
    headers = ["filename", "relative_path", "field", "title", "explanation",
               "severity", "category", "state", "fixable", "changed_excluded", "source"]
    if snapshot.include_absolute_paths:
        headers.insert(2, "absolute_path")
    if include_technical_ids:
        headers.insert(0, "issue_id")
        headers.extend(("field_id", "severity_id", "category_id", "state_id", "source_id"))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow([_translate(translator, "meta_report_problems_title")])
    writer.writerow([_context(snapshot.root_display_name, snapshot.root,
                              snapshot.include_absolute_paths,
                              _translate(translator, snapshot.scope_label_key),
                              snapshot.generated_at, translator)])
    writer.writerow([_translate(translator, "meta_report_problems_summary", count=len(snapshot.entries))])
    writer.writerow([])
    writer.writerow([_translate(translator, f"meta_report_header_{header}") for header in headers])
    for entry in snapshot.entries:
        args = dict(entry.message_args)
        row = [entry.filename, entry.relative_path]
        if snapshot.include_absolute_paths:
            row.append(entry.absolute_path)
        row.extend([
            _report_value(translator, "field", entry.field),
            _translate(translator, entry.title_key),
            _translate(translator, entry.explanation_key, **args),
            _report_value(translator, "severity", entry.severity),
            _report_value(translator, "category", entry.category),
            _report_value(translator, "state", entry.state),
            _translate(translator, "yes" if entry.fixable else "no"),
            _translate(translator, "yes" if entry.changed_excluded else "no"),
            _report_value(translator, "source", entry.source),
        ])
        if include_technical_ids:
            row.insert(0, entry.issue_id)
            row.extend((entry.field, entry.severity, entry.category, entry.state, entry.source))
        writer.writerow([_formula_safe(str(value), spreadsheet_safe) for value in row])
    return codecs_bom_utf8(stream.getvalue())


def codecs_bom_utf8(text: str) -> bytes:
    return b"\xef\xbb\xbf" + text.encode("utf-8", errors="strict")


def _safe_link(url: str) -> str:
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _html_document(title: str, summary: str, headers: list[str], rows: list[list[tuple[str, bool]]],
                   *, language: str, generated_at: str) -> bytes:
    direction = "rtl" if language == "he" else "ltr"
    th = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = "".join(
            '<td dir="ltr" class="technical">{}</td>'.format(value)
            if technical else '<td>{}</td>'.format(value)
            for value, technical in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    document = f"""<!doctype html>
<html lang="{html.escape(language)}" dir="{direction}"><head><meta charset="utf-8">
<title>{html.escape(title)}</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:2rem;color:#222}}h1{{font-size:1.5rem}}
.summary{{margin:.8rem 0 1.2rem}}table{{border-collapse:collapse;width:100%;font-size:.9rem}}
th,td{{border:1px solid #bbb;padding:.4rem;text-align:start;vertical-align:top}}
th{{background:#eee}}.technical{{direction:ltr;text-align:left;unicode-bidi:embed}}
@media print{{body{{margin:.5rem}}a{{color:inherit;text-decoration:none}}}}
</style></head><body><h1>{html.escape(title)}</h1><div class="summary">{html.escape(summary)}</div>
<div class="technical" dir="ltr">{html.escape(generated_at)}</div>
<table><thead><tr>{th}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></body></html>"""
    return document.encode("utf-8", errors="strict")


def render_change_report_html(snapshot: ChangeReportSnapshot, translator: Translator,
                              *, language: str = "en") -> bytes:
    header_ids = ["filename", "relative_path", "field", "original", "previous", "proposed",
                  "operation", "origin", "included", "capability", "warning", "source"]
    if snapshot.include_absolute_paths:
        header_ids.insert(2, "absolute_path")
    rows = []
    for entry in snapshot.entries:
        source_label = entry.source_attribution or _report_value(
            translator, "source", entry.source_provider)
        source = html.escape(source_label)
        link = _safe_link(entry.source_url)
        if link:
            source = f'<a href="{html.escape(link, quote=True)}">{source or html.escape(link)}</a>'
        row = [
            (html.escape(entry.filename), True), (html.escape(entry.relative_path), True),
        ]
        if snapshot.include_absolute_paths:
            row.append((html.escape(entry.absolute_path), True))
        row.extend([
            (html.escape(_report_value(translator, "field", entry.field)), False),
            (html.escape(_value(entry.original_value)), entry.field == "isrc"),
            (html.escape(_value(entry.previous_value)), entry.field == "isrc"),
            (html.escape(_value(entry.proposed_value)), entry.field == "isrc"),
            (html.escape(_report_value(translator, "operation", entry.operation)), False),
            (html.escape(_report_value(translator, "origin", entry.origin)), False),
            (html.escape(_translate(translator, "yes" if entry.included_in_apply else "no")), False),
            (html.escape(_report_value(translator, "capability", entry.capability)), False),
            (html.escape(_translate(translator, "meta_report_warning_present")) if entry.diagnostic else "", False),
            (source, bool(link)),
        ])
        rows.append(row)
    summary = _translate(translator, "meta_report_change_summary", files=snapshot.changed_files,
                         fields=snapshot.changed_fields, included=snapshot.included_files,
                         excluded=snapshot.excluded_files) + " · " + _context(
                             snapshot.root_display_name, snapshot.root,
                             snapshot.include_absolute_paths,
                             _scope_label(snapshot.scope, translator),
                             snapshot.generated_at, translator)
    return _html_document(_translate(translator, "meta_report_change_title"), summary,
        [_translate(translator, f"meta_report_header_{value}") for value in header_ids], rows,
        language=language, generated_at=snapshot.generated_at)


def render_problems_report_html(snapshot: ProblemsReportSnapshot, translator: Translator,
                                *, language: str = "en") -> bytes:
    header_ids = ["filename", "relative_path", "field", "title", "explanation",
                  "severity", "category", "state", "fixable", "source"]
    if snapshot.include_absolute_paths:
        header_ids.insert(2, "absolute_path")
    rows = []
    for entry in snapshot.entries:
        args = dict(entry.message_args)
        row = [
            (html.escape(entry.filename), True), (html.escape(entry.relative_path), True),
        ]
        if snapshot.include_absolute_paths:
            row.append((html.escape(entry.absolute_path), True))
        row.extend([
            (html.escape(_report_value(translator, "field", entry.field)), False),
            (html.escape(_translate(translator, entry.title_key)), False),
            (html.escape(_translate(translator, entry.explanation_key, **args)), False),
            (html.escape(_report_value(translator, "severity", entry.severity)), False),
            (html.escape(_report_value(translator, "category", entry.category)), False),
            (html.escape(_report_value(translator, "state", entry.state)), False),
            (html.escape(_translate(translator, "yes" if entry.fixable else "no")), False),
            (html.escape(_report_value(translator, "source", entry.source)), False),
        ])
        rows.append(row)
    summary = _translate(translator, "meta_report_problems_summary", count=len(snapshot.entries)) + " · " + _context(
        snapshot.root_display_name, snapshot.root, snapshot.include_absolute_paths,
        _translate(translator, snapshot.scope_label_key), snapshot.generated_at, translator)
    return _html_document(_translate(translator, "meta_report_problems_title"), summary,
        [_translate(translator, f"meta_report_header_{value}") for value in header_ids], rows,
        language=language, generated_at=snapshot.generated_at)


def export_report(destination: Path, data: bytes, *, overwrite: bool = False,
                  cancellation: CancellationToken | None = None,
                  html_report: bool = False):
    def validate(path: Path) -> bool:
        raw = path.read_bytes()
        if raw != data:
            return False
        if html_report:
            text = raw.decode("utf-8", errors="strict").casefold()
            return "<script" not in text and "</html>" in text
        return True
    return atomic_write_bytes(destination, data, overwrite=overwrite,
                              validator=validate, cancellation=cancellation)
