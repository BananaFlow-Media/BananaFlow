from pathlib import Path

from core.change_sets import ChangeOrigin, FileIdentity
from core.metadata_io import IOScope
from core.metadata_models import AudioTrackItem, OriginalTags
from core.metadata_reports import (
    build_change_report_snapshot, build_problems_report_snapshot,
    export_report, render_change_report_csv, render_change_report_html,
    render_problems_report_csv, render_problems_report_html,
)
from core.metadata_validation import MetadataValidationEngine
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.i18n import set_language, t


def translator(key, **kwargs):
    values = {
        "meta_report_change_title": "Pending <Changes>",
        "meta_report_problems_title": "Problems",
        "meta_report_change_summary": "{files} changed files; {fields} fields; {included} included; {excluded} excluded",
        "meta_report_problems_summary": "{count} problems",
        "meta_report_context": "Root: {root}; Scope: {scope}; Generated: {generated}",
        "meta_io_scope_changed": "Changed",
        "meta_report_scope_all_issues": "All issues",
        "meta_report_warning_present": "Warning",
        "meta_report_value_not_applicable": "Not applicable",
        "meta_report_source_musicbrainz": "MusicBrainz",
        "meta_field_title": "Title:",
        "meta_change_operation_set": "Set",
        "meta_change_origin_online_metadata": "Online metadata",
        "yes": "Yes", "no": "No",
    }
    text = values.get(key, key)
    try: return text.format(**kwargs)
    except (KeyError, ValueError): return text


def workspace(tmp_path: Path):
    root = tmp_path / "music"; root.mkdir(); path = root / "track & song.mp3"; path.write_bytes(b"media")
    item = AudioTrackItem(path, root, ".mp3", original=OriginalTags(title="", artist=""),
        format_id="mp3", baseline_identity=FileIdentity(str(path), path.stat().st_size, path.stat().st_mtime_ns))
    state = TagEditorWorkspaceState(); state.set_tracks([item])
    before = state.proposal_checkpoint(); item.proposed.title = "<script>alert(1)</script>"
    state.capture_proposals([item], ChangeOrigin.ONLINE_METADATA, before=before,
        source={"provider": "musicbrainz", "attribution": "Provider <name>",
                "url": "https://example.test/item?q=<unsafe>"})
    state.set_apply_excluded_ids({state.item_id(item)}, True)
    return state, root, item


def test_change_report_snapshot_csv_and_safe_english_hebrew_html(tmp_path):
    state, root, item = workspace(tmp_path)
    snapshot = build_change_report_snapshot(state, root=root, item_ids=(state.item_id(item),),
                                            scope=IOScope.CHANGED)
    assert snapshot.changed_files == 1 and snapshot.changed_fields == 1
    assert snapshot.excluded_files == 1 and not snapshot.entries[0].included_in_apply
    assert snapshot.entries[0].source_provider == "musicbrainz"
    csv_data = render_change_report_csv(snapshot, translator, spreadsheet_safe=True)
    assert csv_data.startswith(b"\xef\xbb\xbf")
    assert str(root).encode("utf-8") not in csv_data
    assert b"music" in csv_data and b"track & song.mp3" in csv_data
    english = render_change_report_html(snapshot, translator, language="en").decode()
    hebrew = render_change_report_html(snapshot, translator, language="he").decode()
    assert 'lang="en" dir="ltr"' in english
    assert 'lang="he" dir="rtl"' in hebrew
    assert '<script' not in english.casefold()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in english
    assert 'href="https://example.test/item?q=&lt;unsafe&gt;"' in english
    assert 'class="technical"' in hebrew
    assert str(root) not in english and "music" in english and "changed" in english
    destination = tmp_path / "report.html"
    export_report(destination, english.encode(), html_report=True)
    assert destination.read_text(encoding="utf-8").endswith("</html>")


def test_unsafe_source_scheme_is_never_clickable(tmp_path):
    state, root, item = workspace(tmp_path)
    record = state.change_set.records()[0]
    state.change_set.record(record.item_id, record.field, record.original_value,
        record.proposed_value, operation=record.operation, origin=record.origin,
        equal=lambda *_: False, source_url="javascript:alert(1)")
    snapshot = build_change_report_snapshot(state, root=root, item_ids=(state.item_id(item),), scope=IOScope.CHANGED)
    document = render_change_report_html(snapshot, translator).decode()
    assert "javascript:" not in document and "href=" not in document


