"""Spotify artist category helpers remain locale-aware and deterministic."""

from core.scraper import (
    _SpotifyArtistReleaseRegistry,
    _spotify_album_position,
    _spotify_album_id_from_url,
    _spotify_artist_base_url,
    _spotify_credit_matches_artist,
    _spotify_release_type,
    _spotify_release_id_from_grid,
    _spotify_release_occurrence_key,
    _spotify_section_key,
    scrape_spotify_artist,
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


def test_explicit_spotify_release_type_wins_over_discovery_section():
    assert _spotify_release_type("album", "Single · 1 song", 1) == "single"
    assert _spotify_release_type("single", "Album · 12 songs", 12) == "album"
    assert _spotify_release_type("album", "אוסף · 20 שירים", 20) == "compilation"


def test_appears_on_credit_filter_requires_exact_credit_not_substring():
    assert _spotify_credit_matches_artist({"artist": "Artist, Guest"}, "Artist")
    assert not _spotify_credit_matches_artist({"artist": "Artist Junior"}, "Artist")


def test_release_id_uses_the_closest_album_link_for_a_track_grid():
    class Grid:
        @staticmethod
        def evaluate(_script):
            return "/album/21jF5jlMtzo94wbxmJ18aa"

    assert _spotify_release_id_from_grid(Grid()) == "21jF5jlMtzo94wbxmJ18aa"


def test_release_id_is_optional_when_spotify_dom_has_no_owner_link():
    class Grid:
        @staticmethod
        def evaluate(_script):
            return ""

    assert _spotify_release_id_from_grid(Grid()) == ""


def test_album_id_helper_does_not_confuse_tracks_and_albums():
    assert (
        _spotify_album_id_from_url(
            "https://open.spotify.com/album/21jF5jlMtzo94wbxmJ18aa"
        )
        == "21jF5jlMtzo94wbxmJ18aa"
    )
    assert _spotify_album_id_from_url("https://open.spotify.com/track/abc") == ""


def test_complete_spotify_release_is_not_expanded_again_in_another_section():
    registry = _SpotifyArtistReleaseRegistry()
    item = {"album_index": 1, "discovery_roles": ["album"]}
    registry.commit("release-1", "album", ["album"], [item], expected_total=1)

    should_scan, canonical, roles = registry.begin("release-1", "single")

    assert should_scan is False
    assert canonical == "album"
    assert roles == ["album", "single"]
    assert item["discovery_roles"] == ["album", "single"]


def test_incomplete_spotify_release_can_recover_missing_cross_section_tracks():
    registry = _SpotifyArtistReleaseRegistry()
    first = {"album_index": 1}
    registry.commit("release-1", "album", ["album"], [first], expected_total=2)

    should_scan, canonical, roles = registry.begin("release-1", "single")
    second = {"album_index": 2}
    registry.commit(
        "release-1", canonical, roles, [second], expected_total=2,
    )
    scan_third, _canonical, third_roles = registry.begin(
        "release-1", "compilation",
    )

    assert should_scan is True
    assert canonical == "album"
    assert first["discovery_roles"] == ["album", "single", "compilation"]
    assert second["discovery_roles"] == ["album", "single", "compilation"]
    assert scan_third is False
    assert third_roles == ["album", "single", "compilation"]


def test_spotify_releases_without_stable_ids_are_never_auto_merged():
    registry = _SpotifyArtistReleaseRegistry()

    first = registry.begin("", "album")
    second = registry.begin("", "single")

    assert first == (True, "album", ["album"])
    assert second == (True, "single", ["single"])


def test_spotify_occurrence_key_merges_roles_but_preserves_positions():
    album_position_one = _spotify_release_occurrence_key(
        section_key="album", release_id="release-1", release_title="Release",
        spotify_id="track-1", position=1, track_title="Song",
    )
    single_position_one = _spotify_release_occurrence_key(
        section_key="single", release_id="release-1", release_title="Release",
        spotify_id="track-1", position=1, track_title="Song",
    )
    repeated_position_three = _spotify_release_occurrence_key(
        section_key="album", release_id="release-1", release_title="Release",
        spotify_id="track-1", position=3, track_title="Song",
    )

    assert album_position_one == single_position_one
    assert repeated_position_three != album_position_one


def test_spotify_album_position_survives_appears_on_credit_filtering():
    assert _spotify_album_position({"album_index": 3}, 1) == 3
    assert _spotify_album_position({"album_index": ""}, 2) == 2
    assert _spotify_album_position({"album_index": "invalid"}, 4) == 4


def test_direct_spotify_artist_import_discovers_sections_without_legacy_paths(
    monkeypatch,
):
    monkeypatch.setattr(
        "core.scraper.discover_spotify_artist_sections",
        lambda *_args, **_kwargs: ("Artist", []),
    )
    monkeypatch.setattr(
        "core.scraper._scrape_spotify_artist_legacy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the legacy /album + /single path must remain inactive")
        ),
    )

    artist, tracks = scrape_spotify_artist(
        "https://open.spotify.com/artist/abc",
    )

    assert artist == "Artist"
    assert tracks == []
