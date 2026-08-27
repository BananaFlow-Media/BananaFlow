"""Offline coverage for staged artist discovery and duplicate decisions."""

from __future__ import annotations

from core.artist_catalog import (
    ArtistCatalogDiscovery,
    ArtistCatalogSection,
    apply_catalog_decisions,
    deduplicate_catalog_occurrences,
    detect_catalog_duplicates,
    discover_artist_catalog,
    scrape_artist_catalog,
)
from core.playlist_parser import SourcePlatform


def _track(
    title: str,
    section: str,
    *,
    artist: str = "Artist",
    duration: int = 180,
    spotify_id: str = "",
    video_id: str = "",
    album: str = "Release",
    position: int = 1,
    release_id: str = "",
) -> dict:
    return {
        "title": title,
        "artist": artist,
        "duration_sec": duration,
        "platform": "spotify" if spotify_id else "ytmusic",
        "spotify_id": spotify_id,
        "source_id": video_id,
        "catalog_section": section,
        "category": section,
        "album": album,
        "album_index": position,
        "source_release_id": release_id,
    }


def test_exact_spotify_id_is_grouped_across_album_and_single():
    tracks = [
        _track("Song", "album", spotify_id="same"),
        _track("Song", "single", spotify_id="same"),
    ]
    groups = detect_catalog_duplicates(tracks)
    assert len(groups) == 1
    assert groups[0].confidence == "exact"
    assert groups[0].indices == (0, 1)


def test_exact_youtube_video_id_is_grouped_across_categories():
    tracks = [
        _track("Song", "single", video_id="vid-1"),
        _track("Song", "video", video_id="vid-1"),
    ]
    groups = detect_catalog_duplicates(tracks)
    assert len(groups) == 1
    assert groups[0].confidence == "exact"


def test_probable_match_requires_same_title_artist_and_near_duration():
    tracks = [
        _track("Song", "album", spotify_id="one", duration=180),
        _track("Song", "single", spotify_id="two", duration=182),
    ]
    groups = detect_catalog_duplicates(tracks)
    assert len(groups) == 1
    assert groups[0].confidence == "probable"


def test_live_and_studio_versions_are_not_collapsed():
    tracks = [
        _track("Song", "album", spotify_id="one"),
        _track("Song (Live)", "performance", spotify_id="two"),
    ]
    assert detect_catalog_duplicates(tracks) == []


def test_same_occurrence_repeated_inside_one_category_is_not_reviewed():
    tracks = [
        _track("Song", "album", spotify_id="same"),
        _track("Song", "album", spotify_id="same"),
    ]
    assert detect_catalog_duplicates(tracks) == []


def test_duplicates_across_releases_inside_one_category_are_reviewed():
    tracks = [
        _track("Song", "album", spotify_id="same", album="Original", release_id="a"),
        _track("Song", "album", spotify_id="same", album="Deluxe", release_id="b"),
    ]
    groups = detect_catalog_duplicates(tracks)
    assert len(groups) == 1
    assert groups[0].confidence == "exact"
    assert groups[0].indices == (0, 1)


def test_exact_repeat_of_a_release_occurrence_is_collapsed_once():
    original = _track(
        "Song", "album", spotify_id="same", album="Album", position=3,
        release_id="release-1",
    )
    duplicate = dict(original)
    other_release = dict(original, album="Album Deluxe", source_release_id="release-2")

    assert deduplicate_catalog_occurrences(
        [original, duplicate, other_release]
    ) == [original, other_release]


def test_decisions_keep_selected_occurrence_and_preserve_order():
    tracks = [
        _track("Song", "album", spotify_id="same"),
        _track("Song", "single", spotify_id="same"),
        _track("Other", "album", spotify_id="other"),
    ]
    groups = detect_catalog_duplicates(tracks)
    kept = apply_catalog_decisions(tracks, groups, {groups[0].group_id: {1}})
    assert kept == [tracks[1], tracks[2]]


def test_ytm_discovery_preserves_all_nonempty_categories(monkeypatch):
    monkeypatch.setattr(
        "utils.ytm_scraper.discover_ytm_artist_catalog",
        lambda _url: ("Artist", {
            "album": [{"id": "a"}],
            "single": [{"id": "s"}],
            "video": [{"id": "v"}],
        }),
    )
    result = discover_artist_catalog(
        "https://music.youtube.com/channel/UC1",
        SourcePlatform.YOUTUBE_MUSIC,
    )
    assert result.artist_name == "Artist"
    assert [section.key for section in result.sections] == ["album", "single", "video"]


