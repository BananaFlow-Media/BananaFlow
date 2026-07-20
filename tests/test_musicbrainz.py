from unittest.mock import MagicMock, patch
import pytest
from core.musicbrainz_enricher import _clean_search_term, _find_recording


def test_clean_search_term():
    """Test cleaning of noisy titles, artists, and albums for MusicBrainz search."""
    # Test feat/ft removal
    assert _clean_search_term("Song Title (feat. Artist Name)") == "Song Title"
    assert _clean_search_term("Song Title [feat Artist]") == "Song Title"
    assert _clean_search_term("Song Title feat. Artist") == "Song Title"
    assert _clean_search_term("Song Title ft. Artist") == "Song Title"

    # Test remastered/live removal
    assert _clean_search_term("Song Title (Remastered 2020)") == "Song Title"
    assert _clean_search_term("Song Title - 2011 Remaster") == "Song Title"
    assert _clean_search_term("Song Title (Live)") == "Song Title"
    assert _clean_search_term("Song Title - Live from London") == "Song Title"

    # Test common annotations
    assert _clean_search_term("Song Title (Official Video)") == "Song Title"
    assert _clean_search_term("Song Title - Lyric Video") == "Song Title"

    # Test Lucene character sanitization
    assert _clean_search_term("Title: Subtitle (Part 1)") == "Title Subtitle Part 1"
    assert _clean_search_term('My "Favorite" Song!') == "My Favorite Song"


@patch("core.musicbrainz_enricher._do_mb_search")
def test_find_recording_strict_success(mock_search):
    """Test find_recording succeeds on the first strict query."""
    mock_search.return_value = [{"id": "rec-123", "title": "Test Song", "length": 180000}]

    result = _find_recording("Test Song (feat. Noise)", "Artist Name", "Album Name", 180)

    assert result is not None
    assert result["id"] == "rec-123"
    assert mock_search.call_count == 1

    # Verify the query was cleaned and used strict double quotes
    args, _ = mock_search.call_args
    assert 'recording:"Test Song"' in args[0]
    assert 'artist:"Artist Name"' in args[0]
    assert 'release:"Album Name"' in args[0]


@patch("core.musicbrainz_enricher._do_mb_search")
def test_find_recording_fallback_no_album(mock_search):
    """Test find_recording falls back to Title + Artist search if Album fails."""
    # First call (Strict) fails, second call (Title+Artist) succeeds
    mock_search.side_effect = [
        [],
        [{"id": "rec-456", "title": "Test Song", "length": 180000}]
    ]

    result = _find_recording("Test Song", "Artist Name", "Album Name", 180)

    assert result is not None
    assert result["id"] == "rec-456"
    assert mock_search.call_count == 2

    # Verify first query used album, second did not
    calls = mock_search.call_args_list
    assert 'release:"Album Name"' in calls[0][0][0]
    assert 'release:"Album Name"' not in calls[1][0][0]
    assert 'recording:"Test Song"' in calls[1][0][0]


@patch("core.musicbrainz_enricher._do_mb_search")
def test_find_recording_fallback_fuzzy(mock_search):
    """Test find_recording falls back to Fuzzy/Soft search if first two attempts fail."""
    # 1. Strict (with album) -> fails
    # 2. Strict (no album) -> fails
    # 3. Fuzzy -> succeeds
    mock_search.side_effect = [
        [],
        [],
        [{"id": "rec-789", "title": "Test Song", "length": 180000}]
    ]

    result = _find_recording("Test Song", "Artist Name", "Album Name", 180)

    assert result is not None
    assert result["id"] == "rec-789"
    assert mock_search.call_count == 3

    # Verify the last query used fuzzy style (parentheses instead of strict quotes)
    calls = mock_search.call_args_list
    assert "recording:(Test Song)" in calls[2][0][0]
    assert "artist:(Artist Name)" in calls[2][0][0]


@patch("core.musicbrainz_enricher._do_mb_search")
def test_find_recording_failure(mock_search):
    """Test find_recording returns None if all attempts fail."""
    mock_search.return_value = []

    result = _find_recording("Test Song", "Artist Name", "Album Name", 180)

    assert result is None
    assert mock_search.call_count == 3
