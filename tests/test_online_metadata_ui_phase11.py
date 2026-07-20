from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.metadata_lookup import (
    ArtworkCandidate, ArtworkRequest, ArtworkResult, FieldDifference, FieldComparison, LocalTrackSnapshot, LookupMode,
    LookupRequest, LookupResult, LookupState, MatchPreview, MetadataCandidate,
    ProviderAttribution,
)
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState
from ui.controllers.metadata_controller import MetadataController
from core.change_drafts import DraftStore
from core.metadata_match_service import MetadataMatchService
from core.metadata_lookup import AcceptedFieldSelection
from ui.i18n import set_language
from ui.panels.metadata_editor.online_metadata_dialog import OnlineMetadataDialog
from ui.panels.metadata_editor.panel import MetadataEditorPanel


def app():
    return QApplication.instance() or QApplication([])


def workspace(tmp_path):
    item = AudioTrackItem(tmp_path / "track.mp3", tmp_path, ".mp3",
                          original=OriginalTags(title="Song", artist="", album="Album"))
    state = TagEditorWorkspaceState(); state.set_tracks([item]); state.set_selected_items([item])
    return state, item, state.item_id(item)


def candidate():
    attr = ProviderAttribution("musicbrainz", "key", "MusicBrainz", "https://musicbrainz.org")
    return MetadataCandidate("musicbrainz", "c", recording_id="r", release_id="rel",
                             title="Song", artist="Artist", album="Album", score=98,
                             attribution=attr, source_url="https://musicbrainz.org/recording/r")


def request(state, identity):
    local = LocalTrackSnapshot(identity, "Song", "", "Album")
    return LookupRequest("req", state.generation, state.change_set.revision, (identity,),
                         "musicbrainz", LookupMode.TRACK, "Song", "", "Album", (local,))


def test_dialog_is_explicit_search_only_and_exposes_accessible_scope(tmp_path):
    app(); state, _item, identity = workspace(tmp_path); calls = []
    dialog = OnlineMetadataDialog(state, (identity,), search=calls.append, cancel=lambda: calls.append("cancel"),
                                  preview=lambda value: None, artwork=lambda value: None, accept=lambda value: None)
    assert calls == [] and "1" in dialog.scope_label.text()
    assert dialog.search_button.accessibleName() and dialog.candidates.accessibleName()
    dialog.search_button.click()
    assert calls and calls[0]["item_ids"] == (identity,) and calls[0]["title"] == "Song"
    dialog.reject()


def test_candidate_comparison_recommended_clear_and_final_accept(tmp_path):
    app(); state, _item, identity = workspace(tmp_path); previews=[]; accepted=[]
    dialog = OnlineMetadataDialog(state, (identity,), search=lambda value: None, cancel=lambda: None,
                                  preview=previews.append, artwork=lambda value: None, accept=accepted.append)
    req = request(state, identity); cand = candidate()
    dialog.on_lookup_result(LookupResult(req, LookupState.READY, (cand,)))
    assert previews == [cand] and dialog.candidates.count() == 1
    preview = MatchPreview(req, cand, (
        FieldComparison(identity, "title", "Song", "Song", FieldDifference.NO_OP, False),
        FieldComparison(identity, "artist", "", "Artist", FieldDifference.CHANGE, True),
        FieldComparison(identity, "album", "Album", "Other", FieldDifference.CHANGE, False),
    ))
    dialog.on_match_preview(preview); dialog.recommended.click()
    assert dialog.comparison.item(0, 0).checkState() == Qt.CheckState.Unchecked
    assert dialog.comparison.item(1, 0).checkState() == Qt.CheckState.Checked
    assert dialog.comparison.item(2, 0).checkState() == Qt.CheckState.Unchecked
    dialog.add_button.click()
    assert accepted and accepted[0][1].selected == frozenset({(identity, "artist")})
    dialog.clear_selection.click(); assert dialog.comparison.item(1, 0).checkState() == Qt.CheckState.Unchecked


def test_no_results_offline_rate_limit_cancel_and_error_are_localized(tmp_path):
    app(); state, _item, identity = workspace(tmp_path); dialog = OnlineMetadataDialog(
        state, (identity,), search=lambda value: None, cancel=lambda: None,
        preview=lambda value: None, artwork=lambda value: None, accept=lambda value: None)
    req = request(state, identity)
    for lookup_state in (LookupState.NO_RESULTS, LookupState.OFFLINE, LookupState.RATE_LIMITED, LookupState.CANCELLED, LookupState.ERROR):
        dialog.on_lookup_result(LookupResult(req, lookup_state))
        assert dialog.state.text() and "{" not in dialog.state.text() and "json" not in dialog.state.text().casefold()