def test_selected_ytm_categories_are_the_only_releases_scraped(monkeypatch):
    discovery = ArtistCatalogDiscovery(
        url="https://music.youtube.com/channel/UC1",
        platform=SourcePlatform.YOUTUBE_MUSIC,
        artist_name="Artist",
        sections=[
            ArtistCatalogSection("album", releases=({"id": "a", "type": "album"},)),
            ArtistCatalogSection("single", releases=({"id": "s", "type": "single"},)),
        ],
    )
    captured = {}

    def fake_scrape(_url, **kwargs):
        captured["releases"] = kwargs["releases"]
        return "Artist", [{"title": "Song", "url": "https://music.youtube.com/watch?v=x"}]

    monkeypatch.setattr("core.scraper.scrape_ytm_artist", fake_scrape)
    started = []
    tracks = scrape_artist_catalog(discovery, ["single"], on_section=started.append)

    assert [release["id"] for release in captured["releases"]] == ["s"]
    assert started == ["single"]
    assert tracks[0]["source_kind"] == "ARTIST"
    assert tracks[0]["source_url"] == discovery.url


def test_ytm_release_found_in_two_selected_shelves_is_expanded_once(monkeypatch):
    discovery = ArtistCatalogDiscovery(
        url="https://music.youtube.com/channel/UC1",
        platform=SourcePlatform.YOUTUBE_MUSIC,
        artist_name="Artist",
        sections=[
            ArtistCatalogSection("album", releases=({
                "id": "MPRE-release", "type": "album", "title": "Release",
            },)),
            ArtistCatalogSection("single", releases=({
                "id": "MPRE-release", "type": "single", "title": "Release",
            },)),
        ],
    )
    captured = {}

    def fake_scrape(_url, **kwargs):
        captured["releases"] = kwargs["releases"]
        return "Artist", []

    monkeypatch.setattr("core.scraper.scrape_ytm_artist", fake_scrape)

    scrape_artist_catalog(discovery, ["album", "single"])

    assert len(captured["releases"]) == 1
    assert captured["releases"][0]["id"] == "MPRE-release"
    assert captured["releases"][0]["type"] == "album"
    assert captured["releases"][0]["discovery_roles"] == ["album", "single"]


def test_ytm_same_named_releases_without_ids_are_not_merged(monkeypatch):
    discovery = ArtistCatalogDiscovery(
        url="https://music.youtube.com/channel/UC1",
        platform=SourcePlatform.YOUTUBE_MUSIC,
        artist_name="Artist",
        sections=[
            ArtistCatalogSection("album", releases=({"title": "Greatest Hits"},)),
            ArtistCatalogSection("single", releases=({"title": "Greatest Hits"},)),
        ],
    )
    captured = {}
    monkeypatch.setattr(
        "core.scraper.scrape_ytm_artist",
        lambda _url, **kwargs: captured.setdefault("result", (
            "Artist", kwargs["releases"],
        )),
    )

    tracks = scrape_artist_catalog(discovery, ["album", "single"])

    assert len(tracks) == 2
    assert [track["type"] for track in tracks] == ["album", "single"]


def test_selected_spotify_labels_are_forwarded_and_pending(monkeypatch):
    discovery = ArtistCatalogDiscovery(
        url="https://open.spotify.com/artist/abc",
        platform=SourcePlatform.SPOTIFY,
        artist_name="Artist",
        sections=[
            ArtistCatalogSection("album", "Albums"),
            ArtistCatalogSection("single", "Singles and EPs"),
        ],
    )
    captured = {}

    def fake_scrape(_url, **kwargs):
        captured.update(kwargs)
        return "Artist", [{"title": "Song", "artist": "Artist", "spotify_id": "id"}]

    monkeypatch.setattr("core.scraper.scrape_spotify_artist", fake_scrape)
    tracks = scrape_artist_catalog(discovery, ["single"])

    assert captured["selected_sections"] == {"single": "Singles and EPs"}
    assert captured["metadata_only"] is True
    assert tracks[0]["match_status"] == "pending"
    assert tracks[0]["url"].startswith("ytsearch1:")


def test_artist_scan_collapses_a_provider_returning_the_whole_release_twice(
    monkeypatch,
):
    discovery = ArtistCatalogDiscovery(
        url="https://open.spotify.com/artist/abc",
        platform=SourcePlatform.SPOTIFY,
        artist_name="Artist",
        sections=[ArtistCatalogSection("album", "Albums")],
    )
    occurrence = {
        "title": "Song",
        "artist": "Artist",
        "album": "Only Album",
        "album_index": 1,
        "duration_sec": 180,
        "spotify_id": "track-id",
        "catalog_section": "album",
        "platform": "spotify",
    }
    monkeypatch.setattr(
        "core.scraper.scrape_spotify_artist",
        lambda *_args, **_kwargs: ("Artist", [occurrence, dict(occurrence)]),
    )

    tracks = scrape_artist_catalog(discovery, ["album"])

    assert len(tracks) == 1
    assert tracks[0]["spotify_id"] == "track-id"