def test_problems_report_uses_fresh_proposal_aware_snapshot_without_raw_ids(tmp_path):
    state, root, item = workspace(tmp_path)
    validation = MetadataValidationEngine().validate(state)
    snapshot = build_problems_report_snapshot(state, validation, root=root)
    assert snapshot.generation == state.generation and snapshot.revision == state.change_set.revision
    assert snapshot.entries
    document = render_problems_report_html(snapshot, translator, language="he").decode()
    assert 'dir="rtl"' in document
    assert "metadata.artist.required.v1" not in document
    assert "tag.set_field.v1" not in document
    assert "<script" not in document.casefold()


def test_report_absolute_paths_are_explicit_consistent_and_not_implied_by_technical_mode(tmp_path):
    state, root, item = workspace(tmp_path)
    identity = state.item_id(item)
    validation = MetadataValidationEngine().validate(state)
    change_default = build_change_report_snapshot(
        state, root=root, item_ids=(identity,), scope=IOScope.CHANGED)
    problems_default = build_problems_report_snapshot(state, validation, root=root)
    renderers = (
        lambda: render_change_report_csv(change_default, translator),
        lambda: render_change_report_html(change_default, translator),
        lambda: render_problems_report_csv(problems_default, translator),
        lambda: render_problems_report_html(problems_default, translator),
        lambda: render_change_report_csv(change_default, translator, include_technical_ids=True),
        lambda: render_problems_report_csv(problems_default, translator, include_technical_ids=True),
    )
    for render in renderers:
        assert str(root).encode("utf-8") not in render()

    change_absolute = build_change_report_snapshot(
        state, root=root, item_ids=(identity,), scope=IOScope.CHANGED,
        include_absolute_paths=True)
    problems_absolute = build_problems_report_snapshot(
        state, validation, root=root, include_absolute_paths=True)
    for data in (
        render_change_report_csv(change_absolute, translator),
        render_change_report_html(change_absolute, translator),
        render_problems_report_csv(problems_absolute, translator),
        render_problems_report_html(problems_absolute, translator),
    ):
        assert str(root).encode("utf-8") in data


def test_human_report_values_are_localized_and_raw_values_are_technical_only(tmp_path):
    state, root, item = workspace(tmp_path)
    record = state.change_set.records()[0]
    state.change_set.record(
        record.item_id, record.field, record.original_value, record.proposed_value,
        operation=record.operation, origin=record.origin, equal=lambda *_: False,
        capability="read_only", source_provider="musicbrainz",
    )
    snapshot = build_change_report_snapshot(
        state, root=root, item_ids=(state.item_id(item),), scope=IOScope.CHANGED)
    try:
        set_language("en")
        english = render_change_report_csv(snapshot, t).decode("utf-8-sig")
        assert "Read-only" in english and "MusicBrainz" in english
        assert "read_only" not in english and "online_metadata" not in english
        technical = render_change_report_csv(
            snapshot, t, include_technical_ids=True).decode("utf-8-sig")
        assert "Technical capability value" in technical and "read_only" in technical
        assert "Technical origin value" in technical and "online_metadata" in technical

        set_language("he")
        hebrew = render_change_report_csv(snapshot, t).decode("utf-8-sig")
        assert "קריאה בלבד" in hebrew and "MusicBrainz" in hebrew
        assert "read_only" not in hebrew and "online_metadata" not in hebrew
    finally:
        set_language("en")


def test_unknown_report_values_use_localized_fallback(tmp_path):
    state, root, item = workspace(tmp_path)
    record = state.change_set.records()[0]
    state.change_set.record(
        record.item_id, record.field, record.original_value, record.proposed_value,
        operation=record.operation, origin=record.origin, equal=lambda *_: False,
        capability="future_core_value", source_provider="future_source",
    )
    snapshot = build_change_report_snapshot(
        state, root=root, item_ids=(state.item_id(item),), scope=IOScope.CHANGED)
    set_language("en")
    human = render_change_report_csv(snapshot, t).decode("utf-8-sig")
    assert human.count("Unknown value") >= 2
    assert "future_core_value" not in human and "future_source" not in human
