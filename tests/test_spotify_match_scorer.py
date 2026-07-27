"""
tests/test_spotify_match_scorer.py  –  Offline scoring tests
==============================================================
Tests only the pure scoring functions — no yt-dlp or network.
"""

from __future__ import annotations

import sys
import types

import pytest

from core.spotify_match_scorer import (
    MatchResult,
    _artist_score,
    _channel_score,
    _duration_score,
    _normalize,
    _title_score,
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

    def test_duration_cannot_outweigh_missing_artist_evidence(self):
        cover_score, cover = score_candidate(
            "באת לי פתאום - 2020", "קרן פלס", 192,
            "באת לי פתאום קאבר", "חפצי פרג", 194,
        )
        artist_score, artist = score_candidate(
            "באת לי פתאום - 2020", "קרן פלס", 192,
            "קרן פלס ורוני אלטר - באת לי פתאום", "קרן פלס", 202,
        )

        assert cover["artist"] == 0.0
        assert cover["unexpected_versions"] == ["cover"]
        assert artist["artist"] >= 16.0
        assert artist_score > cover_score

    @pytest.mark.parametrize("source_title, candidate_title", [
        ("Song (Cover)", "Sample Artist - Song (Cover)"),
        ("Song - Live", "Sample Artist - Song (Live)"),
        ("Song (Remix)", "Sample Artist - Song (Remix)"),
    ])
    def test_requested_version_is_not_penalized(self, source_title, candidate_title):
        _score, breakdown = score_candidate(
            source_title, "Sample Artist", 180,
            candidate_title, "Sample Artist - Topic", 180,
        )
        assert breakdown["version_penalty"] == 0.0
        assert breakdown["unexpected_versions"] == []
        assert breakdown["missing_versions"] == []


class TestFlatYoutubeSearch:
    def _find_with_entries(
        self, monkeypatch, entries, *, title="Sample Song", artist="Sample Artist", duration=180,
    ):
        """Run the matcher against a yt-dlp stand-in and retain its options."""
        captured = {}

        class FakeYoutubeDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, query, download):
                captured["query"] = query
                captured["download"] = download
                return {"entries": entries}

        monkeypatch.setitem(
            sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        )
        from core.spotify_match_scorer import find_best_youtube_match

        result = find_best_youtube_match(
            title=title, artist=artist, duration_sec=duration
        )
        return result, captured

    def test_searches_flat_results_with_the_fields_needed_for_scoring(self, monkeypatch):
        result, captured = self._find_with_entries(monkeypatch, [{
            "id": "good-id",
            "url": "good-id",
            "title": "Sample Artist - Sample Song (Official Audio)",
            "channel": "Sample Artist - Topic",
            "duration": 180,
        }])

        assert captured["extract_flat"] is True
        assert captured["skip_download"] is True
        assert captured["download"] is False
        assert result is not None
        assert result.duration_sec == 180
        assert result.url == "https://www.youtube.com/watch?v=good-id"

    def test_flat_duration_preserves_the_better_match_choice(self, monkeypatch):
        result, _ = self._find_with_entries(monkeypatch, [
            {
                "id": "wrong-duration",
                "url": "https://www.youtube.com/watch?v=wrong-duration",
                "title": "Sample Artist - Sample Song (Official Audio)",
                "channel": "Sample Artist Official",
                "duration": 420,
            },
            {
                "id": "right-duration",
                "url": "https://www.youtube.com/watch?v=right-duration",
                "title": "Sample Artist - Sample Song (Official Audio)",
                "channel": "Sample Artist - Topic",
                "duration": 180,
            },
        ])

        assert result is not None
        assert result.url.endswith("right-duration")

    def test_missing_flat_duration_is_scored_conservatively(self, monkeypatch):
        result, _ = self._find_with_entries(monkeypatch, [{
            "id": "no-duration",
            "title": "Sample Artist - Sample Song",
            "uploader": "Sample Artist",
        }])

        assert result is not None
        assert result.duration_sec is None
        assert result.breakdown["duration"] == 9.0

    def test_deep_validation_repairs_missing_duration_before_accepting(self, monkeypatch):
        calls = []
        flat = [
            {
                "id": "flat-winner", "title": "Sample Artist - Sample Song",
                "channel": "Sample Artist Official",
            },
            {
                "id": "flat-runner-up", "title": "Sample Artist - Sample Song",
                "channel": "Sample Artist - Topic",
            },
        ]
        deep = {
            "https://www.youtube.com/watch?v=flat-winner": {
                "id": "flat-winner", "title": "Sample Artist - Sample Song",
                "channel": "Sample Artist Official", "duration": 420,
            },
            "https://www.youtube.com/watch?v=flat-runner-up": {
                "id": "flat-runner-up", "title": "Sample Artist - Sample Song",
                "channel": "Sample Artist - Topic", "duration": 180,
            },
        }

        class FakeYoutubeDL:
            def __init__(self, _opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, query, download):
                calls.append(query)
                return {"entries": flat} if query.startswith("ytsearch") else deep[query]

        monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
        from core.spotify_match_scorer import find_best_youtube_match

        result = find_best_youtube_match(
            title="Sample Song", artist="Sample Artist", duration_sec=180
        )

        assert result is not None
        assert result.url.endswith("flat-runner-up")
        assert calls[0] == "ytsearch5:Sample Artist Sample Song audio"
        assert set(calls[1:]) == {
            "https://www.youtube.com/watch?v=flat-winner",
            "https://www.youtube.com/watch?v=flat-runner-up",
        }

    def test_decisive_flat_result_does_not_trigger_deep_extraction(self, monkeypatch):
        calls = []

        class FakeYoutubeDL:
            def __init__(self, _opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, query, download):
                calls.append(query)
                return {"entries": [{
                    "id": "certain", "title": "Sample Artist - Sample Song",
                    "channel": "Sample Artist - Topic", "duration": 180,
                }]}

        monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
        from core.spotify_match_scorer import find_best_youtube_match

        result = find_best_youtube_match(
            title="Sample Song", artist="Sample Artist", duration_sec=180
        )

        assert result is not None
        assert calls == ["ytsearch5:Sample Artist Sample Song audio"]

    def test_rejects_cover_without_source_artist_and_selects_artist_recording(self, monkeypatch):
        source_title = "\u05d1\u05d0\u05ea \u05dc\u05d9 \u05e4\u05ea\u05d0\u05d5\u05dd - 2020"
        source_artist = "\u05e7\u05e8\u05df \u05e4\u05dc\u05e1"
        result, _ = self._find_with_entries(monkeypatch, [
            {
                "id": "cover", "title": "באת לי פתאום קאבר",
                "channel": "חפצי פרג", "duration": 194,
            },
            {
                "id": "keren", "title": "קרן פלס ורוני אלטר - באת לי פתאום",
                "channel": "קרן פלס", "duration": 202,
            },
        ], title=source_title, artist=source_artist, duration=192)

        assert result is not None
        assert result.url.endswith("keren")

    def test_returns_no_match_when_flat_and_deep_lack_artist_evidence(self, monkeypatch):
        result, _ = self._find_with_entries(monkeypatch, [{
            "id": "wrong-artist", "title": "Sample Song",
            "channel": "Other Artist", "duration": 180,
        }])

        assert result is None


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
        def mock_find_best(title, artist, duration_sec, min_confidence=0.55, cookies_file=None):
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

    def test_resolve_no_safe_match_returns_empty(self, mock_ytmusic, monkeypatch):
        from core.scraper import _resolve_to_ytm_url
        monkeypatch.setattr("core.spotify_match_scorer.find_best_youtube_match", lambda *a, **k: None)

        url = _resolve_to_ytm_url("Nonexistent Song", "Nonexistent Artist", 224)
        assert url == ""
