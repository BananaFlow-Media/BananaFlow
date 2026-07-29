"""
tests/test_spotify_match_scorer.py  –  Offline scoring tests
==============================================================
Tests only the pure scoring functions — no yt-dlp or network.
"""

from __future__ import annotations

import pytest

from core.spotify_match_scorer import (
    MatchResult,
    _artist_score,
    _channel_score,
    _duration_score,
    _normalize,
    _title_score,
    assess_candidate,
    parse_artist_credits,
    score_candidate,
)


class TestNormalize:
    def test_strips_official_audio(self):
        assert "sample song" in _normalize("Sample Song (Official Audio)")

    def test_strips_lyrics(self):
        assert "song" in _normalize("Song [Lyrics]")

    def test_strips_feat(self):
        assert "feat" not in _normalize("Song (feat. Someone)")

    def test_collapses_whitespace(self):
        assert "  " not in _normalize("hello   world")


class TestTitleScore:
    def test_exact_match(self):
        s = _title_score("Sample Song", "Sample Song")
        assert s == 40.0

    def test_with_noise(self):
        s = _title_score("Sample Song", "Sample Song (Official Audio)")
        assert s >= 35.0  # after normalization, nearly identical

    def test_totally_different(self):
        s = _title_score("Sample Song", "Unrelated Track Title")
        assert s < 15.0


class TestDurationScore:
    def test_exact_match(self):
        assert _duration_score(369, 369) == 30.0

    def test_within_tolerance(self):
        assert _duration_score(369, 371) == 30.0  # ±3s

    def test_moderate_diff(self):
        s = _duration_score(369, 379)  # 10s off
        assert 0 < s < 30

    def test_large_diff(self):
        assert _duration_score(369, 400) == 0.0  # >15s

    def test_unknown_duration(self):
        s = _duration_score(369, None)
        assert s == 30.0 * 0.3  # partial credit

    def test_both_unknown(self):
        s = _duration_score(None, None)
        assert s == 30.0 * 0.3


class TestArtistScore:
    def test_in_both(self):
        s = _artist_score("Sample Artist", "Sample Artist - Sample Song", "Sample Artist")
        assert s == 20.0

    def test_in_title_only(self):
        s = _artist_score("Sample Artist", "Sample Artist - Sample Song", "SomeChannel")
        assert s == 16.0  # 80% (in title but not channel)

    def test_not_present(self):
        s = _artist_score("Sample Artist", "Sample Song", "SomeChannel")
        assert s < 10.0

    def test_empty_artist(self):
        assert _artist_score("", "Sample Song", "Channel") == 0.0


class TestArtistCredits:
    @pytest.mark.parametrize(
        ("raw", "primary", "credited"),
        [
            ("Lady Gaga, Bradley Cooper", "lady gaga", ("bradley cooper",)),
            ("Simon & Garfunkel", "simon", ("garfunkel",)),
            ("Disclosure feat. Sam Smith", "disclosure", ("sam smith",)),
            ("עידן רייכל x ריטה", "עידן רייכל", ("ריטה",)),
            ("Artist One / Artist Two; Artist Three", "artist one", ("artist two", "artist three")),
        ],
    )
    def test_raw_separators_are_parsed_before_punctuation_folding(
        self, raw, primary, credited,
    ):
        parsed = parse_artist_credits(raw)
        assert parsed.primary == primary
        assert parsed.credited == credited

    @pytest.mark.parametrize("raw", ["X Ambassadors", "AC/DC", "The xx"])
    def test_short_separator_like_artist_names_remain_whole(self, raw):
        parsed = parse_artist_credits(raw)
        assert parsed.primary == _normalize(raw)
        assert parsed.credited == ()


class TestChannelScore:
    def test_branded_channel(self):
        s = _channel_score("SampleArtistOfficial", "Sample Artist")
        assert s > 0

    def test_topic(self):
        s = _channel_score("Sample Artist - Topic", "Sample Artist")
        assert s > 0

    def test_official(self):
        s = _channel_score("Sample Artist Official", "Sample Artist")
        assert s > 0

    def test_generic_channel(self):
        s = _channel_score("RandomUploader", "Sample Artist")
        assert s == 0.0


