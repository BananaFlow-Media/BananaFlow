import json
import pytest
from core.scraper import _parse_spotify_json_fallback, _extract_spotify_data_from_json


def test_extract_spotify_data_from_json_playlist():
    """Test generic JSON traversal for a Spotify playlist payload."""
    payload = {
        "name": "My Great Hits",
        "type": "playlist",
        "tracks": {
            "items": [
                {
                    "track": {
                        "type": "track",
                        "name": "Easy On Me",
                        "uri": "spotify:track:4tv53J43423423",
                        "duration_ms": 224000,
                        "artists": [
                            {"name": "Adele", "type": "artist"}
                        ],
                        "album": {
                            "name": "30",
                            "images": [
                                {"url": "https://i.scdn.co/image/ab67616d0000b27357", "width": 640, "height": 640}
                            ]
                        }
                    }
                }
            ]
        }
    }

    title, items = _extract_spotify_data_from_json(payload, "Playlist")

    assert title == "My Great Hits"
    assert len(items) == 1
    assert items[0]["title"] == "Easy On Me"
    assert items[0]["artist"] == "Adele"
    assert items[0]["album"] == "30"
    assert items[0]["duration_sec"] == 224
    assert "https://i.scdn.co/image/" in items[0]["thumbnail_url"]


def test_parse_spotify_json_fallback_script_tag():
    """Test parsing embedded initial-state JSON from a script tag in HTML."""
    html = """
    <html>
        <body>
            <script id="initial-state" type="application/json">
            {
                "type": "album",
                "name": "Mock Album",
                "tracks": [
                    {
                        "type": "track",
                        "name": "Track One",
                        "uri": "spotify:track:111111",
                        "duration_ms": 150000,
                        "artists": [{"name": "Mock Artist"}],
                        "album": {"name": "Mock Album", "images": []}
                    }
                ]
            }
            </script>
        </body>
    </html>
    """

    title, items = _parse_spotify_json_fallback(html, "Album")
    assert title == "Mock Album"
    assert len(items) == 1
    assert items[0]["title"] == "Track One"
    assert items[0]["artist"] == "Mock Artist"
    assert items[0]["duration_sec"] == 150


def test_parse_spotify_json_fallback_js_assignment():
    """Test parsing embedded JSON from window.__INITIAL_STATE__ JS assignment."""
    html = """
    <html>
        <body>
            <script>
                window.__INITIAL_STATE__ = {
                    "type": "playlist",
                    "name": "Dynamic Playlist",
                    "tracks": [
                        {
                            "type": "track",
                            "name": "Track Two",
                            "uri": "spotify:track:222222",
                            "duration_ms": 180000,
                            "artists": [{"name": "Second Artist"}],
                            "album": {"name": "Some Album"}
                        }
                    ]
                };
            </script>
        </body>
    </html>
    """

    title, items = _parse_spotify_json_fallback(html, "Playlist")
    assert title == "Dynamic Playlist"
    assert len(items) == 1
    assert items[0]["title"] == "Track Two"
    assert items[0]["artist"] == "Second Artist"
    assert items[0]["duration_sec"] == 180
