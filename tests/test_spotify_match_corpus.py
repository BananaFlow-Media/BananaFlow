from __future__ import annotations

import json
import random
from pathlib import Path

from core.spotify_match_scorer import (
    _rank,
    find_best_youtube_match,
    match_from_metadata,
    youtube_search_query_variants,
)
from utils.metadata_cleaner import clean_title_and_artist
from scripts.validate_spotify_matching import independent_recording_oracle


CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "spotify_match_corpus.json").read_text(
        encoding="utf-8"
    )
)


def _rank_case(case: dict, candidates: list | None = None):
    title, artist, duration = case["source"]
    matches = []
    for index, candidate in enumerate(candidates or case["candidates"]):
        candidate_title, channel, candidate_duration = candidate
        matches.append(match_from_metadata(
            url=f"https://youtube.test/candidate-{index}",
            title=title,
            artist=artist,
            duration_sec=duration,
            yt_title=candidate_title,
            yt_channel=channel,
            yt_duration_sec=candidate_duration,
        ))
    return _rank(matches)


def test_representative_recording_intent_corpus():
    assert len(CORPUS) >= 50
    for case in CORPUS:
        ranked = _rank_case(case)
        safe = [candidate for candidate in ranked if candidate.safe]
        if case["expected"] is None:
            assert not safe, case["name"]
        else:
            assert safe, case["name"]
            assert safe[0].youtube_title == case["expected"], case["name"]


def test_corpus_ranking_is_deterministic_across_result_order():
    rng = random.Random(20260728)
    for case in CORPUS:
        for _ in range(12):
            shuffled = list(case["candidates"])
            rng.shuffle(shuffled)
            safe = [candidate for candidate in _rank_case(case, shuffled) if candidate.safe]
            if case["expected"] is None:
                assert not safe, case["name"]
            else:
                assert safe[0].youtube_title == case["expected"], case["name"]


def test_decisive_flat_result_avoids_deep_extraction(monkeypatch):
    entries = [
        {"id": "right", "title": "Adele - Easy On Me", "channel": "Adele", "duration": 224},
        {"id": "cover", "title": "Easy On Me (Cover)", "channel": "Covers", "duration": 224},
    ]
    deep_calls = []
    monkeypatch.setattr("core.spotify_match_scorer._search", lambda *a, **k: entries)
    monkeypatch.setattr(
        "core.spotify_match_scorer._deep_validate_urls",
        lambda urls, **kwargs: deep_calls.append(list(urls)) or [],
    )
    paths = []
    result = find_best_youtube_match("Easy On Me", "Adele", 224, path_observer=paths.append)
    assert result and result.youtube_title == "Adele - Easy On Me"
    assert deep_calls == []
    assert paths == ["flat"]


def test_ambiguous_flat_search_deep_validates_at_most_three(monkeypatch):
    entries = [
        {"id": str(index), "title": f"Artist - Song {index}", "channel": "Artist"}
        for index in range(8)
    ]
    seen = []
    monkeypatch.setattr("core.spotify_match_scorer._search", lambda *a, **k: entries)
    monkeypatch.setattr(
        "core.spotify_match_scorer._deep_validate_urls",
        lambda urls, **kwargs: seen.extend(urls) or [],
    )
    paths = []
    find_best_youtube_match("Song", "Artist", 200, path_observer=paths.append)
    assert len(seen) <= 3
    assert "deep_validation" in paths


def test_search_query_variants_keep_artist_and_title_identity():
    variants = youtube_search_query_variants(
        "HO’ADERES—VEHAMUNAH!", "Ruvi New", "Faith: Collection",
    )

    assert variants == (
        "Ruvi New HO’ADERES—VEHAMUNAH!",
        "Ruvi New HO ADERES VEHAMUNAH",
        "HO ADERES VEHAMUNAH Ruvi New",
        "Ruvi New HO ADERES VEHAMUNAH Faith Collection",
    )
    assert all("Ruvi New" in query for query in variants)
    assert all("HO" in query and "VEHAMUNAH" in query for query in variants)
    assert all("audio" not in query.casefold() for query in variants)


def test_empty_primary_search_uses_normalized_variant_and_scores_result(monkeypatch):
    queries = []

    def fake_search(query, **_kwargs):
        queries.append(query)
        if query == "ytsearch8:Ruvi New HO ADERES VEHAMUNAH":
            return [{
                "id": "right",
                "title": "Ruvi New - HO'ADERES VEHAMUNAH",
                "channel": "Ruvi New - Topic",
                "artists": [{"name": "Ruvi New"}],
                "duration": 203,
            }]
        return []

    monkeypatch.setattr("core.spotify_match_scorer._search", fake_search)
    monkeypatch.setattr(
        "core.spotify_match_scorer._deep_validate_urls", lambda *_a, **_k: [],
    )

    result = find_best_youtube_match(
        "HO’ADERES—VEHAMUNAH!", "Ruvi New", 203,
    )

    assert result is not None
    assert result.url.endswith("right")
    assert result.breakdown["resolution_path"] == "flat_variants"
    assert queries == [
        "ytsearch8:Ruvi New HO’ADERES—VEHAMUNAH!",
        "ytsearch8:Ruvi New HO ADERES VEHAMUNAH",
    ]


def test_decisive_primary_search_does_not_issue_extra_variants(monkeypatch):
    queries = []
    entries = [{
        "id": "right",
        "title": "Adele - Easy On Me",
        "channel": "Adele - Topic",
        "artists": [{"name": "Adele"}],
        "duration": 224,
    }]
    monkeypatch.setattr(
        "core.spotify_match_scorer._search",
        lambda query, **_kwargs: queries.append(query) or entries,
    )

    result = find_best_youtube_match("Easy On Me", "Adele", 224)

    assert result is not None
    assert queries == ["ytsearch8:Adele Easy On Me"]


def test_metadata_cleaner_preserves_recording_version_markers():
    title, artist = clean_title_and_artist(
        "Fleetwood Mac - Dreams (2004 Remaster)", "Fleetwood Mac"
    )
    assert title == "Dreams (2004 Remaster)"
    assert artist == "Fleetwood Mac"


def test_independent_live_oracle_accepts_descriptive_parentheticals():
    assert independent_recording_oracle(
        "Summertime Sadness (Cedric Gervais Remix)", "Lana Del Rey", 214,
        "Summertime Sadness (Lana Del Rey Vs. Cedric Gervais) (Cedric Gervais Remix)",
        ["Lana Del Rey"], 215,
    )


def test_independent_live_oracle_rejects_wrong_artist_even_at_same_duration():
    assert not independent_recording_oracle(
        "Stay", "Rihanna", 240, "Stay", ["Random Covers"], 240,
    )
