from pathlib import Path

import pytest

import core.metadata_csv as metadata_csv
from core.change_sets import ChangeOrigin, FileIdentity
from core.metadata_csv import (
    BlankValuePolicy, CsvColumnMapping, CsvDelimiter, CsvEncoding,
    CsvIdentityMapping, CsvIdentityRole, ImportResultState,
    accept_import_preview, app_generated_mapping, build_metadata_import_preview,
    parse_csv_file,
)
from core.metadata_io import CancellationToken, IOScope, IOErrorKind, MetadataIOError
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def make_workspace(tmp_path: Path, names=("one.mp3", "two.mp3")):
    root = tmp_path / "music"; root.mkdir()
    items = []
    for index, name in enumerate(names, 1):
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(f"media-{index}".encode())
        item = AudioTrackItem(path, path.parent, ".mp3",
            original=OriginalTags(title=f"Old {index}", artist="Artist", track_num=index),
            format_id="mp3", baseline_identity=FileIdentity(str(path), path.stat().st_size,
                                                              path.stat().st_mtime_ns))
        items.append(item)
    state = TagEditorWorkspaceState(); state.set_tracks(items)
    return state, root, items


def write_csv(path: Path, text: str, encoding="utf-8-sig"):
    path.write_text(text, encoding=encoding, newline="")
    return path


def test_large_dry_run_cancels_during_cell_parsing_before_all_rows(tmp_path, monkeypatch):
    state, root, items = make_workspace(tmp_path, names=("one.mp3",))
    csv_path = write_csv(
        tmp_path / "large.csv",
        "relative_path,title\n" + "".join(
            f"one.mp3,Title {index}\n" for index in range(2000)))
    parsed = parse_csv_file(csv_path)
    mapping, identity = app_generated_mapping(parsed.headers)
    token = CancellationToken()
    calls = 0
    original = metadata_csv._parse_import_value

    def cancelling_parse(field, raw):
        nonlocal calls
        calls += 1
        if calls == 25:
            token.cancel()
        return original(field, raw)

    monkeypatch.setattr(metadata_csv, "_parse_import_value", cancelling_parse)
    with pytest.raises(MetadataIOError) as raised:
        build_metadata_import_preview(
            state, parsed, root=root, item_ids=(state.item_id(items[0]),),
            scope=IOScope.ALL_LOADED, mapping=mapping,
            identity_mapping=identity, cancellation=token)
    assert raised.value.info.kind is IOErrorKind.CANCELLED
    assert calls == 25 < len(parsed.rows)


def preview_for(state, root, parsed, *, blank=BlankValuePolicy.NO_CHANGE,
                role=CsvIdentityRole.RELATIVE_PATH, identity_column="relative_path", mapping=None):
    if mapping is None:
        mapping, identity = app_generated_mapping(parsed.headers)
    else:
        identity = CsvIdentityMapping(identity_column, role)
    return build_metadata_import_preview(
        state, parsed, root=root, item_ids=tuple(state.item_id(item) for item in state.tracks),
        scope=IOScope.ALL_LOADED, mapping=mapping, identity_mapping=identity,
        blank_policy=blank,
    )


def test_app_csv_auto_mapping_dry_run_is_immutable_and_accepts_one_command(tmp_path):
    state, root, items = make_workspace(tmp_path)
    items[0].excluded_from_apply = True
    csv_path = write_csv(tmp_path / "edit.csv",
        "bananaflow_schema,relative_path,title,artist,track_num\r\n"
        "bananaflow.metadata.csv.v1,one.mp3,New title,,3\r\n"
        "bananaflow.metadata.csv.v1,two.mp3,Old 2,Artist,2\r\n")
    parsed = parse_csv_file(csv_path)
    before_revision = state.change_set.revision
    preview = preview_for(state, root, parsed)
    assert state.change_set.revision == before_revision
    first = preview.rows[0]
    assert first.state is ImportResultState.CHANGE
    assert {change.field: change.state for change in first.changes} == {
        "title": ImportResultState.CHANGE,
        "artist": ImportResultState.SKIPPED,
        "track_num": ImportResultState.CHANGE,
    }
    result = accept_import_preview(state, preview, preview.safe_change_ids)
    assert result.accepted and result.changed_items == 1 and result.selected_cells == 2
    records = state.change_set.records(item_ids={state.item_id(items[0])})
    assert {record.origin for record in records} == {ChangeOrigin.IMPORT}
    assert {record.source_provider for record in records} == {"csv_import"}
    assert {record.source_attribution for record in records} == {"edit.csv"}
    assert state.proposal_history.can_undo(state.generation)
    assert items[0].excluded_from_apply is True
    assert items[0].path.read_bytes() == b"media-1"
    assert items[0].path.name == "one.mp3"
    assert state.undo_proposals()
    assert items[0].proposed.title is None


def test_blank_clear_is_explicit_and_reviewable(tmp_path):
    state, root, items = make_workspace(tmp_path)
    parsed = parse_csv_file(write_csv(tmp_path / "clear.csv", "relative_path,title\none.mp3,\n"))
    skip = preview_for(state, root, parsed)
    clear = preview_for(state, root, parsed, blank=BlankValuePolicy.CLEAR)
    assert skip.rows[0].changes[0].state is ImportResultState.SKIPPED
    change = clear.rows[0].changes[0]
    assert change.state is ImportResultState.CHANGE and change.operation == "clear"
    assert clear.mapping_identity != skip.mapping_identity
    assert accept_import_preview(state, clear, (change.id,)).accepted
    record = state.change_set.records()[0]
    assert record.proposed_value == "" and record.operation.value == "clear"


