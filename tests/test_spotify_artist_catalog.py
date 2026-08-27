"""Spotify artist category helpers remain locale-aware and deterministic."""

from core.scraper import (
    _spotify_artist_base_url,
    _spotify_credit_matches_artist,
    _spotify_release_type,
    _spotify_section_key,
)


def test_artist_base_url_strips_discography_route():
    assert _spotify_artist_base_url(
        "https://open.spotify.com/artist/abc/discography/all?si=x"
    ) == "https://open.spotify.com/artist/abc"


def test_section_labels_support_english_and_hebrew():
    assert _spotify_section_key("Albums") == "album"
    assert _spotify_section_key("סינגלים ו-EP") == "single"
    assert _spotify_section_key("אוספים") == "compilation"
    assert _spotify_section_key("Appears On") == "appears_on"


def test_release_type_distinguishes_album_ep_single_and_compilation():
    assert _spotify_release_type("album", "", 1) == "album"
    assert _spotify_release_type("single", "EP · 4 songs", 4) == "ep"
    assert _spotify_release_type("single", "Single · 1 song", 1) == "single"
    assert _spotify_release_type("compilation", "", 12) == "compilation"


def test_appears_on_credit_filter_requires_exact_credit_not_substring():
    assert _spotify_credit_matches_artist({"artist": "Artist, Guest"}, "Artist")
    assert not _spotify_credit_matches_artist({"artist": "Artist Junior"}, "Artist")
