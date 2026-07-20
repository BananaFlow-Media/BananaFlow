from pathlib import Path
import errno

import pytest

from core.change_sets import FileIdentity
from core.metadata_csv import (
    CsvDelimiter, CsvDialectSpec, CsvEncoding, CsvEncodingSpec,
    build_metadata_export_plan, export_metadata_csv, parse_csv_file,
    render_metadata_csv,
)
import core.metadata_io as metadata_io
from core.metadata_io import (
    CancellationToken, IOErrorKind, IOScope, MetadataIOError, MetadataValueSource,
    atomic_write_bytes,
)
from core.metadata_models import (
    ArtworkEntry, ArtworkValue, AudioTrackItem, LyricsEntry, LyricsValue, OriginalTags,
)
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


def workspace(tmp_path: Path):
    path = tmp_path / "אלבום" / "01, שיר.mp3"
    path.parent.mkdir(); path.write_bytes(b"ID3-fixture")
    original = OriginalTags(
        title=' שיר, "ישן" ', artist="אמן; אורח", genre="רוק; פופ",
        comment="שורה 1\nשורה 2 😀", lyrics=LyricsValue((LyricsEntry("מילים\nנוספות"),)),
        artwork=ArtworkValue((ArtworkEntry(b"secret-image-bytes", "image/png", width=10, height=20),)),
        artwork_captured=True,
    )
    item = AudioTrackItem(path, path.parent, ".mp3", original=original,
                          format_id="mp3", baseline_identity=FileIdentity(str(path), path.stat().st_size,
                                                                          path.stat().st_mtime_ns))
    state = TagEditorWorkspaceState(); state.set_tracks([item])
    item.proposed.title = "כותרת חדשה 😀"
    return state, item


@pytest.mark.parametrize("encoding", [CsvEncoding.UTF8_BOM, CsvEncoding.UTF8, CsvEncoding.UTF16_LE])
@pytest.mark.parametrize("delimiter", [CsvDelimiter.COMMA, CsvDelimiter.SEMICOLON, CsvDelimiter.TAB])
def test_roundtrip_export_preserves_unicode_quoting_multiline_and_encoding(tmp_path, encoding, delimiter):
    state, item = workspace(tmp_path)
    plan = build_metadata_export_plan(
        state, root=tmp_path, item_ids=(state.item_id(item),), scope=IOScope.ALL_LOADED,
        value_source=MetadataValueSource.EFFECTIVE,
        encoding=CsvEncodingSpec(encoding), dialect=CsvDialectSpec(delimiter),
    )
    destination = tmp_path / f"metadata-{encoding.value}-{delimiter.name}.csv"
    result = export_metadata_csv(plan, destination)
    assert result.destination == destination
    parsed = parse_csv_file(destination, encoding=encoding, delimiter=delimiter)
    values = dict(zip(parsed.headers, parsed.rows[0]))
    assert values["bananaflow_schema"] == "bananaflow.metadata.csv.v1"
    assert values["title"] == "כותרת חדשה 😀"
    assert values["comment"] == "שורה 1\nשורה 2 😀"
    assert values["artist"] == '["אמן","אורח"]'
    assert values["lyrics"].startswith('{"entries":')
    assert b"secret-image-bytes" not in destination.read_bytes()
    assert values["artwork_count"] == "1"
    assert "absolute_path" not in parsed.headers


def test_original_effective_and_explicit_absolute_path_are_distinct(tmp_path):
    state, item = workspace(tmp_path)
    original = build_metadata_export_plan(state, root=tmp_path, item_ids=(state.item_id(item),),
        scope=IOScope.SELECTED, value_source=MetadataValueSource.ORIGINAL)
    effective = build_metadata_export_plan(state, root=tmp_path, item_ids=(state.item_id(item),),
        scope=IOScope.VISIBLE, value_source=MetadataValueSource.EFFECTIVE, include_absolute_paths=True)
    assert original.rows[0].mapping()["title"] == ' שיר, "ישן" '
    assert effective.rows[0].mapping()["title"] == "כותרת חדשה 😀"
    assert original.rows[0].mapping()["relative_path"].startswith("אלבום/")
    assert "absolute_path" in effective.headers