def test_candidate_switch_resets_artwork_and_unsupported_row_is_disabled(tmp_path):
    app(); state, _item, identity = workspace(tmp_path); previews=[]
    dialog = OnlineMetadataDialog(state, (identity,), search=lambda value: None, cancel=lambda: None,
        preview=previews.append, artwork=lambda value: None, accept=lambda value: None)
    req = request(state, identity); first = candidate()
    second = MetadataCandidate(**{**first.__dict__, "candidate_id": "b", "release_id": "rel-b"})
    dialog.on_lookup_result(LookupResult(req, LookupState.READY, (first, second)))
    dialog._artwork_entry = object(); dialog.artwork_use.setEnabled(True); dialog.artwork_use.setChecked(True)
    dialog.candidates.setCurrentRow(1)
    assert dialog._artwork_entry is None and not dialog.artwork_use.isEnabled() and not dialog.artwork_use.isChecked()
    preview = MatchPreview(req, second, (
        FieldComparison(identity, "artist", "", "Artist", FieldDifference.UNSUPPORTED, False),
    ))
    dialog.on_match_preview(preview)
    check = dialog.comparison.item(0, 0)
    assert not bool(check.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert not dialog.add_button.isEnabled()


def test_hebrew_strings_and_ltr_search_fields(tmp_path):
    app(); set_language("he")
    try:
        state, _item, identity = workspace(tmp_path); dialog = OnlineMetadataDialog(
            state, (identity,), search=lambda value: None, cancel=lambda: None,
            preview=lambda value: None, artwork=lambda value: None, accept=lambda value: None)
        assert "מטא" in dialog.windowTitle()
        assert dialog.title_edit.layoutDirection() == Qt.LayoutDirection.LeftToRight
    finally:
        set_language("en")


def test_production_inspector_opens_dialog_without_network_until_search(tmp_path):
    app(); panel = MetadataEditorPanel(); state, item, identity = workspace(tmp_path); panel.set_workspace_state(state)
    calls=[]; panel.online_search_requested.connect(calls.append)
    panel._on_online_metadata()
    dialog = panel._online_dialog
    assert dialog is not None and calls == []
    dialog.search_button.click(); assert calls and calls[0]["item_ids"] == (identity,)
    dialog.reject(); panel.close()


def test_controller_acceptance_emits_once_schedules_one_draft_and_rejects_stale(tmp_path):
    app(); controller = MetadataController(); controller._draft_store = DraftStore(tmp_path / "draft.json")
    item = AudioTrackItem(tmp_path / "track.mp3", tmp_path, ".mp3", original=OriginalTags(title="Song", artist=""))
    controller.workspace_state.set_tracks([item]); controller.workspace_state.set_selected_items([item])
    identity = controller.workspace_state.item_id(item)
    req = request(controller.workspace_state, identity); controller._online_request = req
    controller._online_selected_candidate = candidate()
    preview = MetadataMatchService().preview(req, controller._online_selected_candidate)
    notifications=[]; controller.tags_modified.connect(lambda: notifications.append(True))
    assert controller.accept_online_match((preview, AcceptedFieldSelection(frozenset({(identity, "artist")}))))
    assert notifications == [True] and controller._draft_timer.isActive()
    assert item.proposed.artist == "Artist" and len(controller.workspace_state.change_set.records()) == 1
    assert controller.workspace_state.undo_proposals() and item.proposed.artist is None
    controller._draft_timer.stop()

    # A proposal revision created after the lookup invalidates the preview.
    item.proposed.album = "Later"; controller.workspace_state.capture_proposals([item])
    assert not controller.accept_online_match((preview, AcceptedFieldSelection(frozenset({(identity, "artist")}))))
    assert item.proposed.artist is None


def test_late_artwork_result_from_candidate_a_cannot_update_b(tmp_path):
    app(); controller = MetadataController()
    item = AudioTrackItem(tmp_path / "track.mp3", tmp_path, ".mp3", original=OriginalTags(title="Song"))
    controller.workspace_state.set_tracks([item]); controller.workspace_state.set_selected_items([item])
    identity = controller.workspace_state.item_id(item); req = request(controller.workspace_state, identity)
    cand_a = candidate(); cand_b = MetadataCandidate(**{**cand_a.__dict__, "candidate_id": "b", "release_id": "rel-b"})
    attr = cand_a.attribution
    art_a_req = ArtworkRequest("art-a", req.request_id, req.workspace_generation, req.change_revision,
                               req.item_ids, cand_a.candidate_id, cand_a.release_id)
    art_b_req = ArtworkRequest("art-b", req.request_id, req.workspace_generation, req.change_revision,
                               req.item_ids, cand_b.candidate_id, cand_b.release_id)
    art_a = ArtworkCandidate("cover_art_archive", "rel", "a", ("Front",), True, False,
        "image/png", "thumb-a", "full-a", "source-a", attr)
    art_b = ArtworkCandidate("cover_art_archive", "rel-b", "b", ("Front",), True, False,
        "image/png", "thumb-b", "full-b", "source-b", attr)
    from ui.workers.metadata_lookup_worker import ArtworkLookupWorker
    worker_a = ArtworkLookupWorker(object(), art_a_req, parent=controller)
    worker_b = ArtworkLookupWorker(object(), art_b_req, parent=controller)
    worker_a.result_ready.connect(controller._on_online_artwork_result)
    worker_b.result_ready.connect(controller._on_online_artwork_result)
    controller._online_request = req; controller._online_selected_candidate = cand_b
    controller._online_artwork_request = art_b_req; controller._online_artwork_worker = worker_b
    worker_a.result_ready.emit(ArtworkResult(art_a_req, LookupState.READY, (art_a,), art_a, b"not-b"))
    assert controller._online_artwork_entry is None
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    worker_b.result_ready.emit(ArtworkResult(art_b_req, LookupState.READY, (art_b,), art_b, png))
    assert controller._online_artwork_candidate == art_b
