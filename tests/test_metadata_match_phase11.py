from __future__ import annotations

import base64
from pathlib import Path

from core.artwork import validate_artwork_bytes
from core.metadata_lookup import (
    AcceptedFieldSelection, AlbumMappingState, FieldDifference, LocalTrackSnapshot,
    LookupMode, LookupRequest, MetadataCandidate, ProviderAttribution, ReleaseTrack,
)
from core.metadata_match_service import MetadataMatchService
from core.metadata_models import AudioTrackItem, OriginalTags
from ui.controllers.tag_editor_workspace_state import TagEditorWorkspaceState


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
ATTR = ProviderAttribution("musicbrainz", "key", "MusicBrainz", "https://musicbrainz.org")


def req(local, mode=LookupMode.TRACK, revision=0):
    return LookupRequest("id", 1, revision, tuple(row.item_id for row in local), "musicbrainz", mode,
                         local[0].title if local else "", local[0].artist if local else "",
                         local[0].album if local else "", tuple(local))


def candidate(**kwargs):
    values = dict(provider_id="musicbrainz", candidate_id="c", recording_id="r", release_id="rel",
                  title="Song", artist="Artist", album="Album", duration_ms=180_000,
                  attribution=ATTR, source_url="https://musicbrainz.org/recording/r")
    values.update(kwargs); return MetadataCandidate(**values)


def test_exact_title_artist_scores_above_fuzzy_and_duration_breaks_preference():
    local = (LocalTrackSnapshot(1, "Song", "Artist", "Album", duration_ms=180_000),)
    service = MetadataMatchService(); request = req(local)
    exact = service.score_candidate(request, candidate(candidate_id="exact"))
    fuzzy = service.score_candidate(request, candidate(candidate_id="fuzzy", title="Other", duration_ms=180_000))
    long = service.score_candidate(request, candidate(candidate_id="long", duration_ms=260_000))
    assert exact.score > fuzzy.score and exact.score > long.score
    assert {row.component for row in exact.evidence} >= {"title", "artist", "album", "duration"}


def test_album_preference_and_deterministic_tie_order():
    local = (LocalTrackSnapshot(1, "Song", "Artist", "Wanted"),); request = req(local)
    ranked = MetadataMatchService().rank(request, [
        candidate(candidate_id="z", recording_id="z", album="Other"),
        candidate(candidate_id="b", recording_id="b", album="Wanted"),
        candidate(candidate_id="a", recording_id="a", album="Wanted"),
    ])
    assert [row.recording_id for row in ranked] == ["a", "b", "z"]


def test_recommended_fields_only_fill_empty_local_values_and_never_auto_accept():
    local = (LocalTrackSnapshot(1, "Stored", "", "Local album", date="", isrc=""),)
    preview = MetadataMatchService().preview(req(local), candidate(title="Online", date="2024", isrc="CODE"))
    by_field = {row.field: row for row in preview.comparisons}
    assert by_field["title"].difference is FieldDifference.CHANGE and not by_field["title"].recommended
    assert by_field["artist"].recommended and by_field["year"].recommended and by_field["isrc"].recommended
    assert all(row.difference is not FieldDifference.AMBIGUOUS for row in preview.comparisons)


def test_album_mapping_matches_unique_tracks_and_marks_unmatched_and_ambiguous():
    tracks = (
        ReleaseTrack(title="One", artist="Artist", track_num=1, duration_ms=100_000),
        ReleaseTrack(title="Same", artist="Artist", track_num=2, duration_ms=110_000),
        ReleaseTrack(title="Same", artist="Artist", track_num=3, duration_ms=110_000),
    )
    local = (
        LocalTrackSnapshot(1, "One", "Artist", track_num=1, duration_ms=100_000),
        LocalTrackSnapshot(2, "Same", "Artist", duration_ms=110_000),
        LocalTrackSnapshot(3, "Unrelated", "Nobody", duration_ms=900_000),
    )
    mapped = MetadataMatchService().map_album(local, tracks)
    assert mapped[0].state is AlbumMappingState.MATCHED
    assert mapped[1].state is AlbumMappingState.AMBIGUOUS
    assert mapped[2].state is AlbumMappingState.UNMATCHED


def workspace(tmp_path):
    item = AudioTrackItem(tmp_path / "song.mp3", tmp_path, ".mp3",
                          original=OriginalTags(title="Stored", artist="", album="Local", year="", isrc=""))
    state = TagEditorWorkspaceState(); state.set_tracks([item]); state.set_selected_items([item])
    return state, item


def request_for_workspace(state, item):
    identity = state.item_id(item); tags = item.proposed.effective_tags(item.original)
    local = LocalTrackSnapshot(identity, tags.title, str(tags.artist), tags.album, str(tags.album_artist),
                               tags.track_num, tags.track_total, tags.disc_num, tags.disc_total,
                               tags.year, str(tags.genre), tags.isrc, tags.publisher)
    return LookupRequest("accept", state.generation, state.change_set.revision, (identity,), "musicbrainz",
                         LookupMode.TRACK, tags.title, str(tags.artist), tags.album, (local,))