def test_atomic_overwrite_boundary_and_cancellation_leave_no_partial(tmp_path):
    state, item = workspace(tmp_path)
    plan = build_metadata_export_plan(state, root=tmp_path, item_ids=(state.item_id(item),),
                                      scope=IOScope.ALL_LOADED)
    destination = tmp_path / "metadata.csv"; destination.write_bytes(b"keep")
    with pytest.raises(MetadataIOError):
        export_metadata_csv(plan, destination, overwrite=False)
    assert destination.read_bytes() == b"keep"
    token = CancellationToken(); token.cancel()
    with pytest.raises(MetadataIOError):
        export_metadata_csv(plan, tmp_path / "cancelled.csv", cancellation=token)
    assert not (tmp_path / "cancelled.csv").exists()
    assert not list(tmp_path.glob(".cancelled.csv.*.tmp"))


def test_no_replace_publication_falls_back_when_hard_links_are_unsupported(tmp_path, monkeypatch):
    destination = tmp_path / "cloud-target.csv"
    monkeypatch.setattr(metadata_io.os, "link", lambda *_: (_ for _ in ()).throw(
        OSError(errno.EOPNOTSUPP, "links unsupported")))
    result = atomic_write_bytes(destination, b"validated")
    assert result.destination == destination
    assert destination.read_bytes() == b"validated"
    assert not list(tmp_path.glob(".cloud-target.csv.*.tmp"))


def test_no_replace_race_preserves_concurrently_created_destination(tmp_path, monkeypatch):
    destination = tmp_path / "race.csv"
    monkeypatch.setattr(metadata_io.os, "link", lambda *_: (_ for _ in ()).throw(
        OSError(errno.EOPNOTSUPP, "links unsupported")))

    def concurrent_create(_temporary, target):
        target.write_bytes(b"other process")
        raise FileExistsError(str(target))

    fallback_name = ("_publish_no_replace_windows" if metadata_io.os.name == "nt"
                     else "_publish_no_replace_posix")
    monkeypatch.setattr(metadata_io, fallback_name, concurrent_create)
    with pytest.raises(MetadataIOError) as raised:
        atomic_write_bytes(destination, b"ours")
    assert raised.value.info.kind is IOErrorKind.DESTINATION_EXISTS
    assert destination.read_bytes() == b"other process"
    assert not list(tmp_path.glob(".race.csv.*.tmp"))


def test_unsupported_no_replace_strategy_is_structured_and_leaves_no_output(tmp_path, monkeypatch):
    destination = tmp_path / "unsupported.csv"
    monkeypatch.setattr(metadata_io.os, "link", lambda *_: (_ for _ in ()).throw(
        OSError(errno.EOPNOTSUPP, "links unsupported")))

    def unsupported(*_):
        raise MetadataIOError(metadata_io.IOErrorInfo(IOErrorKind.UNSUPPORTED_PUBLICATION))

    fallback_name = ("_publish_no_replace_windows" if metadata_io.os.name == "nt"
                     else "_publish_no_replace_posix")
    monkeypatch.setattr(metadata_io, fallback_name, unsupported)
    with pytest.raises(MetadataIOError) as raised:
        atomic_write_bytes(destination, b"ours")
    assert raised.value.info.kind is IOErrorKind.UNSUPPORTED_PUBLICATION
    assert not destination.exists()
    assert not list(tmp_path.glob(".unsupported.csv.*.tmp"))


def test_render_is_deterministic_and_utf8_bom_default(tmp_path):
    state, item = workspace(tmp_path)
    plan = build_metadata_export_plan(state, root=tmp_path, item_ids=(state.item_id(item),),
                                      scope=IOScope.CHANGED)
    assert render_metadata_csv(plan).startswith(b"\xef\xbb\xbf")
    assert render_metadata_csv(plan) == render_metadata_csv(plan)