def test_relative_escape_filename_ambiguity_duplicate_targets_and_unmatched(tmp_path):
    state, root, _ = make_workspace(tmp_path, ("a/dup.mp3", "b/dup.mp3"))
    parsed = parse_csv_file(write_csv(tmp_path / "targets.csv",
        "relative_path,title\n../escape.mp3,X\nmissing.mp3,Y\na/dup.mp3,Z\na/dup.mp3,Q\n"))
    mapping, identity = app_generated_mapping(parsed.headers)
    preview = build_metadata_import_preview(state, parsed, root=root,
        item_ids=tuple(state.item_id(item) for item in state.tracks), scope=IOScope.ALL_LOADED,
        mapping=mapping, identity_mapping=identity)
    assert [row.state for row in preview.rows] == [
        ImportResultState.BLOCKED, ImportResultState.UNMATCHED,
        ImportResultState.DUPLICATE_TARGET, ImportResultState.DUPLICATE_TARGET,
    ]
    filename_parsed = parse_csv_file(write_csv(tmp_path / "filename.csv", "filename,title\ndup.mp3,X\n"))
    filename_mapping = (
        CsvColumnMapping("filename", identity_role=CsvIdentityRole.FILENAME),
        CsvColumnMapping("title", target_field="title"),
    )
    filename_preview = preview_for(state, root, filename_parsed, role=CsvIdentityRole.FILENAME,
        identity_column="filename", mapping=filename_mapping)
    assert filename_preview.rows[0].state is ImportResultState.AMBIGUOUS


def test_arbitrary_mapping_numeric_validation_read_only_and_unsupported(tmp_path):
    state, root, items = make_workspace(tmp_path)
    items[1].metadata_editable = False
    parsed = parse_csv_file(write_csv(tmp_path / "mapped.csv",
        "Path,Name,Number\none.mp3,Changed,zero\ntwo.mp3,Changed,4\n"))
    mapping = (
        CsvColumnMapping("Path", identity_role=CsvIdentityRole.RELATIVE_PATH),
        CsvColumnMapping("Name", target_field="title"),
        CsvColumnMapping("Number", target_field="track_num"),
    )
    preview = preview_for(state, root, parsed, identity_column="Path", mapping=mapping)
    assert any(change.state is ImportResultState.INVALID for change in preview.rows[0].changes)
    assert all(change.state is ImportResultState.READ_ONLY for change in preview.rows[1].changes)


def test_encoding_detection_windows_1255_and_manual_semicolon(tmp_path):
    state, root, _ = make_workspace(tmp_path)
    path = tmp_path / "hebrew.csv"
    path.write_bytes("relative_path;title\none.mp3;שלום\n".encode("cp1255"))
    with pytest.raises(Exception):
        parse_csv_file(path)
    parsed = parse_csv_file(path, encoding=CsvEncoding.WINDOWS_1255,
                            delimiter=CsvDelimiter.SEMICOLON)
    assert parsed.rows[0][1] == "שלום"


def test_source_change_and_workspace_revision_reject_acceptance(tmp_path):
    state, root, items = make_workspace(tmp_path)
    path = write_csv(tmp_path / "stale.csv", "relative_path,title\none.mp3,Changed\n")
    preview = preview_for(state, root, parse_csv_file(path))
    path.write_text("relative_path,title\none.mp3,Different\n", encoding="utf-8-sig")
    result = accept_import_preview(state, preview, preview.safe_change_ids)
    assert not result.accepted and result.error.kind is IOErrorKind.SOURCE_CHANGED
    path.write_text("relative_path,title\none.mp3,Changed\n", encoding="utf-8-sig")
    preview = preview_for(state, root, parse_csv_file(path))
    items[1].proposed.title = "pending"; state.capture_proposals([items[1]])
    result = accept_import_preview(state, preview, preview.safe_change_ids)
    assert not result.accepted and result.error.kind is IOErrorKind.STALE_PREVIEW


def test_identity_evidence_mapping_and_media_change_are_safety_boundaries(tmp_path):
    state, root, items = make_workspace(tmp_path)
    source = write_csv(tmp_path / "evidence.csv",
        "bananaflow_schema,relative_path,size_bytes,modified_time_ns,extension,format_id,title\n"
        "bananaflow.metadata.csv.v1,one.mp3,999,1,.mp3,mp3,Changed\n")
    parsed = parse_csv_file(source)
    preview = preview_for(state, root, parsed)
    assert preview.rows[0].state is ImportResultState.STALE_IDENTITY
    assert not preview.safe_change_ids

    invalid_mapping = (
        CsvColumnMapping("relative_path", identity_role=CsvIdentityRole.RELATIVE_PATH),
        CsvColumnMapping("title", identity_role=CsvIdentityRole.FILENAME),
    )
    with pytest.raises(MetadataIOError) as error:
        build_metadata_import_preview(state, parsed, root=root,
            item_ids=tuple(state.item_id(item) for item in state.tracks), scope=IOScope.ALL_LOADED,
            mapping=invalid_mapping,
            identity_mapping=CsvIdentityMapping("relative_path", CsvIdentityRole.RELATIVE_PATH))
    assert error.value.info.kind is IOErrorKind.INVALID_MAPPING

    clean = parse_csv_file(write_csv(tmp_path / "clean.csv", "relative_path,title\none.mp3,Changed\n"))
    preview = preview_for(state, root, clean)
    items[0].path.write_bytes(b"externally changed media")
    result = accept_import_preview(state, preview, preview.safe_change_ids)
    assert not result.accepted and result.error.kind is IOErrorKind.STALE_PREVIEW
    assert items[0].proposed.title is None