def test_selected_fields_create_one_origin_attributed_command_preserve_exclusion_and_no_disk_write(tmp_path, monkeypatch):
    state, item = workspace(tmp_path); state.set_apply_excluded_ids([], True)
    request = request_for_workspace(state, item); service = MetadataMatchService()
    preview = service.preview(request, candidate(title="Online", artist="Artist", album="Online album", date="2024", isrc="CODE"))
    identity = state.item_id(item); selection = AcceptedFieldSelection(frozenset({(identity, "artist"), (identity, "year"), (identity, "isrc")}))
    monkeypatch.setattr("core.metadata_processor.write_tags", lambda *a, **k: (_ for _ in ()).throw(AssertionError("disk write")))
    assert service.accept(state, preview, selection)
    assert item.proposed.artist == "Artist" and item.proposed.year == "2024" and item.proposed.isrc == "CODE"
    assert item.proposed.title is None and item.proposed.album is None
    records = state.change_set.records(); assert len(records) == 3
    assert {row.source_provider for row in records} == {"musicbrainz"}
    assert all(row.source_attribution == "MusicBrainz" and row.source_url for row in records)
    assert state.can_undo_proposals() and state.undo_proposals()
    assert item.proposed.artist is None and state.redo_proposals() and item.proposed.artist == "Artist"


def test_stale_revision_scope_and_workspace_reject_acceptance(tmp_path):
    state, item = workspace(tmp_path); request = request_for_workspace(state, item); service = MetadataMatchService()
    preview = service.preview(request, candidate(artist="Artist")); selection = AcceptedFieldSelection(frozenset({(state.item_id(item), "artist")}))
    item.proposed.album = "changed"; state.capture_proposals([item])
    assert not service.accept(state, preview, selection) and item.proposed.artist is None


def test_artwork_acceptance_uses_existing_proposal_and_is_undoable(tmp_path):
    state, item = workspace(tmp_path); request = request_for_workspace(state, item); service = MetadataMatchService()
    preview = service.preview(request, candidate()); identity = state.item_id(item)
    entry = validate_artwork_bytes(PNG)
    assert service.accept(state, preview, AcceptedFieldSelection(artwork_item_ids=frozenset({identity})), artwork_entry=entry)
    assert item.proposed.effective_tags(item.original).artwork.primary.content_hash == entry.content_hash
    assert state.change_set.records()[0].field == "artwork"
    assert state.undo_proposals() and not item.proposed.effective_tags(item.original).artwork.entries


def test_unsupported_capabilities_are_visible_not_recommended_and_rejected(tmp_path):
    local = (LocalTrackSnapshot(1, "Song", "", "Album", format_id="aac",
                                metadata_editable=False, editable_fields=frozenset()),)
    request = req(local); service = MetadataMatchService()
    preview = service.preview(request, candidate(artist="Artist"))
    artist = next(row for row in preview.comparisons if row.field == "artist")
    assert artist.difference is FieldDifference.UNSUPPORTED and not artist.recommended
    state, item = workspace(tmp_path)
    identity = state.item_id(item)
    local2 = (LocalTrackSnapshot(identity, "Stored", "", "Local", format_id="aac",
                                 metadata_editable=False, editable_fields=frozenset()),)
    blocked = LookupRequest("blocked", state.generation, state.change_set.revision, (identity,),
        "musicbrainz", LookupMode.TRACK, "Stored", "", "Local", local2)
    blocked_preview = service.preview(blocked, candidate(artist="Artist"))
    forced = AcceptedFieldSelection(frozenset({(identity, "artist")}))
    assert not service.accept(state, blocked_preview, forced)
    assert item.proposed.artist is None and not state.change_set.records()


def test_mixed_format_artwork_capability_is_per_file():
    metadata_fields = frozenset({"title", "artist", "album"})
    local = (
        LocalTrackSnapshot(1, "Song", "Artist", "Album", format_id="mp3", metadata_editable=True,
                           editable_fields=metadata_fields | {"artwork"}),
        LocalTrackSnapshot(2, "Song", "Artist", "Album", format_id="opus", metadata_editable=True,
                           editable_fields=metadata_fields),
    )
    request = LookupRequest("mixed", 1, 0, (1, 2), "musicbrainz", LookupMode.TRACK,
                            "Song", "Artist", "Album", local)
    preview = MetadataMatchService().preview(request, candidate())
    assert preview.artwork_supported_item_ids == (1,)


def test_mixed_metadata_and_caa_artwork_keep_field_sources_in_one_undo(tmp_path):
    from dataclasses import replace
    state, item = workspace(tmp_path); request = request_for_workspace(state, item)
    service = MetadataMatchService(); preview = service.preview(request, candidate(artist="Artist"))
    identity = state.item_id(item)
    entry = replace(validate_artwork_bytes(PNG), source_id="https://coverartarchive.org/release/rel/1")
    selection = AcceptedFieldSelection(frozenset({(identity, "artist")}), frozenset({identity}))
    assert service.accept(state, preview, selection, artwork_entry=entry)
    records = {record.field: record for record in state.change_set.records()}
    assert records["artist"].source_provider == "musicbrainz"
    assert records["artwork"].source_provider == "cover_art_archive"
    assert records["artwork"].source_url.endswith("/rel/1")
    assert len(state.proposal_history._undo) == 1
    assert state.undo_proposals() and item.proposed.artist is None
