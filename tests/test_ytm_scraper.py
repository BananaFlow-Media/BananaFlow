import sys
import types


class _FakeYoutubeDL:
    received_url = ""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        type(self).received_url = url
        return {
            "channel_id": "UCresolvedartist123",
            "channel_url": "https://www.youtube.com/channel/UCresolvedartist123",
        }


def test_resolve_ytm_artist_id_from_browse_url():
    from utils.ytm_scraper import _resolve_ytm_artist_id

    assert (
        _resolve_ytm_artist_id("https://music.youtube.com/browse/UCabc123?feature=share")
        == "UCabc123"
    )


def test_resolve_ytm_artist_id_from_channel_url():
    from utils.ytm_scraper import _resolve_ytm_artist_id

    assert (
        _resolve_ytm_artist_id("https://music.youtube.com/channel/UCabc123/releases")
        == "UCabc123"
    )


def test_resolve_ytm_artist_id_from_handle(monkeypatch):
    fake_yt_dlp = types.SimpleNamespace(YoutubeDL=_FakeYoutubeDL)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_yt_dlp)

    from utils.ytm_scraper import _resolve_ytm_artist_id

    url = "https://music.youtube.com/@noyfadlon"
    assert _resolve_ytm_artist_id(url) == "UCresolvedartist123"
    assert _FakeYoutubeDL.received_url == url


def test_artist_shelf_types_include_videos_playlists_and_featured():
    from utils.ytm_scraper import _ARTIST_SHELF_TYPES

    assert _ARTIST_SHELF_TYPES["Videos"] == "video"
    assert _ARTIST_SHELF_TYPES["פלייליסטים"] == "playlist"
    assert _ARTIST_SHELF_TYPES["Featured on"] == "appears_on"


def test_discovery_keeps_album_single_and_video_shelves(monkeypatch):
    from utils import ytm_scraper

    def shelf(label, renderer):
        return {
            "musicCarouselShelfRenderer": {
                "header": {
                    "musicCarouselShelfBasicHeaderRenderer": {
                        "title": {"runs": [{"text": label}]},
                    }
                },
                "contents": [{"musicTwoRowItemRenderer": renderer}],
            }
        }

    def renderer(title, *, playlist_id="", video_id=""):
        result = {
            "title": {"runs": [{"text": title}]},
            "navigationEndpoint": {
                "watchEndpoint": {"videoId": video_id} if video_id else {},
            },
        }
        if playlist_id:
            result["thumbnailOverlay"] = {
                "musicItemThumbnailOverlayRenderer": {
                    "content": {
                        "musicPlayButtonRenderer": {
                            "playNavigationEndpoint": {
                                "watchPlaylistEndpoint": {"playlistId": playlist_id},
                            }
                        }
                    }
                }
            }
        return result

    response = {
        "header": {
            "musicVisualHeaderRenderer": {
                "title": {"runs": [{"text": "Artist"}]},
            }
        },
        "contents": [
            shelf("Albums", renderer("Album", playlist_id="PL_ALBUM")),
            shelf("Singles", renderer("Single", playlist_id="PL_SINGLE")),
            shelf("Videos", renderer("Video", video_id="VIDEO_ID")),
        ],
    }
    monkeypatch.setattr(ytm_scraper, "_resolve_ytm_artist_id", lambda _url: "UC1")
    monkeypatch.setattr(ytm_scraper, "_call_api", lambda *args, **kwargs: response)

    artist, sections = ytm_scraper.discover_ytm_artist_catalog(
        "https://music.youtube.com/channel/UC1"
    )

    assert artist == "Artist"
    assert list(sections) == ["album", "single", "video"]
    assert sections["video"][0]["id"] == "VIDEO_ID"
    assert sections["single"][0]["category_name"] == "סינגלים ו-EP"


def test_legacy_ytm_artist_fetch_still_excludes_video_shelf(monkeypatch):
    from utils import ytm_scraper

    monkeypatch.setattr(
        ytm_scraper,
        "discover_ytm_artist_catalog",
        lambda _url: ("Artist", {
            "album": [{"id": "album"}],
            "single": [{"id": "single"}],
            "performance": [{"id": "live"}],
            "video": [{"id": "video"}],
        }),
    )

    releases = ytm_scraper.fetch_ytm_artist_releases("https://music.youtube.com/channel/UC1")
    assert [release["id"] for release in releases] == ["album", "single", "live"]


class _FakeYTMusic:
    def get_playlist(self, _playlist_id):
        return {
            "title": "Release",
            "tracks": [
                {
                    "videoId": "video1",
                    "title": "First",
                    "artists": [{"name": "Artist"}],
                    "album": {"name": "Underlying Album A"},
                    "duration_seconds": 180,
                },
                {
                    "videoId": "video2",
                    "title": "Second",
                    "artists": [{"name": "Artist"}],
                    "album": {"name": "Underlying Album B"},
                    "duration_seconds": 181,
                },
            ],
        }


def test_ytm_playlist_exposes_original_positions_and_playlist_type(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "ytmusicapi", types.SimpleNamespace(YTMusic=_FakeYTMusic),
    )
    from core.scraper import scrape_ytm_playlist

    _title, items = scrape_ytm_playlist(
        "https://music.youtube.com/playlist?list=PL1"
    )

    assert [item["album_index"] for item in items] == [1, 2]
    assert {item["release_type"] for item in items} == {"playlist"}
    assert {item["collection_title"] for item in items} == {"Release"}
    assert {item["album"] for item in items} == {
        "Underlying Album A", "Underlying Album B",
    }


def test_ytm_album_relabels_items_before_emitting(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "ytmusicapi", types.SimpleNamespace(YTMusic=_FakeYTMusic),
    )
    from core.scraper import scrape_ytm_album

    emitted = []
    _title, items = scrape_ytm_album(
        "https://music.youtube.com/playlist?list=OLAK1",
        on_item=lambda item: emitted.append(dict(item)),
    )

    assert {item["release_type"] for item in items} == {"album"}
    assert {item["release_type"] for item in emitted} == {"album"}
    assert {item["collection_title"] for item in items} == {"Release"}


def test_ytm_artist_multitrack_single_is_normalized_to_ep(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "ytmusicapi", types.SimpleNamespace(YTMusic=_FakeYTMusic),
    )
    from core.scraper import scrape_ytm_artist

    _artist, items = scrape_ytm_artist(
        "https://music.youtube.com/channel/UC1",
        releases=[{
            "id": "PL_EP", "url": "https://www.youtube.com/playlist?list=PL_EP",
            "title": "An EP", "type": "single", "parent_artist": "Artist",
        }],
    )

    assert [item["album_index"] for item in items] == [1, 2]
    assert {item["release_type"] for item in items} == {"ep"}
    assert {item["collection_title"] for item in items} == {"Release"}
