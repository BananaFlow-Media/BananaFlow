"""Deterministic candidate scoring, comparison and proposal acceptance."""
from __future__ import annotations

from dataclasses import replace
import re
import unicodedata
from difflib import SequenceMatcher

from core.change_sets import ChangeOrigin
from core.metadata_lookup import (
    AcceptedFieldSelection, AlbumMappingState, AlbumTrackMapping, FieldComparison,
    FieldDifference, LocalTrackSnapshot, LookupMode, LookupRequest, MatchPreview,
    MetadataCandidate, ReleaseTrack, ScoreEvidence,
)
from core.metadata_models import ArtworkEntry, metadata_values_equal


_FIELD_ORDER = (
    "title", "artist", "album", "album_artist", "track_num", "track_total",
    "disc_num", "disc_total", "year", "genre", "isrc", "publisher",
)


def normalize_match_text(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _similarity(left: object, right: object) -> float:
    a, b = normalize_match_text(left), normalize_match_text(right)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def _duration_similarity(left: int | None, right: int | None) -> tuple[float, str]:
    if left is None or right is None:
        return 0.0, "unavailable"
    diff = abs(left - right)
    if diff <= 2_000: value = 1.0
    elif diff <= 5_000: value = 0.8
    elif diff <= 10_000: value = 0.45
    elif diff <= 30_000: value = 0.1
    else: value = 0.0
    return value, f"difference_ms={diff}"


class MetadataMatchService:
    WEIGHTS = {"title": 32, "artist": 28, "album": 16, "duration": 12, "position": 7, "date": 5}

    def score_candidate(self, request: LookupRequest, candidate: MetadataCandidate) -> MetadataCandidate:
        local = request.local_tracks[0] if request.local_tracks else LocalTrackSnapshot(0)
        evidence: list[ScoreEvidence] = []
        weighted = available = 0.0

        def add(component: str, left, right, *, similarity: float | None = None, detail: str = ""):
            nonlocal weighted, available
            if left in (None, "") or right in (None, ""):
                return
            sim = _similarity(left, right) if similarity is None else similarity
            weight = self.WEIGHTS[component]
            weighted += weight * sim; available += weight
            evidence.append(ScoreEvidence(component, weight, round(sim, 4), detail or f"{left!s} ↔ {right!s}"))

        add("title", request.title or local.title, candidate.title)
        # A release candidate's primary artist is represented by album_artist.
        artist_value = candidate.album_artist if request.mode is LookupMode.ALBUM else candidate.artist
        add("artist", request.artist or local.artist, artist_value or candidate.artist)
        add("album", request.album or local.album, candidate.album)
        if local.duration_ms is not None and candidate.duration_ms is not None:
            similarity, detail = _duration_similarity(local.duration_ms, candidate.duration_ms)
            add("duration", local.duration_ms, candidate.duration_ms, similarity=similarity, detail=detail)
        if local.track_num is not None and candidate.track_num is not None:
            add("position", local.track_num, candidate.track_num,
                similarity=1.0 if local.track_num == candidate.track_num else 0.0,
                detail=f"track={local.track_num}/{candidate.track_num}")
        local_date = local.date[:4] if local.date else ""
        online_date = candidate.date[:4] if candidate.date else ""
        add("date", local_date, online_date)
        score = round((weighted / available) * 100.0, 2) if available else 0.0
        return replace(candidate, score=score, evidence=tuple(evidence))

    def rank(self, request: LookupRequest, candidates) -> tuple[MetadataCandidate, ...]:
        scored = [self.score_candidate(request, candidate) for candidate in candidates]
        return tuple(sorted(scored, key=lambda c: (-c.score, c.provider_id, c.recording_id, c.release_id, c.candidate_id)))

    def preview(self, request: LookupRequest, candidate: MetadataCandidate) -> MatchPreview:
        mappings = self.map_album(request.local_tracks, candidate.tracks) if request.mode is LookupMode.ALBUM else ()
        comparisons: list[FieldComparison] = []
        mapping_by_id = {mapping.item_id: mapping for mapping in mappings}
        for local in request.local_tracks:
            track = mapping_by_id.get(local.item_id).track if mapping_by_id.get(local.item_id) else None
            if request.mode is LookupMode.ALBUM and track is None:
                continue
            online = {
                "title": track.title if track else candidate.title,
                "artist": track.artist if track and track.artist else candidate.artist,
                "album": candidate.album,
                "album_artist": candidate.album_artist,
                "track_num": track.track_num if track else candidate.track_num,
                "track_total": track.track_total if track else candidate.track_total,
                "disc_num": track.disc_num if track else candidate.disc_num,
                "disc_total": track.disc_total if track else candidate.disc_total,
                "year": candidate.date,
                "genre": candidate.genre,
                "isrc": candidate.isrc,
                "publisher": candidate.publisher,
            }
            local_values = {
                "title": local.title, "artist": local.artist, "album": local.album,
                "album_artist": local.album_artist, "track_num": local.track_num,
                "track_total": local.track_total, "disc_num": local.disc_num,
                "disc_total": local.disc_total, "year": local.date,
                "genre": local.genre, "isrc": local.isrc, "publisher": local.publisher,
            }
            for field_name in _FIELD_ORDER:
                value = online[field_name]
                if value in (None, ""):
                    difference = FieldDifference.EMPTY
                elif not local.supports(field_name):
                    difference = FieldDifference.UNSUPPORTED
                elif metadata_values_equal(field_name, local_values[field_name], value):
                    difference = FieldDifference.NO_OP
                else:
                    difference = FieldDifference.CHANGE
                comparisons.append(FieldComparison(
                    local.item_id, field_name, local_values[field_name], value, difference,
                    recommended=(difference is FieldDifference.CHANGE
                                 and local_values[field_name] in (None, "")),
                ))
        artwork_ids = tuple(local.item_id for local in request.local_tracks if local.supports("artwork"))
        return MatchPreview(request, candidate, tuple(comparisons), tuple(mappings), artwork_ids)

    def map_album(self, local_tracks: tuple[LocalTrackSnapshot, ...], release_tracks: tuple[ReleaseTrack, ...]) -> tuple[AlbumTrackMapping, ...]:
        used: set[int] = set(); results: list[AlbumTrackMapping] = []
        for local in sorted(local_tracks, key=lambda value: value.item_id):
            ranked: list[tuple[float, int, tuple[ScoreEvidence, ...]]] = []
            for index, track in enumerate(release_tracks):
                if index in used:
                    continue
                title = _similarity(local.title, track.title)
                artist = _similarity(local.artist, track.artist) if local.artist and track.artist else 0.5
                duration, detail = _duration_similarity(local.duration_ms, track.duration_ms)
                position = 1.0 if local.track_num is not None and local.track_num == track.track_num else 0.0
                score = title * 0.48 + artist * 0.24 + duration * 0.18 + position * 0.10
                evidence = (
                    ScoreEvidence("title", 48, title, "album track title"),
                    ScoreEvidence("artist", 24, artist, "album track artist"),
                    ScoreEvidence("duration", 18, duration, detail),
                    ScoreEvidence("position", 10, position, "album track position"),
                )
                ranked.append((score, index, evidence))
            ranked.sort(key=lambda value: (-value[0], value[1]))
            if not ranked or ranked[0][0] < 0.62:
                results.append(AlbumTrackMapping(local.item_id, AlbumMappingState.UNMATCHED))
            elif len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 0.035:
                results.append(AlbumTrackMapping(local.item_id, AlbumMappingState.AMBIGUOUS, evidence=ranked[0][2]))
            else:
                _score, index, evidence = ranked[0]; used.add(index)
                results.append(AlbumTrackMapping(local.item_id, AlbumMappingState.MATCHED, release_tracks[index], evidence))
        return tuple(results)

    def accept(self, workspace, preview: MatchPreview, selection: AcceptedFieldSelection,
               *, artwork_entry: ArtworkEntry | None = None) -> bool:
        request = preview.request
        if (workspace.generation != request.workspace_generation
                or workspace.change_set.revision != request.change_revision):
            return False
        current_ids = tuple(sorted(workspace.item_id(item) for item in workspace.selected_tracks()))
        if current_ids != request.item_ids:
            return False
        before = workspace.proposal_checkpoint()
        touched = []
        comparisons = {(row.item_id, row.field): row for row in preview.comparisons}
        for item_id, field_name in sorted(selection.selected):
            row = comparisons.get((item_id, field_name))
            item = workspace.track_for_id(item_id)
            if (row is None or item is None or row.difference is not FieldDifference.CHANGE
                    or not self._supports_snapshot(request, item_id, field_name)):
                continue
            value = row.online_value
            if field_name in {"track_num", "track_total", "disc_num", "disc_total"}:
                value = int(value)
            setattr(item.proposed, field_name, value)
            if item not in touched: touched.append(item)
        if artwork_entry is not None:
            for item_id in sorted(selection.artwork_item_ids):
                item = workspace.track_for_id(item_id)
                if item is None or not self._supports_snapshot(request, item_id, "artwork"):
                    continue
                item.proposed.set_artwork(artwork_entry, original=item.proposed.effective_tags(item.original).artwork)
                if item not in touched: touched.append(item)
        if not touched:
            return False
        attribution = preview.candidate.attribution
        source = None if attribution is None else {
            "provider": attribution.provider_id,
            "attribution": attribution.text,
            "url": preview.candidate.source_url or attribution.url,
        }
        field_sources = {}
        for item_id, field_name in selection.selected:
            if (item_id, field_name) in comparisons:
                field_sources[(item_id, field_name)] = source
        if artwork_entry is not None:
            artwork_source = {
                "provider": "cover_art_archive", "attribution": "Cover Art Archive",
                "url": getattr(artwork_entry, "source_id", "") or "https://coverartarchive.org/",
            }
            for item_id in selection.artwork_item_ids:
                field_sources[(item_id, "artwork")] = artwork_source
        workspace.capture_proposals(touched, ChangeOrigin.ONLINE_METADATA,
                                    label="online metadata", before=before,
                                    field_sources=field_sources)
        return True

    @staticmethod
    def _supports_snapshot(request: LookupRequest, item_id: int, field_name: str) -> bool:
        snapshot = next((value for value in request.local_tracks if value.item_id == item_id), None)
        return bool(snapshot and snapshot.supports(field_name))
