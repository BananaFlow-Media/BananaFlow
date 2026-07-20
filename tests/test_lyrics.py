from unittest.mock import MagicMock, patch
import pytest
from core.lyrics_embedder import _fetch_lyrics_genius_fallback


@pytest.fixture
def mock_search_response():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "response": {
            "sections": [
                {
                    "type": "top_hit",
                    "hits": [
                        {
                            "result": {
                                "title": "Test Title",
                                "primary_artist": {"name": "Test Artist"},
                                "url": "https://genius.com/Test-artist-test-title-lyrics"
                            }
                        }
                    ]
                }
            ]
        }
    }
    return response


@pytest.fixture
def mock_lyrics_response():
    response = MagicMock()
    response.status_code = 200
    response.text = """
    <html>
        <body>
            <div data-lyrics-container="true">12 ContributorsTest Title Lyrics Some song annotation description... Read More[Intro]<br/>Lyrics line 1<br/>Lyrics line 2</div>
            <div data-lyrics-container="true">Lyrics line 3<br/>Embed</div>
        </body>
    </html>
    """
    return response


def test_genius_fallback_success(mock_search_response, mock_lyrics_response):
    """Test _fetch_lyrics_genius_fallback successfully fetches, parses and cleans up Genius lyrics."""
    with patch("requests.get") as mock_get:
        # Mock first call (search) and second call (lyrics page)
        mock_get.side_effect = [mock_search_response, mock_lyrics_response]

        lyrics = _fetch_lyrics_genius_fallback("Test Title", "Test Artist")

        # Verify both requests were made
        assert mock_get.call_count == 2

        # Verify the returned lyrics were cleaned up correctly
        assert lyrics is not None
        assert "Contributors" not in lyrics
        assert "Read More" not in lyrics
        assert "Embed" not in lyrics
        assert lyrics.startswith("[Intro]")
        assert "Lyrics line 1\nLyrics line 2\nLyrics line 3" in lyrics


def test_genius_fallback_search_failure():
    """Test _fetch_lyrics_genius_fallback returns None if search request fails."""
    mock_search = MagicMock()
    mock_search.status_code = 404

    with patch("requests.get", return_value=mock_search):
        lyrics = _fetch_lyrics_genius_fallback("Test Title", "Test Artist")
        assert lyrics is None


def test_genius_fallback_lyrics_page_failure(mock_search_response):
    """Test _fetch_lyrics_genius_fallback returns None if lyrics page fails to load."""
    mock_lyrics = MagicMock()
    mock_lyrics.status_code = 500

    with patch("requests.get") as mock_get:
        mock_get.side_effect = [mock_search_response, mock_lyrics]
        lyrics = _fetch_lyrics_genius_fallback("Test Title", "Test Artist")
        assert lyrics is None