class TestScoreCandidate:
    def test_perfect_match(self):
        total, bd = score_candidate(
            "Sample Song", "Sample Artist", 369,
            "Sample Artist - Sample Song (Official Audio)", "SampleArtistOfficial", 369,
        )
        assert total >= 80.0
        assert bd["title"] > 30
        assert bd["duration"] == 30.0

    def test_wrong_track(self):
        total, bd = score_candidate(
            "Sample Song", "Sample Artist", 369,
            "Completely Different Title", "OtherArtistChannel", 354,
        )
        assert total < 30.0

    def test_right_track_wrong_duration(self):
        total, _ = score_candidate(
            "Sample Song", "Sample Artist", 369,
            "Sample Artist - Sample Song (Extended Mix)", "SampleArtistOfficial", 480,
        )
        # Good title/artist but duration kills it
        assert total < 70.0

    def test_structured_artist_credit_overrides_title_name_drop(self):
        _score, breakdown, safe = assess_candidate(
            "Shallow", "Lady Gaga, Bradley Cooper", 215,
            "Shallow (Lady Gaga & Bradley Cooper)", "Rodrigues", 222,
            yt_artists=["Fabio Rodrigues"],
        )
        assert safe is False
        assert "artist" in breakdown["reject_reasons"]
        assert breakdown["structured_artist_evidence"] == 0.0

    def test_specific_remaster_year_must_match(self):
        _score, breakdown, safe = assess_candidate(
            "Dreams - 2004 Remaster", "Fleetwood Mac", 257,
            "Dreams (2002 Remaster)", "Fleetwood Mac", 256,
            yt_artists=["Fleetwood Mac"],
        )
        assert safe is False
        assert "version_qualifier" in breakdown["reject_reasons"]

    @pytest.mark.parametrize(
        ("candidate_title", "channel"),
        [
            ("Adele - Easy On Me", "Bedroom Singer"),
            ("Easy On Me - Adele tribute performance", "Local Theatre"),
            ("Adele Easy On Me", "Adele Fan Archive"),
            ("Adele - Easy On Me Karaoke", "Sing Along Now"),
            ("ORIGINAL Adele Easy On Me", "Unrelated Performer"),
        ],
    )
    def test_title_name_drop_is_not_independent_performer_proof(
        self, candidate_title, channel,
    ):
        _score, breakdown, safe = assess_candidate(
            "Easy On Me", "Adele", 224,
            candidate_title, channel, 224,
        )
        assert safe is False
        assert "artist_source" in breakdown["reject_reasons"]
        assert breakdown["artist_proof"] == "none"

    def test_nearly_identical_duration_does_not_rescue_unlabelled_cover(self):
        _score, breakdown, safe = assess_candidate(
            "Shallow", "Lady Gaga, Bradley Cooper", 215,
            "Lady Gaga & Bradley Cooper - Shallow", "Acoustic Sessions", 216,
        )
        assert safe is False
        assert breakdown["duration_delta"] == 1
        assert "artist_source" in breakdown["reject_reasons"]

    def test_matching_channel_is_independent_performer_evidence(self):
        _score, breakdown, safe = assess_candidate(
            "Easy On Me", "Adele", 224,
            "Adele - Easy On Me", "Adele - Topic", 224,
        )
        assert safe is True
        assert breakdown["artist_proof"] == "channel"

    def test_structured_collaboration_requires_every_credited_artist(self):
        _score, breakdown, safe = assess_candidate(
            "Shallow", "Lady Gaga, Bradley Cooper", 215,
            "Shallow", "Lady Gaga", 215,
            yt_artists=["Lady Gaga"],
        )
        assert safe is False
        assert "credited_artist" in breakdown["reject_reasons"]


class TestResolveToYtmUrl:
    @pytest.fixture
    def mock_ytmusic(self, monkeypatch):
        class MockYTMusic:
            def search(self, query, filter=None, limit=5):
                if "Nonexistent" in query:
                    return []
                return [
                    {
                        "videoId": "good_vid",
                        "title": "Easy On Me",
                        "artists": [{"name": "Adele"}],
                        "duration_seconds": 224
                    },
                    {
                        "videoId": "cover_vid",
                        "title": "Easy On Me (Cover)",
                        "artists": [{"name": "Cover Artist"}],
                        "duration_seconds": 224
                    }
                ]
        monkeypatch.setattr("ytmusicapi.YTMusic", MockYTMusic)

    def test_resolve_strong_ytm_match(self, mock_ytmusic, monkeypatch):
        from core.scraper import _resolve_to_ytm_url
        url = _resolve_to_ytm_url("Easy On Me", "Adele", 224)
        assert url == "https://music.youtube.com/watch?v=good_vid"

    def test_resolve_fallback_to_yt_general(self, mock_ytmusic, monkeypatch):
        from core.scraper import _resolve_to_ytm_url
        from core.spotify_match_scorer import MatchResult

        called_fallback = []
        def mock_find_best(
            title, artist, duration_sec, min_confidence=0.55,
            cookies_file=None, **_kwargs,
        ):
            called_fallback.append(True)
            return MatchResult(
                url="https://www.youtube.com/watch?v=fallback_vid",
                youtube_title="Adele - Easy On Me",
                channel="AdeleOfficial",
                duration_sec=224,
                score=90.0,
                confidence=0.90,
                breakdown={}
            )
        monkeypatch.setattr("core.spotify_match_scorer.find_best_youtube_match", mock_find_best)

        url = _resolve_to_ytm_url("Easy On Me", "Unrelated Artist", 224)
        assert url == "https://www.youtube.com/watch?v=fallback_vid"
        assert called_fallback == [True]

    def test_unproved_result_uses_final_legacy_search_request(self, mock_ytmusic, monkeypatch):
        from core.scraper import _resolve_to_ytm_url
        monkeypatch.setattr("core.spotify_match_scorer.find_best_youtube_match", lambda *a, **k: None)

        url = _resolve_to_ytm_url("Nonexistent Song", "Nonexistent Artist", 224)
        assert url == "ytsearch1:Nonexistent Artist Nonexistent Song audio"
